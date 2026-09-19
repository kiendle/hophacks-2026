# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/bridge.py   then open http://127.0.0.1:5195

The website's chat widget talks only to this process. Each message spawns the local Claude Code
(claude_runner.py), whose only tools are our MCP server's (demo_mcp_server.py), and its answer is
streamed back as server-sent events. The Confirm button posts here, not to the model: the harness
writes the confirmation record, and `submit_project` checks it — see harness/DESIGN.md §0 and §10.

mcp and duckdb are declared above although this file does not import them: the MCP server runs on
this interpreter (claude_runner.mcp_config), so uv must install them into it.
"""
import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

from claude_runner import Runner

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SESSIONS = ROOT / "state/sessions"
ASSETS = {"/signal.css": (ROOT.parent / "jetstream-demo/styles.css", "text/css")}  # one visual system for every demo
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json", ".woff2": "font/woff2"}
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
PLACEHOLDER = b"<!doctype html><title>Signal harness</title><p>The chat page has not been written yet. The API is live.\n"
CONFIRMATION = re.compile(r"[A-Za-z0-9_-]{16}")
GONE = (ConnectionResetError, RuntimeError, OSError)  # the page may be gone before the last frame is written
MAX_TURNS = 2  # at most two Claude Code processes at once: one laptop, one subscription (DESIGN.md §0)


def fail(status, message):
    return web.json_response({"error": message}, status=status, headers=HEADERS)


@web.middleware
async def local_only(request, handler):
    if request.path.startswith("/api/") and request.method != "GET":
        if request.url.host not in ("127.0.0.1", "localhost") or request.headers.get("Origin", str(request.url.origin())) != str(request.url.origin()):
            return fail(403, "Only same-origin local requests are accepted.")
    return await handler(request)


async def asset(request):
    """Read per request, so the front end can be edited without restarting the server."""
    name = request.match_info.get("name") or "index.html"
    path, content_type = ASSETS.get(request.path) or (WEB / name, TYPES.get(Path(name).suffix, "application/octet-stream"))
    if "/" in name or "\\" in name or name.startswith("."):
        return fail(404, "No such file.")
    if not path.exists():
        if request.path == "/":
            return web.Response(body=PLACEHOLDER, content_type="text/html", charset="utf-8", headers=HEADERS)
        return fail(404, "No such file.")
    return web.Response(body=path.read_bytes(), content_type=content_type, charset="utf-8", headers=HEADERS)


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

    async def send(event):
        await response.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))

    try:
        async with request.app["state"]["gate"]:
            async for event in session["runner"].turn(text):
                finished = finished or event["type"] == "done"
                await send(event)
    except Exception as error:  # a broken turn must still close the stream, or the page waits forever
        with contextlib.suppress(*GONE):
            await send({"type": "error", "text": f"The harness failed: {error!r}"[:300]})
    finally:
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
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return fail(400, "Send a message between 1 and 4000 characters.")
    if session["busy"]:
        return fail(409, "This chat is still answering the previous message.")
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
    yield


def attach(app):
    """The chat API on any aiohttp application, so signal_server.py can serve it beside Morning Brief."""
    app["state"] = {"sessions": {}, "gate": None, **(app.get("state") or {})}  # mutated in place; aiohttp freezes the app mapping once it starts
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.post("/api/sessions", create_session),
        web.post("/api/sessions/{id}/messages", post_message),
        web.post("/api/sessions/{id}/confirm", post_confirm),
    ])
    return app


app = attach(web.Application(middlewares=[local_only], client_max_size=64 * 1024))
app.add_routes([
    web.get("/", asset), *[web.get(path, asset) for path in ASSETS], web.get("/{name}", asset),  # the catch-all is last, or it shadows ASSETS
])

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("PORT", 5195)))
