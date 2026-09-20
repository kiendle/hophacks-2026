# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "anthropic>=0.75", "imageio-ffmpeg>=0.6"]
# ///
"""Run: uv run morning-brief/server.py   then open http://127.0.0.1:5194

Morning Brief follows your interests on Bluesky around the clock and turns the
last few hours into a short podcast: collector.py gathers, briefing.py writes and
voices.  Keys go in morning-brief/.env (see .env.example); both are optional.
"""
import asyncio
import json
import os
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else []:
    key, separator, value = line.partition("=")
    if separator and value.strip() and not key.lstrip().startswith("#"):
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

from briefing import APPVIEW, DEFAULT_VOICE, MAX_SECONDS, MIN_SECONDS, BriefError, build, expand_interest, list_voices, now_ms, warm  # noqa: E402  (reads the environment loaded above)
from collector import Collector, Store  # noqa: E402

DATA = ROOT / "data"
BRIEFS = DATA / "briefs"
ASSETS = {
    "/": (ROOT / "web/index.html", "text/html"),
    "/app.js": (ROOT / "web/app.js", "text/javascript"),
    "/brief.css": (ROOT / "web/brief.css", "text/css"),
    "/signal.css": (ROOT.parent / "jetstream-demo/styles.css", "text/css"),  # one visual system for both demos
}
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; media-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
DEFAULT_HOURS = float(os.environ.get("BRIEF_HOURS", 8))
DEFAULT_SECONDS = 90
LANGS = [lang for lang in os.environ.get("BRIEF_LANGS", "en").split(",") if lang]
SEED = {"id": "ai", "name": "AI", "langs": LANGS, "created": 0, "covered_from": None, "terms": [
    "AI", "A.I.", "AGI", "LLM", "LLMs", "GenAI", "OpenAI", "Anthropic", "ChatGPT", "DeepMind", "Nvidia", "Sam Altman",
    "artificial intelligence", "machine learning", "generative AI", "large language model", "large language models", "AI safety",
]}


def fail(status, message):
    return web.json_response({"error": message}, status=status, headers=HEADERS)


@web.middleware
async def local_only(request, handler):
    if request.path.startswith("/api/") and request.method != "GET":
        if request.url.host not in ("127.0.0.1", "localhost") or request.headers.get("Origin", str(request.url.origin())) != str(request.url.origin()):
            return fail(403, "Only same-origin local requests are accepted.")
    return await handler(request)


async def asset(request):
    path, content_type = ASSETS[request.path]
    return web.Response(body=path.read_bytes(), content_type=content_type, charset="utf-8", headers=HEADERS)


async def status(request):
    app = request.app
    return web.json_response({
        **app["collector"].status(), "default_hours": DEFAULT_HOURS, "default_seconds": DEFAULT_SECONDS, "min_seconds": MIN_SECONDS, "max_seconds": MAX_SECONDS, "brief_at": os.environ.get("BRIEF_AT"),
        "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")), "engagement_host": urlsplit(APPVIEW).hostname,
        "working": app["state"]["working"]["id"] if app["state"]["working"] and app["state"]["working"]["status"] == "working" else None,
    }, headers=HEADERS)


async def collecting(request):
    try:
        action = (await request.json()).get("action")
    except ValueError:
        return fail(400, "Invalid JSON.")
    if action not in ("stop", "start"):
        return fail(400, 'Send {"action": "stop"} or {"action": "start"}.')
    collector = request.app["collector"]
    async with request.app["state"]["lock"]:  # a double-click must not pause and resume at the same time
        await (collector.pause() if action == "stop" else collector.resume())
    return await status(request)


async def add_interest(request):
    try:
        query = (await request.json()).get("query")
    except ValueError:
        return fail(400, "Invalid JSON.")
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        return fail(400, "Describe the interest in up to 300 characters.")
    collector = request.app["collector"]
    if len(collector.store.interests) >= 12:
        return fail(409, "Twelve interests is the limit; remove one first.")
    name, terms, note = await expand_interest(query.strip())
    return web.json_response({"interest": collector.add_interest(name, terms, LANGS), "note": note}, headers=HEADERS)


