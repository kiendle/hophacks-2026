# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb==1.5.5", "jsonschema>=4.23,<5", "pytz"]
# ///
"""Run: python -m uv run harness/bridge.py   then open http://127.0.0.1:5195

The website's chat widget talks only to this process. Each message spawns the local Claude Code
(claude_runner.py), whose only tools are our MCP server's (demo_mcp_server.py), and its answer is
streamed back as server-sent events. The Confirm button posts here, not to the model: the harness
writes the confirmation record, and `submit_project` checks it — see harness/DESIGN.md §0 and §10.

mcp and duckdb are declared above although this file does not import them: the MCP server runs on
this interpreter (claude_runner.mcp_config), so uv must install them into it.

Optional route modules (harness/voice.py, harness/analysis_api.py) are picked up by `attach`, so a
stream of work can add /api routes without editing this file and the combined server gets them too.

The React app in web/dist is served from here too (spa_page, spa_asset and spa_headers), so the
combined server and this one hand out the same files under the same policy.
"""
import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path

from aiohttp import web

from agent_runner import Runner

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DIST = ROOT.parent / "web" / "dist"  # the React interface, written by npm run build in web/
SESSIONS = ROOT / "state/sessions"
ASSETS = {"/signal.css": (ROOT.parent / "jetstream-demo/styles.css", "text/css")}  # one visual system for every demo
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
         ".json": "application/json", ".map": "application/json", ".woff2": "font/woff2", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
         ".ico": "image/x-icon", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".webm": "audio/webm", ".txt": "text/plain"}
