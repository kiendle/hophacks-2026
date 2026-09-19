# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "anthropic>=0.75", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/signal_server.py   then open http://127.0.0.1:5194

One server for the whole product. The Morning Brief page is the home page and carries the
observation assistant's chat widget, and one API serves both: the collector and the briefs
(morning-brief/server.py) plus the chat sessions (harness/bridge.py).

Nothing is copied. Both modules are imported and their handlers, middleware and cleanup contexts are
registered on one fresh Application, so `morning-brief/server.py` and `harness/bridge.py` each still
run on their own. No file under morning-brief/web/ is touched: the widget markup is read out of
harness/web/index.html and injected into the page on the way out.
"""
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
MB = ROOT.parent / "morning-brief"


def load_env(path):
    """morning-brief/server.py's own loader: setdefault, so a real environment variable always wins."""
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        key, separator, value = line.partition("=")
        if separator and value.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env(ROOT.parent / ".env")  # before the imports: claude_runner reads its settings at import time
sys.path[:0] = [str(path) for path in (ROOT, MB) if str(path) not in sys.path]

import bridge  # noqa: E402
import collector  # noqa: E402  (compile_terms, so search matches exactly as collecting does)
import server  # noqa: E402  (loads morning-brief/.env, then builds the Morning Brief app and routes)

WEB = ROOT / "web"
WIDGET = {"/chat.js": (WEB / "chat.js", "text/javascript"), "/chat.css": (WEB / "chat.css", "text/css")}
MARKERS = ("<!-- chat-widget:start -->", "<!-- chat-widget:end -->")
# chat.js wires the landing page's two intro buttons unconditionally; this page has neither.
SHIM = '  <button id="open-chat" type="button" hidden></button><button id="open-chat-data" type="button" hidden></button>\n'
MAX_TERMS, MAX_TERM = 12, 80

if os.environ.get("SIGNAL_DATA"):  # a test or a second instance collects somewhere else: two collectors on one folder corrupt posts.jsonl
    server.DATA = Path(os.environ["SIGNAL_DATA"])
    server.BRIEFS = server.DATA / "briefs"


def widget():
    """The launcher and panel markup, read out of the landing page between its two markers."""
    try:  # harness/web/ is edited live: a landing page that is missing, moved or half-written costs the widget, never the home page
        text = (WEB / "index.html").read_text(encoding="utf-8")
        start, end = text.find(MARKERS[0]), text.find(MARKERS[1])
        if start < 0 or end < start:
            raise ValueError(f"{MARKERS[0]} is missing")
        return text[start:end + len(MARKERS[1])] + "\n"
    except (OSError, ValueError) as error:
        print(f"signal_server: harness/web/index.html: {error}; serving the page without the chat widget", flush=True)
        return ""


async def home(request):
    """The Morning Brief page as it is on disk, with the chat widget injected. Read per request."""
    path, _ = server.ASSETS["/"]
    page = path.read_text(encoding="utf-8")
    markup = widget()
    if markup:  # with no panel to wire, chat.js would only throw, so the page goes out as plain Morning Brief
        page = page.replace("</head>", '  <link rel="stylesheet" href="/chat.css">\n</head>', 1)
        page = page.replace("</body>", f'{markup}{SHIM}  <script type="module" src="/chat.js"></script>\n</body>', 1)
    return web.Response(text=page, content_type="text/html", charset="utf-8", headers=server.HEADERS)


async def widget_asset(request):
    path, content_type = WIDGET[request.path]
    try:
        body = path.read_bytes()
    except OSError:  # the clean 404 bridge.asset() gives while harness/web/ is being edited, rather than a 500
        return server.fail(404, "No such file.")
    return web.Response(body=body, content_type=content_type, charset="utf-8", headers=server.HEADERS)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def search(request):
    """Keyword search over the posts the collector holds in memory; nothing is read from disk."""
    store = request.app["collector"].store
    terms = [term.strip() for term in (request.query.get("q") or "").split(",") if term.strip()]
    if not terms or len(terms) > MAX_TERMS or any(len(term) > MAX_TERM for term in terms):
        return server.fail(400, f"Send q as 1 to {MAX_TERMS} comma-separated keywords of at most {MAX_TERM} characters.")
    try:
        hours, limit = float(request.query.get("hours", 8)), int(request.query.get("limit", 20))
    except ValueError:
        return server.fail(400, "hours and limit must be numbers.")
    if not 1 <= hours <= 36 or not 1 <= limit <= 50:
        return server.fail(400, "Choose hours between 1 and 36 and limit between 1 and 50.")
    interest = request.query.get("interest") or None
    known = [followed["id"] for followed in store.interests]
    if interest and interest not in known:
        return server.fail(404, f"No interest {interest!r} is followed. Followed: {', '.join(known) or 'none'}.")
    pattern = collector.compile_terms(terms)  # whole word, and short acronyms stay case-sensitive, so "AI" never matches "said"
    cutoff = collector.now_ms() - hours * 3600_000
    hits = sorted((post for post in store.posts.values() if post["t"] >= cutoff and (interest is None or interest in post["topics"])
                   and pattern.search(post["text"])), key=lambda post: post["t"], reverse=True)
    return web.json_response({
        "keywords": terms, "hours": hours, "interest": interest, "total": len(hits), "returned": min(len(hits), limit),
        "kept_posts": len(store.posts), "matching": "whole word in the post text; short acronyms are case-sensitive",
        "posts": [{
            "uri": post["uri"], "url": f"https://bsky.app/profile/{post['did']}/post/{post['rkey']}", "text": post["text"][:280],
            "topics": post["topics"], "langs": post["langs"], "time": iso(post["t"]), "t": post["t"],
        } for post in hits[:limit]],
    }, headers=server.HEADERS)


def hold(directory):
    """Own the data folder, or refuse to start: two collectors appending to one posts.jsonl corrupt it.

    A port probe only guards one port, and the folder is what is at stake. The lock is the open handle,
    not the file, so the OS drops it when the process dies and a crash never wedges the folder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    handle = os.open(directory / "collector.lock", os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        raise SystemExit(f"Another Morning Brief or Signal process is already collecting into {directory}; stop that one first, whatever port it listens on.")
    return handle


async def one_collector(app):
    """Taken before server.lifecycle, so the folder is owned before the collector opens posts.jsonl."""
    handle = hold(server.DATA)
    yield
    os.close(handle)


def compose():
    """Morning Brief's handlers, middleware and cleanup, plus the chat API, the widget and one home page."""
    app = web.Application(middlewares=[server.local_only, bridge.local_only], client_max_size=64 * 1024)  # the chat composer's limit
    app["state"] = server.app["state"]
    app.cleanup_ctx.extend([one_collector, server.lifecycle])
    for route in server.app.router.routes():  # the same handlers, minus "/": home() serves that with the widget in it
        if route.resource.canonical != "/":
            app.router.add_route(route.method, route.resource.canonical, route.handler)
    bridge.attach(app)
    app.add_routes([web.get("/", home), *[web.get(path, widget_asset) for path in WIDGET], web.get("/api/posts/search", search)])
    return app


def claim(port):
    """Refuse to start on a taken port. Moving ports is never the answer here: one process owns the data folder."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"127.0.0.1:{port} is already in use, so Signal is probably already running; stop that one first.")


app = compose()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5194))
    claim(port)  # before the collector starts and before the data folder is opened
    web.run_app(app, host="127.0.0.1", port=port)