async def remove_interest(request):
    request.app["collector"].remove_interest(request.match_info["id"])
    return web.json_response({}, headers=HEADERS)


async def voices(request):
    found = await list_voices(request.app["http"], request.app["state"])
    default = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE)
    return web.json_response({"default": default, "voices": [{**voice, "preview_url": None, "preview": bool(voice["preview_url"])} for voice in found]}, headers=HEADERS)


async def voice_preview(request):
    """Preview clips live on ElevenLabs' storage; relaying them keeps the page's media-src at 'self'."""
    found = {voice["id"]: voice for voice in await list_voices(request.app["http"], request.app["state"])}
    voice = found.get(request.match_info["id"])
    if not voice or not voice["preview_url"]:
        return fail(404, "No preview for this voice.")
    async with request.app["http"].get(voice["preview_url"]) as response:
        if response.status != 200:
            return fail(502, "The preview could not be fetched.")
        return web.Response(body=await response.read(), content_type="audio/mpeg", headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"})


def summary(brief):
    return {key: brief.get(key) for key in ("id", "created", "hours", "status", "title")} | {"audio": bool(brief.get("audio"))}


def saved_briefs():
    for path in sorted(BRIEFS.glob("*/brief.json"), reverse=True):
        yield json.loads(path.read_text(encoding="utf-8"))


async def list_briefs(request):
    working = request.app["state"]["working"]
    rows = ([summary(working)] if working and working["status"] != "ready" else []) + [summary(brief) for brief in saved_briefs()]
    return web.json_response({"briefs": rows[:30]}, headers=HEADERS)


async def get_brief(request):
    working, brief_id = request.app["state"]["working"], request.match_info["id"]
    if working and working["id"] == brief_id:
        return web.json_response(working, headers=HEADERS)
    path = BRIEFS / brief_id / "brief.json"
    if not re.fullmatch(r"\d{8}-\d{6}", brief_id) or not path.exists():
        return fail(404, "No such brief.")
    return web.Response(body=path.read_bytes(), content_type="application/json", headers=HEADERS)


async def audio(request):
    brief_id, name = request.match_info["id"], request.match_info["file"]
    path = BRIEFS / brief_id / name
    if not re.fullmatch(r"\d{8}-\d{6}", brief_id) or not re.fullmatch(r"[a-z0-9-]+\.mp3", name) or not path.exists():
        return fail(404, "No such audio.")
    return web.FileResponse(path, headers={"Content-Type": "audio/mpeg", "X-Content-Type-Options": "nosniff"})


def start_brief(app, hours, interest_ids, seconds=DEFAULT_SECONDS, voice=None):
    seconds = min(seconds, MAX_SECONDS)
    created = datetime.now()
    brief = {"id": f"{created:%Y%m%d-%H%M%S}", "created": now_ms(), "hours": hours, "seconds": int(seconds), "voice": voice or {"id": os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE), "name": None}, "interest_ids": interest_ids, "status": "working", "step": "Starting", "notes": [], "audio": None, "usage": {}}
    app["state"]["working"] = brief

    async def run():
        directory = BRIEFS / brief["id"]
        directory.mkdir(parents=True, exist_ok=True)
        try:
            await build(app["collector"].store, app["http"], app["views"], brief, directory)
        except BriefError as error:
            brief.update(status="failed", step=str(error))
        except Exception as error:
            traceback.print_exc()
            brief.update(status="failed", step=f"Unexpected error: {error!r}")
        if brief["status"] == "failed" and not any(directory.iterdir()):
            directory.rmdir()

    app["state"]["task"] = asyncio.create_task(run())
    return brief