TEXTUAL = {"text/html", "text/javascript", "text/css", "image/svg+xml", "application/json", "text/plain"}
HEADERS = {
    "Cache-Control": "no-store",
    # media-src carries the voice recording the page makes of itself (a blob: url); everything else
    # a page may load still comes from this server alone.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "microphone=(self)",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Optional modules, each with setup(app). One that is not there is ignored, one that is broken is
# skipped with its traceback, so a stream of work that is still being written never costs the server.
PLUGINS = ("voice", "analysis_api", "realtime_api", "live_voice", "agent_voice", "stream_api", "setup_api", "ui_server", "brief_delivery", "automation_api", "translation_api")
VOICE_MAX = 10 * 1024 * 1024  # a recorded clip, on the voice routes only
PLACEHOLDER = b"<!doctype html><title>Signal harness</title><p>The chat page has not been written yet. The API is live.\n"
ASSET_CACHE = "public, max-age=31536000, immutable"  # every name under dist/assets carries its own build hash
HOSTNAME = re.compile(r"[A-Za-z0-9.:\[\]-]{1,120}")  # what may be repeated back into a header
BUILD_FIRST = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Sentimeter</title>
<h1>The interface is not built yet</h1>
<p>Build the interface first: run npm run build in the web folder.</p>
<p>Then reload this page.</p>
</html>
"""
CONFIRMATION = re.compile(r"[A-Za-z0-9_-]{16}")
GONE = (ConnectionResetError, RuntimeError, OSError)  # the page may be gone before the last frame is written
MAX_TURNS = 2  # at most two Claude Code processes at once: one laptop, one subscription (DESIGN.md §0)
SSE_HEARTBEAT_SECONDS = 10


def fail(status, message):
    return web.json_response({"error": message}, status=status, headers=HEADERS)


@web.middleware
async def local_only(request, handler):
    """Same origin for anything that changes something, and for the live chart's socket.

    A WebSocket handshake is a GET and gets no preflight, so its Origin header is the only check there
    is; past that check the upgrade is handed straight to its handler, which is what it needs to work.
    """
    upgrading = request.headers.get("Upgrade", "").lower() == "websocket"
    if upgrading or (request.path.startswith("/api/") and request.method != "GET"):
        if request.url.host not in ("127.0.0.1", "localhost") or request.headers.get("Origin", str(request.url.origin())) != str(request.url.origin()):
            return fail(403, "Only same-origin local requests are accepted.")
        if request.path.startswith("/api/voice/"):
            # aiohttp has no per-route body limit: the limit is read when the body is read, so raising
            # it here raises it for this one request and leaves the chat composer's 64 KB everywhere else.
            with contextlib.suppress(AttributeError):
                request._client_max_size = VOICE_MAX  # noqa: SLF001
    return await handler(request)


def web_file(name, root=None):
    """A file of any name under harness/web/, and never one outside it.

    root lets the combined server hand in its own copy of the folder, so both servers share one rule.
    """
    root = root or WEB
    parts = str(name or "").split("/")
    if not parts or any(part in ("", ".", "..") or part.startswith(".") or "\\" in part or ":" in part for part in parts):
        return None
    try:
        path = (root / "/".join(parts)).resolve()
        path.relative_to(root.resolve())  # a symlink out of the folder resolves out of it and is refused here
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


async def asset(request):
    """Read per request, so the front end can be edited without restarting the server."""
    mapped = ASSETS.get(request.path)
    path = mapped[0] if mapped else web_file(request.match_info.get("name") or "index.html")
    if path is None or not path.exists():
        if request.path == "/":
            return web.Response(body=PLACEHOLDER, content_type="text/html", charset="utf-8", headers=HEADERS)
        return fail(404, "No such file.")
    content_type = mapped[1] if mapped else TYPES.get(path.suffix.lower(), "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=content_type,
                        charset="utf-8" if content_type in TEXTUAL else None, headers=HEADERS)


def spa_headers(request, cache="no-store"):
    """The React interface's own policy, which is the strict one plus exactly what that app needs.

    React and d3 set style ATTRIBUTES on elements, so those are allowed and inline style and script
    elements still are not (web/dist/index.html has neither). The live chart opens a socket back to
    this very server, so the socket address is this request's own host and port and nothing else.
    """
    host = request.host if HOSTNAME.fullmatch(request.host or "") else "127.0.0.1"
    return {**HEADERS, "Cache-Control": cache, "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; style-src 'self'; style-src-attr 'unsafe-inline'; "
        f"img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws://{host} wss://{host} "
        "https://api.elevenlabs.io wss://api.elevenlabs.io https://livekit.rtc.elevenlabs.io wss://livekit.rtc.elevenlabs.io; "
        "base-uri 'none'; frame-ancestors 'none'")}


async def spa_page(request):
    """web/dist/index.html exactly as the build wrote it, for the home page and every address it owns."""
    try:
        body = (DIST / "index.html").read_bytes()
    except OSError:  # nobody has built the interface yet: say so in words, not in a stack trace
        return web.Response(status=503, text=BUILD_FIRST, content_type="text/html", charset="utf-8", headers=HEADERS)
    return web.Response(body=body, content_type="text/html", charset="utf-8", headers=spa_headers(request))


async def spa_asset(request, root=None, cache=ASSET_CACHE):
    """One file out of web/dist/assets, and never one outside it. Their names carry a build hash, so they keep.

    root and cache let the catch-all hand in the top of web/dist instead, where names are not hashed.
    """
    path = web_file(request.match_info.get("name"), root or DIST / "assets")
    if path is None:
        return fail(404, "No such file.")
    content_type = TYPES.get(path.suffix.lower(), "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=content_type,
                        charset="utf-8" if content_type in TEXTUAL else None, headers=spa_headers(request, cache))


def session_of(request):
    return request.app["state"]["sessions"].get(request.match_info["id"])


async def create_session(request):
    session_id = str(uuid.uuid4())
    directory = SESSIONS / session_id
    (directory / "confirmations").mkdir(parents=True, exist_ok=True)
    runner = Runner(session_id, directory)
    runner.mcp_config()
    request.app["state"]["sessions"][session_id] = {"id": session_id, "dir": directory, "runner": runner, "busy": False}
    return web.json_response({"session_id": session_id}, headers=HEADERS)


async def body_of(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text='{"error": "Send application/json."}', content_type="application/json", headers=HEADERS)
    try:
        payload = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text='{"error": "Invalid JSON."}', content_type="application/json", headers=HEADERS)
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='{"error": "Send a JSON object."}', content_type="application/json", headers=HEADERS)
    return payload


async def stream(request, session, text):
    """One turn, one SSE response. The caller has already claimed the session; `done` always arrives."""
    response = web.StreamResponse(headers={**HEADERS, "X-Accel-Buffering": "no"})
    response.content_type, response.charset = "text/event-stream", "utf-8"
    await response.prepare(request)
    started, finished = time.monotonic(), False
    write_lock = asyncio.Lock()

    async def send(event):
        async with write_lock:
            await response.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))

    async def keep_alive():
        try:
            while True:
                await asyncio.sleep(SSE_HEARTBEAT_SECONDS)
                async with write_lock:
                    await response.write(b": keepalive\n\n")
        except GONE:
            return

    heartbeat = asyncio.create_task(keep_alive())

    try:
        async with request.app["state"]["gate"]:
            async for event in session["runner"].turn(text):
                finished = finished or event["type"] == "done"
                await send(event)
    except Exception as error:  # a broken turn must still close the stream, or the page waits forever
        print(f"chat {session['id']}: stream interrupted after {time.monotonic() - started:.1f}s ({type(error).__name__})", flush=True)
        with contextlib.suppress(*GONE):
            await send({"type": "error", "text": f"The harness failed: {error!r}"[:300]})
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, *GONE):
            await heartbeat
        session["busy"] = False
        with contextlib.suppress(*GONE):
            if not finished:
                await send({"type": "done", "duration_ms": int((time.monotonic() - started) * 1000)})
            await response.write_eof()
    return response


async def post_message(request):
    session, payload = session_of(request), await body_of(request)
    if session is None:
        return fail(404, "No such session.")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        return fail(400, "Send a message and chart context between 1 and 32000 characters.")
    purpose = payload.get("purpose")
    if purpose not in (None, "automation_proposal"):
        return fail(400, "Unknown conversation purpose.")
    if session["busy"]:
        return fail(409, "This chat is still answering the previous message.")
    proposal_mode = purpose == "automation_proposal"
    if session["runner"].started and session["runner"].proposal_mode != proposal_mode:
        return fail(409, "Start a new conversation to change its purpose.")
    session["runner"].proposal_mode = proposal_mode
    session["busy"] = True  # claimed here, with no await in between, so two messages cannot both pass
    return await stream(request, session, text.strip())


async def post_confirm(request):
    """The button, not the model, decides. Only this endpoint may write a decision (DESIGN.md §10, §11)."""
    session, payload = session_of(request), await body_of(request)
    if session is None:
        return fail(404, "No such session.")
    confirmation_id, approved = payload.get("confirmation_id"), payload.get("approved")
    if not isinstance(confirmation_id, str) or not CONFIRMATION.fullmatch(confirmation_id) or not isinstance(approved, bool):
        return fail(400, 'Send {"confirmation_id": "…", "approved": true|false}.')
    path = session["dir"] / "confirmations" / f"{confirmation_id}.json"
    if not path.exists():
        return fail(404, "That confirmation is no longer valid; ask for a new one.")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["expires_ms"] <= int(time.time() * 1000):
        return fail(410, "That confirmation expired; ask for a new one.")
    if record["decision"] is not None:
        return fail(409, "That confirmation was already decided.")
    if session["busy"]:
        return fail(409, "This chat is still answering the previous message.")
    if record.get("kind") == "brief_telegram":
        from brief_confirmation import decide_confirmation, delivery_message
        session["busy"] = True
        try:
            result = await decide_confirmation(session["dir"], confirmation_id, approved)
        finally:
            session["busy"] = False
        events = [{"type": "message", "text": delivery_message(result)}, {"type": "done"}]
        return web.Response(text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                            content_type="text/event-stream", headers={"Cache-Control": "no-store"})
    if record.get("kind") == "automation_proposal":
        from automation_tools import decide_proposal, ProposalError
        try:
            result = decide_proposal(session["dir"], confirmation_id, approved)
        except (ProposalError, ValueError) as error:
            return fail(409, str(error))
        events = [
            {"type": "card", "card": result["_card"]},
            {"type": "message", "text": "Your configuration is finalized. Set the total Jev budget in the card and press Start live tracking to open the dashboard." if approved else "The proposal remains editable. Tell me what you would like to change."},
            {"type": "done"},
        ]
        return web.Response(text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                            content_type="text/event-stream", headers={"Cache-Control": "no-store"})
    record.update(decision="approved" if approved else "declined", decided_ms=int(time.time() * 1000))
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temp.replace(path)
    session["busy"] = True
    button = "Confirm" if approved else "Cancel"
    return await stream(request, session, f"The user pressed the {button} button for confirmation {confirmation_id}.")


async def lifecycle(app):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    app["state"]["gate"] = asyncio.Semaphore(MAX_TURNS)
    try:
        yield
    finally:
        for session in app["state"]["sessions"].values():
            close = getattr(session["runner"], "aclose", None)
            if close:
                await close()


def load_plugins(app, names=PLUGINS):
    """Optional modules, each registering its own /api routes in setup(app).

    A module that is simply not there is the normal case and says nothing. A module that IS there and
    fails prints its traceback and is skipped: one broken stream of work must never cost the server.
    """
    loaded = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except Exception:  # noqa: BLE001  (a package that cannot even be looked at)
            traceback.print_exc()
            continue
        try:
            setup = getattr(importlib.import_module(name), "setup", None)
            if setup is None:
                print(f"bridge: {name}.py has no setup(app), so it was skipped", flush=True)
                continue
            setup(app)
        except Exception:  # noqa: BLE001
            print(f"bridge: {name}.py could not be loaded, the server runs without it", flush=True)
            traceback.print_exc()
            continue
        loaded.append(name)
        print(f"bridge: loaded {name}.py", flush=True)
    return loaded


def attach(app):
    """The chat API on any aiohttp application, so signal_server.py can serve it beside Morning Brief."""
    app["state"] = {"sessions": {}, "gate": None, **(app.get("state") or {})}  # mutated in place; aiohttp freezes the app mapping once it starts
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.post("/api/sessions", create_session),
        web.post("/api/sessions/{id}/messages", post_message),
        web.post("/api/sessions/{id}/confirm", post_confirm),
    ])
    app["plugins"] = load_plugins(app)
    return app


app = attach(web.Application(middlewares=[local_only], client_max_size=64 * 1024))
app.add_routes([
    web.get("/", asset), *[web.get(path, asset) for path in ASSETS], web.get("/{name:.*}", asset),  # the catch-all is last, or it shadows ASSETS
])

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("PORT", 5195)))