async def create_brief(request):
    app = request.app
    try:
        body = await request.json()
    except ValueError:
        return fail(400, "Invalid JSON.")
    hours = body.get("hours", DEFAULT_HOURS)
    seconds = body.get("seconds", DEFAULT_SECONDS)
    known = [interest["id"] for interest in app["collector"].store.interests]
    chosen = [interest_id for interest_id in body.get("interests") or known if interest_id in known]
    if not isinstance(hours, (int, float)) or not 1 <= hours <= 48:
        return fail(400, "Choose a window between 1 and 48 hours.")
    if not isinstance(seconds, (int, float)) or not MIN_SECONDS <= seconds <= MAX_SECONDS:
        return fail(400, f"Choose a spoken length between {MIN_SECONDS} and {MAX_SECONDS} seconds.")
    found = {voice["id"]: voice for voice in await list_voices(app["http"], app["state"])}
    if body.get("voice") is not None and body["voice"] not in found:
        return fail(400, "Choose one of the listed voices.")
    voice = found.get(body.get("voice"))
    if not chosen:
        return fail(400, "Follow at least one interest first.")
    if app["state"]["working"] and app["state"]["working"]["status"] == "working":
        return fail(409, "A brief is already being made.")
    return web.json_response({"id": start_brief(app, hours, chosen, seconds, voice and {"id": voice["id"], "name": voice["name"]})["id"]}, headers=HEADERS)


async def every_morning(app):
    """BRIEF_AT=07:30 has the brief written and recorded before the listener wakes up."""
    hour, minute = map(int, os.environ["BRIEF_AT"].split(":"))
    while True:
        now = datetime.now()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        await asyncio.sleep(((due if due > now else due + timedelta(days=1)) - now).total_seconds())
        interests = [interest["id"] for interest in app["collector"].store.interests]
        if interests and not (app["state"]["working"] and app["state"]["working"]["status"] == "working"):
            start_brief(app, DEFAULT_HOURS, interests)


async def keep_warm(app):
    """Hydrate ahead of the listener, so pressing Make my brief starts at the writing step. VIEW_TTL_MS is 20 minutes."""
    while True:
        await asyncio.sleep(300)
        if app["collector"].store.interests and not app["collector"].paused and not (app["state"]["working"] and app["state"]["working"]["status"] == "working"):
            await warm(app["collector"].store, app["http"], app["views"], DEFAULT_HOURS)


async def lifecycle(app):
    BRIEFS.mkdir(parents=True, exist_ok=True)
    store = Store(DATA, retain_hours=float(os.environ.get("RETAIN_HOURS", 36)))
    first_run = not (DATA / "interests.json").exists()
    store.load()
    if first_run:
        store.interests.append(dict(SEED))
        store.save_interests()
    app.update(collector=Collector(store, backfill_hours=float(os.environ.get("BACKFILL_HOURS", DEFAULT_HOURS))), views={},
               http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)))
    await app["collector"].start()
    morning = asyncio.create_task(every_morning(app)) if os.environ.get("BRIEF_AT") else None
    warming = asyncio.create_task(keep_warm(app))
    yield
    if morning:
        morning.cancel()
    warming.cancel()
    await app["collector"].stop()
    await app["http"].close()


app = web.Application(middlewares=[local_only], client_max_size=32 * 1024)
app["state"] = {"working": None, "task": None, "lock": asyncio.Lock()}  # mutated in place; aiohttp freezes the app mapping once it starts
app.cleanup_ctx.append(lifecycle)
app.add_routes([web.get(path, asset) for path in ASSETS] + [
    web.get("/api/status", status), web.post("/api/collector", collecting), web.post("/api/interests", add_interest), web.delete("/api/interests/{id}", remove_interest),
    web.get("/api/briefs", list_briefs), web.post("/api/briefs", create_brief), web.get("/api/briefs/{id}", get_brief),
    web.get("/api/briefs/{id}/audio/{file}", audio), web.get("/api/voices", voices), web.get("/api/voices/{id}/preview", voice_preview),
])

if __name__ == "__main__":
    import socket

    port = int(os.environ.get("PORT", 5194))
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"127.0.0.1:{port} is already in use, so Morning Brief is probably already collecting; stop that one first.")
    DATA.mkdir(parents=True, exist_ok=True)  # the port guards one port; this guards the folder, whatever port the other process listens on
    lock = os.open(DATA / "collector.lock", os.O_RDWR | os.O_CREAT)  # the OS drops the lock when the process dies, so a crash never wedges the folder
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # two collectors appending to one posts.jsonl interleave their writes and corrupt it
        raise SystemExit(f"Another Morning Brief or Signal process is already collecting into {DATA}; stop that one first, whatever port it listens on.")
    web.run_app(app, host="127.0.0.1", port=port)
