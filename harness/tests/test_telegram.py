# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_telegram.py

No Telegram and no model anywhere in here: a fake Bot API server on a free local port records every
request and can be scripted to answer 429 / 400 / 401 / 409, and a stub runner replays event streams.
Virtual time runs 100x (Clock), so the 1.2 s edit gate and the retry_after wait are asserted from
timestamps taken on the client side, in milliseconds of real time.
"""
import asyncio
import contextlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web

HARNESS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS))
os.environ.setdefault("HARNESS_REPO", str(HARNESS.parent))

import telegram_bot as tg  # noqa: E402

TOKEN = "7777777777:AAFakeTokenForTestsOnly-0000000000a"  # never a real one, and it must never reach the output
OUTCOMES = []
CHAT, OTHER, STRANGER = 4242424242, 555000111, 909090
SENTENCE = "The user pressed the {button} button for confirmation {confirmation_id}."  # bridge.py's own wording


class Tee:
    """Everything printed is kept, so the last check can prove the token never appeared anywhere.

    The console gets it folded into its own encoding first: Git Bash here is cp1252, details carry block
    characters from the preview bars, and a test run must not die of the output it prints about itself."""

    def __init__(self, stream):
        self.stream, self.text = stream, []
        self.encoding = getattr(stream, "encoding", None) or "ascii"

    def write(self, data):
        self.text.append(data)
        return self.stream.write(data.encode(self.encoding, "replace").decode(self.encoding, "replace"))

    def flush(self):
        self.stream.flush()


sys.stdout, sys.stderr = Tee(sys.stdout), Tee(sys.stderr)


def captured():
    return "".join(sys.stdout.text + sys.stderr.text)


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)


class Clock:
    """Virtual time, 100x faster than the wall clock: a 1.2 s gate becomes a provable 12 ms."""

    SCALE = 0.01

    def __init__(self):
        self.base, self.slept = time.monotonic(), []

    def now(self):
        return (time.monotonic() - self.base) / self.SCALE

    async def sleep(self, seconds):
        self.slept.append(round(float(seconds), 3))
        await asyncio.sleep(max(0.0, float(seconds)) * self.SCALE)


class StubRunner:
    """The injected runner: one turn of scripted events, with a real delay so the status can be edited."""

    def __init__(self, script, delay):
        self.script, self.delay, self.said = list(script), delay, []

    async def turn(self, text):
        self.said.append(text)
        for event in self.script:
            await asyncio.sleep(self.delay)
            yield event


class Runners:
    def __init__(self, script=(), delay=0.0):
        self.script, self.delay, self.made = list(script), delay, []

    def __call__(self, session_id, directory):
        runner = StubRunner(self.script, self.delay)
        runner.session_id, runner.directory = session_id, Path(directory)
        self.made.append(runner)
        return runner

    def turns(self):
        return sum(len(runner.said) for runner in self.made)


class Fake:
    """The Bot API, as much of it as the bot uses. A scripted entry of None means answer normally."""

    def __init__(self, clock):
        self.clock, self.requests, self.queue, self.scripted, self.next_id = clock, [], [], {}, 100

    def script(self, method, *answers):
        self.scripted.setdefault(method, []).extend(answers)

    def got(self, method):
        return [row for row in self.requests if row["method"] == method]

    async def handle(self, request):
        method, payload = request.match_info["method"], {}
        if request.content_type.startswith("multipart/"):  # sendAudio: the fields as sent, the file as its name, type and size
            for key, value in (await request.post()).items():
                payload[key] = value if isinstance(value, str) else {
                    "filename": value.filename, "content_type": value.content_type, "bytes": len(value.file.read())}
        else:
            with contextlib.suppress(ValueError):
                payload = await request.json()
        payload = payload if isinstance(payload, dict) else {}
        self.requests.append({"method": method, "at": self.clock.now(), **payload})
        queued = self.scripted.get(method) or []
        answer = queued.pop(0) if queued else None
        if answer is not None:
            status, body = answer
            return web.json_response(body, status=status)
        if method == "getMe":
            return web.json_response({"ok": True, "result": {"id": 1, "is_bot": True, "username": "signal_test_bot"}})
        if method == "getUpdates":
            batch = [update for update in self.queue if update["update_id"] >= (payload.get("offset") or 0)]
            if not batch:
                await asyncio.sleep(0.02)  # the long poll, shortened
            return web.json_response({"ok": True, "result": batch})
        if method in ("sendMessage", "editMessageText"):
            self.next_id += 1
            return web.json_response({"ok": True, "result": {
                "message_id": payload.get("message_id") or self.next_id,
                "chat": {"id": payload.get("chat_id")}, "text": payload.get("text")}})
        return web.json_response({"ok": True, "result": True})


MP3 = b"ID3" + bytes(range(256)) * 8  # never played, only carried
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # the same: a chart's picture is passed through, never read
BRIEF = "20260919-070001"
READY = {"id": BRIEF, "status": "ready", "step": "", "title": "AI labs trade blows", "estimated_seconds": 171,
         "audio": {"full": "brief.mp3", "voice": "Rachel", "characters": 2400},
         "segments": [{"topic": "AI", "headline": "A loud day", "script": "Good morning. Here is what happened overnight.",
                       "stories": [{"title": "A new model lands"}, {"title": "It's a \"<big>\" & bold claim"}]}]}


class Briefs:
    """The Morning Brief server, as much of it as the bot uses: order a brief, follow it, fetch the recording."""

    def __init__(self, busy=0, working=2, final=None):
        self.busy, self.working, self.final = busy, working, final or READY
        self.orders, self.polls, self.fetched = [], 0, 0

    async def create(self, request):
        self.orders.append({"origin": request.headers.get("Origin"), **(await request.json())})
        if self.busy > 0:
            self.busy -= 1
            return web.json_response({"error": "A brief is already being made."}, status=409)
        return web.json_response({"id": BRIEF})

    async def get(self, request):
        self.polls += 1
        if self.polls <= self.working:
            return web.json_response({"id": BRIEF, "status": "working", "step": "Claude is choosing the stories"})
        return web.json_response(self.final)

    async def audio(self, request):
        self.fetched += 1
        return web.Response(body=MP3, content_type="audio/mpeg")


class Charts:
    """The chat server's own API, as much of it as a chart card needs: one picture, fetched by path.

    `anything` is every other path on that host. Nothing on this front end may ever ask for one, so
    what it records is the proof: a card that tries to leave /api/ leaves its footprint here.
    """

    def __init__(self, missing=False, hangs=0.0, copies=1):
        self.missing, self.hangs, self.copies = missing, hangs, copies
        self.asked, self.elsewhere = [], []

    async def picture(self, request):
        self.asked.append(request.path)
        if self.hangs:
            await asyncio.sleep(self.hangs)
        if self.missing:
            return web.json_response({"error": "No such chart."}, status=404)
        return web.Response(body=PNG * self.copies, content_type="image/png")

    async def anything(self, request):
        self.elsewhere.append(request.path)
        return web.Response(body=b"a session file, or whatever else this laptop serves")


def message(update_id, chat_id, text, kind="private"):
    return {"update_id": update_id, "message": {"message_id": update_id, "date": 0,
                                                "chat": {"id": chat_id, "type": kind}, "text": text}}


def callback(update_id, chat_id, data, message_id, text="Ready to start\n20,000 posts"):
    return {"update_id": update_id, "callback_query": {"id": f"q{update_id}", "data": data, "message": {
        "message_id": message_id, "chat": {"id": chat_id, "type": "private"}, "text": text}}}


async def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def polled(box, times=2):
    """Wait until the bot has been back to getUpdates, which proves the batch before it was handled."""
    target = len(box.fake.got("getUpdates")) + times
    return await until(lambda: len(box.fake.got("getUpdates")) >= target)


async def served(box, chat_id=CHAT, turns=1, timeout=10.0):
    """Wait for that many turns to have been started and for none of them to be running any more."""
    await until(lambda: box.runners.turns() >= turns, timeout)
    return await until(lambda: chat_id in box.bot.chats and box.bot.chats[chat_id]["busy"] is False, timeout)


@contextlib.asynccontextmanager
async def running(allowed, script=(), delay=0.0, start=True, scripted=(), wall=None, plans=None, briefs=None, brief_base=None,
                  charts=None, http_layer=None):
    clock, briefs, charts = Clock(), briefs or Briefs(), charts or Charts()
    fake = Fake(clock)
    for method, *answers in scripted:
        fake.script(method, *answers)
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", fake.handle)
    app.router.add_post("/api/briefs", briefs.create)  # one port plays both Telegram and the Morning Brief server
    app.router.add_get("/api/briefs/{id}", briefs.get)
    app.router.add_get("/api/briefs/{id}/audio/{file}", briefs.audio)
    app.router.add_get("/api/charts/{file}", charts.picture)
    app.router.add_get("/{tail:.*}", charts.anything)  # last, so it only ever sees what the routes above did not
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{server.addresses[0][1]}"
    root, runners, calls = Path(tempfile.mkdtemp(prefix="tg-test-")), Runners(script, delay), []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
        bot = tg.Bot(TOKEN, allowed, http_layer(http) if http_layer else http, base=base, runner_factory=runners,
                     sleep=clock.sleep, now=clock.now, sessions=root / "sessions", chats_path=root / "telegram/chats.json",
                     clock=wall or datetime.now, brief_base=brief_base or base)
        bot.plans.update(plans or {})
        original = bot.api.call

        async def traced(method, **payload):  # client-side timestamps: no network jitter in the timing checks
            calls.append({"method": method, "at": clock.now(), **payload})
            return await original(method, **payload)

        bot.api.call = traced
        box = types.SimpleNamespace(bot=bot, fake=fake, runners=runners, clock=clock, root=root, task=None, briefs=briefs,
                                    base=base, charts=charts,
                                    sent=lambda method: [row for row in calls if row["method"] == method])
        box.task = asyncio.create_task(bot.run()) if start else None
        try:
            if start:
                await until(lambda: fake.got("getUpdates"))
            yield box
        finally:
            if box.task:
                box.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await box.task
            for task in list(bot.tasks):
                task.cancel()
            await asyncio.sleep(0.05)
            await server.cleanup()
    shutil.rmtree(root, ignore_errors=True)


# ---- one served turn, rendered ---------------------------------------------------------------------

# The real thing this product is about: AI. Every title, outcome and note below is written the way
# steps.py writes it, and the model's own words arrive with the punctuation a model actually types.
SCRIPT = [
    {"type": "progress", "text": "Counting the posts that match…"},
    {"type": "step", "phase": "start", "id": "t1", "n": 1, "tool": "preview_keywords",
     "title": 'Searching X/Twitter for "anthropic" and "resignation", from Sep 9 to Sep 11',
     "why": "to see how much there is before drafting",
     "detail": {"tool": "mcp__harness__preview_keywords",
                "input": {"reason": "to see how much there is before drafting",
                          "keywords": ["anthropic", "resignation"], "date_from": "2026-09-09", "date_to": "2026-09-12",
                          "note": "the model's own em dash — kept verbatim in the code block"}},
     "facts": [{"label": "Words searched", "value": '"anthropic", "resignation"'}, {"label": "Dates", "value": "Sep 9 to Sep 11"}]},
    {"type": "step", "phase": "end", "id": "t1", "ok": True, "ms": 3100,
     "outcome": "Found 7,221 posts. Most were on Sep 9 (4,821)."},
    {"type": "preview", "title": "What we found on X/Twitter", "total": 7221, "exact": True, "seconds": 4,
     "per_day": [{"day": "2026-09-09", "count": 4821}, {"day": "2026-09-10", "count": 2400}, {"day": "2026-09-11", "count": 0}],
     "examples": [{"body": "the whole safety team walked out today", "like_count": 1203, "day": "2026-09-09",
                   "url": "https://x.com/i/status/1"}],
     "note": "82% of matches are one giveaway template"},
    {"type": "step", "phase": "start", "id": "t2", "n": 2, "tool": "save_draft", "title": "Saving your project",
     "detail": {"tool": "mcp__harness__save_draft", "input": {"spec_json": '{"name": "AI resignation backlash"}'}}},
    {"type": "spec", "spec": {"name": "AI resignation backlash"}, "spec_hash": "abcdef1234567890" + "0" * 48},
    {"type": "step", "phase": "end", "id": "t2", "ok": True},
    {"type": "step", "phase": "start", "id": "t3", "n": 3, "title": "Checking the numbers"},
    {"type": "wat", "text": "an event type from a later version"},
    "not even a dict",
    {"type": "step"},
    {"type": "step", "phase": "end", "id": "t3", "ok": True, "outcome": "Done."},
    {"type": "step", "phase": "start", "id": "t4", "n": 4, "tool": "bluesky_recent",
     "title": "Searching the last 15 minutes of Bluesky", "why": "to see what is being said right now",
     "detail": {"tool": "mcp__harness__bluesky_recent", "input": {"keywords": ["AI"], "minutes": 15}}},
    {"type": "step", "phase": "end", "id": "t4", "ok": False, "ms": 1200,
     "outcome": "That did not work: Bluesky did not answer."},
    {"type": "delta", "text": "partial text, not for Telegram"},
    {"type": "message", "text": "Here is what I found. **7,221 posts**."},
    {"type": "done", "duration_ms": 28000},
]
QUESTION = "how did people react to the anthropic resignation post?"


async def serving_turn():
    async with running({CHAT}, SCRIPT, delay=0.05) as box:
        box.fake.queue.append(message(1, CHAT, QUESTION))
        await until(lambda: box.sent("editMessageText") and box.sent("editMessageText")[-1]["text"].endswith("28 seconds"))
        await asyncio.sleep(0.1)
        sends, edits, typing = box.sent("sendMessage"), box.sent("editMessageText"), box.sent("sendChatAction")
        bodies = [row["text"] for row in edits]
        gaps = [round(second["at"] - first["at"], 2) for first, second in zip(edits, edits[1:])]
        answer = [row for row in sends if "7,221 posts</b>" in (row["text"] or "")]
        preview = [row for row in sends if "<pre>" in (row["text"] or "")]
        rows = [line for body in bodies for line in body.splitlines()]

        check("typing is sent and the status starts as one message",
              typing and all(row["action"] == "typing" for row in typing) and sends and sends[0]["text"].startswith("Thinking"),
              f"{len(typing)} chat actions, first message {sends[0]['text'][:20]!r}")
        check("status edits are at least 1.2 s apart and far fewer than the events",
              len(edits) >= 3 and min(gaps) >= 1.2 and len(edits) < len(SCRIPT),
              f"{len(edits)} edits for {len(SCRIPT)} events, gaps {gaps} virtual s")
        check("a running step is marked with an hourglass", any(tg.HOURGLASS in body for body in bodies),
              f"{sum(tg.HOURGLASS in body for body in bodies)} of {len(bodies)} edits showed one")
        done = next((body for body in bodies if f"{tg.OK} Saving your project" in body), "")
        check("a finished step is one line: a check mark and the title, nothing else",
              bool(done) and f"{tg.OK} Searching X/Twitter for &quot;anthropic&quot;" in done
              and all(line.startswith((tg.OK, tg.HOURGLASS, tg.FAILED, "   ")) for line in done.splitlines()),
              repr(done.splitlines()[:2]) if done else "step 2 never rendered")
        check("the status carries no why, no result, no draft and no duration of its own",
              not any("Why:" in row or "Took" in row or "Draft" in row or "version abcdef12" in row for row in rows)
              and not any("7,221 posts." in row or "(3.1" in row or " s)" in row for row in rows),
              repr(next((row for row in rows if "Why" in row or "Draft" in row or "Took" in row), "none of them")))
        failed = next((body for body in bodies if tg.FAILED in body), "")
        check("a step that did not work shows one short result line under its title",
              bool(failed) and f"{tg.FAILED} Searching the last 15 minutes of Bluesky" in failed
              and "   That did not work: Bluesky did not answer." in failed,
              repr(failed.splitlines()[-2:]) if failed else "the failed step never rendered")
        check("the preview is its own message, with dates in words, scaled bars and thousands separators",
              len(preview) == 1 and tg.BLOCK * 12 in preview[0]["text"] and "4,821" in preview[0]["text"]
              and "2,400" in preview[0]["text"] and "7,221 posts" in preview[0]["text"] and "Sep 9" in preview[0]["text"]
              and "2026-09-09" not in preview[0]["text"] and "1,203 likes" in preview[0]["text"]
              and "giveaway template" in preview[0]["text"] and "https://x.com/i/status/1" in preview[0]["text"],
              repr(next((line for line in preview[0]["text"].splitlines() if tg.BLOCK in line), "")) if preview else "no preview message")
        check("the answer arrives as a new message with **bold** rendered as <b>",
              len(answer) == 1 and answer[0]["parse_mode"] == "HTML" and "Here is what I found." in answer[0]["text"],
              f"{len(answer)} answer message(s)")
        check("unknown event types, deltas and junk are ignored without a crash",
              not any("wat" in (row["text"] or "") or "partial text" in (row["text"] or "") for row in sends + edits)
              and len(box.runners.made) == 1 and box.runners.turns() == 1,
              f"{len(sends)} messages, {len(edits)} edits, {box.runners.turns()} turn")
        check("the status message collapses into one summary line in the page's own words",
              bodies and bodies[-1] == "4 steps, 1 did not finish, 28 seconds", repr(bodies[-1] if bodies else None))
        check("the session directory is the layout the web bridge uses",
              (box.root / "sessions" / box.runners.made[0].session_id / "confirmations").is_dir()
              and 'SESSIONS = ROOT / "state/sessions"' in (HARNESS / "bridge.py").read_text(encoding="utf-8"),
              f"sessions/{box.runners.made[0].session_id[:8]}.../confirmations")
        saved = json.loads((box.root / "telegram/chats.json").read_text(encoding="utf-8"))
        check("the chat is mapped to its session in chats.json",
              saved["chats"][str(CHAT)]["session_id"] == box.runners.made[0].session_id, json.dumps(saved)[:78])

        state, before = box.bot.chats[CHAT], len(edits)
        for index in range(3):
            await box.bot.edit(state, 4242, f"burst {index}")
        burst = [row["at"] for row in box.sent("editMessageText")[before:]]
        check("three edits fired back to back are still one per 1.2 s",
              len(burst) == 3 and all(second - first >= 1.2 for first, second in zip(burst, burst[1:])),
              f"gaps {[round(b - a, 2) for a, b in zip(burst, burst[1:])]} virtual s")


async def progress_fallback():
    script = [{"type": "progress", "text": "Scanning the last minutes of Bluesky…"},
              {"type": "message", "text": "nothing yet"}, {"type": "done", "duration_ms": 1000}]
    async with running({CHAT}, script, delay=0.08) as box:
        box.fake.queue.append(message(1, CHAT, "what is happening on bluesky?"))
        await until(lambda: box.sent("editMessageText") and box.sent("editMessageText")[-1]["text"].startswith("Done"))
        bodies = [row["text"] for row in box.sent("editMessageText")]
        check("with no step events the status falls back to the progress line",
              "Scanning the last minutes of Bluesky…" in bodies, repr(bodies[:2]))
        check("a turn with no steps summarises as Done and its length in words",
              bodies[-1] == "Done, 1 second", repr(bodies[-1]))


async def long_answer():
    body = "\n\n".join(f"Paragraph {index:02d}. " + "x" * 240 for index in range(36))
    async with running({CHAT}, [{"type": "message", "text": body}, {"type": "done", "duration_ms": 1000}]) as box:
        box.fake.queue.append(message(1, CHAT, "tell me everything"))
        await until(lambda: len([row for row in box.sent("sendMessage") if "Paragraph" in (row["text"] or "")]) >= 3)
        await asyncio.sleep(0.1)
        parts = [row["text"] for row in box.sent("sendMessage") if "Paragraph" in row["text"]]
        check(f"a {len(body):,}-character answer is split under 4096 on paragraph boundaries",
              len(parts) >= 3 and max(len(part) for part in parts) <= 4096
              and all(part.startswith("Paragraph ") and part.endswith("x") for part in parts)
              and "".join(part.replace("\n", "") for part in parts) == body.replace("\n", ""),
              f"{len(parts)} messages of {[len(part) for part in parts]} characters")


async def escaping_and_plain_fallback():
    nasty = '<script>alert(1)</script> & <a href="http://evil/x">click</a> <b>unclosed **really**'
    async with running({CHAT}, [{"type": "message", "text": nasty}, {"type": "done", "duration_ms": 1}],
                       scripted=[("sendMessage", None, (400, {"ok": False, "error_code": 400,
                                                              "description": "Bad Request: can't parse entities: Unsupported start tag"}))]) as box:
        box.fake.queue.append(message(1, CHAT, "say something dangerous"))
        await until(lambda: len([row for row in box.sent("sendMessage") if "alert(1)" in (row["text"] or "")]) >= 2)
        tries = [row for row in box.sent("sendMessage") if "alert(1)" in row["text"]]
        first, second = tries[0], tries[-1]
        check("untrusted text is escaped and only **bold** becomes a tag",
              first["parse_mode"] == "HTML" and "&lt;script&gt;" in first["text"] and "&lt;a href=" in first["text"]
              and " &amp; " in first["text"] and "&lt;b&gt;unclosed" in first["text"] and "<b>really</b>" in first["text"]
              and "<script" not in first["text"] and 'href="http' not in first["text"],
              first["text"][:64])
        check("a 400 can't-parse-entities is resent as plain text",
              len(tries) == 2 and second.get("parse_mode") is None and second["text"] == nasty.replace("**", ""),
              f"{len(tries)} attempts, plain: {second['text'][:46]!r}")


class Breaks:
    """An http layer that raises, once, what the bot does not expect: a closed or racing aiohttp session."""

    def __init__(self, http, method, error):
        self.http, self.method, self.error, self.fired = http, method, error, 0

    def post(self, url, json=None):
        if url.rsplit("/", 1)[-1] == self.method and not self.fired:
            self.fired += 1
            raise self.error
        return self.http.post(url, json=json)


def typing_tasks():
    return [task for task in asyncio.all_tasks() if "Bot.typing" in repr(task.get_coro())]


async def wedge_recovery():
    """Starting a turn must not be able to leave the chat busy for ever, with a typing loop running behind it."""
    answer = [{"type": "message", "text": "here is the answer"}, {"type": "done", "duration_ms": 1000}]
    async with running({CHAT}, answer, delay=0.02) as box:
        breaks = Breaks(box.bot.api.http, "sendMessage", RuntimeError("Session is closed"))
        box.bot.api.http, before = breaks, len(typing_tasks())
        box.fake.queue.append(message(1, CHAT, "first"))
        await until(lambda: breaks.fired and not box.bot.tasks)  # the update is finished, whether it went well or not
        stuck, leaked = box.bot.chats[CHAT]["busy"], len(typing_tasks()) - before
        box.fake.queue.append(message(2, CHAT, "second"))
        await until(lambda: any("here is the answer" in (row["text"] or "") for row in box.sent("sendMessage")))
        check("a send that blows up while starting a turn leaves neither a busy chat nor a typing loop",
              stuck is False and leaked == 0 and box.runners.turns() == 1
              and not any("Still working" in (row["text"] or "") for row in box.sent("sendMessage")),
              f"busy {stuck} after the failure, {leaked} leaked typing task(s), then {box.runners.turns()} turn")

    async with running({CHAT}, answer, delay=0.02,
                       scripted=[("sendMessage", (200, {"ok": True, "result": True}))]) as box:
        before = len(typing_tasks())
        box.fake.queue.append(message(1, CHAT, "first"))
        await until(lambda: any("here is the answer" in (row["text"] or "") for row in box.sent("sendMessage")))
        await until(lambda: not box.bot.tasks)
        check("a Bot API that answers sendMessage with true, not an object, still completes the turn",
              box.runners.turns() == 1 and box.bot.chats[CHAT]["busy"] is False
              and len(typing_tasks()) - before == 0,
              f"{box.runners.turns()} turn, busy {box.bot.chats[CHAT]['busy']}, {len(typing_tasks()) - before} leaked")


async def oversized_cards():
    """Every apostrophe in a post becomes &#x27;, so a card of untrusted text can pass 4096 and be refused whole."""
    seeded = "they said " + "'" * 190  # a post seeded to inflate: every apostrophe escapes to six characters
    quote = "it's a \"<big> & bold\" claim, isn't it? "
    events = [{"type": "preview", "title": "Preview - " + quote * 3, "total": 1234567, "exact": True, "seconds": 9,
               "per_day": [{"day": f"2026-09-{day:02d}", "count": day * 1111} for day in range(1, 17)],
               "examples": [{"body": seeded, "like_count": 77, "day": "2026-09-09",
                             "url": "https://x.com/i/status/" + "1" * 290}] * 3, "note": "'" * 300},
              {"type": "confirm_request", "confirmation_id": GOOD, "summary": "PS6 backlash\n" + "'" * 1200,
               "expires_ms": int(time.time() * 1000) + 300_000},
              {"type": "done", "duration_ms": 2000}]
    async with running({CHAT}, events, delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "show me"))
        await until(lambda: any(row.get("reply_markup") for row in box.sent("sendMessage")))
        await until(lambda: box.bot.chats[CHAT]["busy"] is False)
        out = box.sent("sendMessage") + box.sent("editMessageText")
        preview = next((row for row in box.sent("sendMessage") if "<pre>" in (row["text"] or "")), None)
        card = next((row for row in box.sent("sendMessage") if row.get("reply_markup")), None)
        raw = tg.preview_body(events[0])
        broken = [row for row in out if re.search(r"&(?!(?:amp|lt|gt|quot|#39|#x27);)", row["text"] or "")
                  or (row["text"] or "").count("<pre>") != (row["text"] or "").count("</pre>")
                  or (row["text"] or "").count("<b>") != (row["text"] or "").count("</b>")]
        check(f"a {len(raw):,}-character preview card is trimmed to fit, not dropped",
              preview is not None and len(preview["text"]) <= 4096 and "…" in preview["text"]
              and tg.BLOCK in preview["text"] and "1,234,567 posts" in preview["text"],
              f"{len(raw):,} -> {len(preview['text']):,} characters" if preview else "the preview never arrived")
        check("an over-long confirm card still arrives, with its two buttons",
              card is not None and len(card["text"]) <= 4096 and "…" in card["text"]
              and [button["text"] for button in card["reply_markup"]["inline_keyboard"][0]] == ["Confirm", "Cancel"],
              f"{len(card['text']):,} characters and a keyboard" if card else "the confirm card never arrived")
        check("no message goes out over the cap, with a cut entity or with a tag left open",
              not broken and max(len(row.get("text") or "") for row in box.fake.requests) <= 4096,
              f"{len(out)} messages, longest {max(len(row.get('text') or '') for row in box.fake.requests):,}")

    async with running({CHAT}, [{"type": "message", "text": "hello"}, {"type": "done", "duration_ms": 1}],
                       scripted=[("sendMessage", None, (400, {"ok": False, "error_code": 400,
                                                              "description": "Bad Request: message is too long"}))]) as box:
        box.fake.queue.append(message(1, CHAT, "hi"))
        await until(lambda: len([row for row in box.fake.got("sendMessage") if row.get("text") == "hello"]) >= 2)
        tries = [row for row in box.fake.got("sendMessage") if row.get("text") == "hello"]
        check("a 400 message-is-too-long is resent shorter instead of being dropped",
              len(tries) == 2 and "parse_mode" not in tries[-1],
              f"{len(tries)} attempt(s), last one plain: {'parse_mode' not in tries[-1]}")


async def rate_limit():
    async with running({CHAT}, [{"type": "message", "text": "hello"}, {"type": "done", "duration_ms": 1}],
                       scripted=[("sendMessage", None, (429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                                              "parameters": {"retry_after": 2}}))]) as box:
        box.fake.queue.append(message(1, CHAT, "hi"))
        await until(lambda: len([row for row in box.fake.got("sendMessage") if row.get("text") == "hello"]) >= 2)
        tries = [row for row in box.fake.got("sendMessage") if row.get("text") == "hello"]
        check("a 429 is retried after exactly retry_after",
              len(tries) == 2 and 2.0 in box.clock.slept and tries[1]["at"] - tries[0]["at"] >= 1.9,
              f"retried {tries[1]['at'] - tries[0]['at']:.2f} virtual s later, slept {2.0 in box.clock.slept}")


async def busy_and_commands():
    slow = [{"type": "progress", "text": "thinking hard"}, {"type": "message", "text": "done at last"},
            {"type": "done", "duration_ms": 1}]
    async with running({CHAT}, slow, delay=0.3) as box:
        box.fake.queue.extend([message(1, CHAT, "first question"), message(2, CHAT, "second question")])
        await until(lambda: box.runners.turns() == 1 and any("Still working" in (row["text"] or "") for row in box.sent("sendMessage")))
        check("a second message while busy is told to wait, and starts no second turn",
              len(box.runners.made) == 1 and box.runners.turns() == 1
              and sum("Still working on your previous message." in (row["text"] or "") for row in box.sent("sendMessage")) == 1,
              f"{len(box.runners.made)} runner(s), said {box.runners.made[0].said}")
        await until(lambda: any("done at last" in (row["text"] or "") for row in box.sent("sendMessage")))
        await until(lambda: box.bot.chats[CHAT]["busy"] is False)  # /new while busy is refused, and rightly so

        first_session = box.bot.chats[CHAT]["session"]
        box.fake.queue.append(message(3, CHAT, "/new"))
        await until(lambda: any("Fresh start" in (row["text"] or "") for row in box.sent("sendMessage")))
        saved = json.loads((box.root / "telegram/chats.json").read_text(encoding="utf-8"))
        check("/new changes the session id, on disk too",
              box.bot.chats[CHAT]["session"] != first_session and len(box.runners.made) == 2
              and saved["chats"][str(CHAT)]["session_id"] == box.bot.chats[CHAT]["session"],
              f"...{first_session[-8:]} -> ...{box.bot.chats[CHAT]['session'][-8:]}")

        box.fake.queue.extend([message(4, CHAT, "/id"), message(5, CHAT, "/start@signal_test_bot")])
        await until(lambda: any("Ask me things like" in (row["text"] or "") for row in box.sent("sendMessage")))
        await polled(box)
        texts = [row["text"] for row in box.sent("sendMessage")]
        welcome = next((text for text in texts if "Ask me things like" in text), "")
        check("/id and /start answer without a model call",
              any(f"This chat&#x27;s ID is {CHAT}" in text or f"This chat's ID is {CHAT}" in text for text in texts)
              and "/new" in welcome and box.runners.turns() == 1,
              f"{box.runners.turns()} turn for 5 updates")
        check("/start offers three real AI questions and none of the invented ones",
              "What are people saying about AI on Bluesky right now?" in welcome
              and "How did people react to the Anthropic resignation post around Sep 9?" in welcome
              and "What was said about GPT-6 Astra in early September?" in welcome
              and "/details" in welcome and not any(word in welcome for word in ("PlayStation", "student loan", "Physint")),
              repr([line for line in welcome.splitlines() if line.startswith("- ")]))
        offsets = [row.get("offset") for row in box.sent("getUpdates") if row.get("offset")]
        check("offsets only ever advance, so an update is never handled twice",
              offsets == sorted(offsets) and max(offsets) == 6 and box.runners.turns() == 1,
              f"offsets {offsets[0]}..{offsets[-1]} over {len(offsets)} polls")


async def pairing_and_refusal():
    async with running(set(), SCRIPT) as box:
        box.fake.queue.append(message(1, STRANGER, "hello?"))
        await until(lambda: box.sent("sendMessage"))
        await polled(box)
        text = box.sent("sendMessage")[0]["text"]
        check("pairing mode answers with the chat ID and the .env line, and never calls the model",
              str(STRANGER) in text and f"TELEGRAM_ALLOWED_CHAT_IDS={STRANGER}" in text
              and not box.runners.made and len(box.sent("sendMessage")) == 1,
              repr(" ".join(text.split())[:58]))
    async with running({CHAT}, SCRIPT) as box:
        box.fake.queue.extend([message(1, STRANGER, "let me in"), message(2, STRANGER, "please")])
        await until(lambda: box.sent("sendMessage"))
        await polled(box, 3)
        check("a chat that is not allow-listed is refused once an hour and never reaches the model",
              len(box.sent("sendMessage")) == 1 and "private" in box.sent("sendMessage")[0]["text"] and not box.runners.made,
              f"1 reply to 2 messages: {box.sent('sendMessage')[0]['text'][:46]!r}")


# ---- the confirm button ----------------------------------------------------------------------------

GOOD, EXPIRED, DECIDED, ELSEWHERE = "Conf1rmat1on_id0", "Expired000000000", "Decided000000000", "Otherchat0000000"


def write_record(directory, confirmation_id, **extra):
    """The shape demo_mcp_server.request_confirmation writes (test_bridge.py 4a)."""
    now = int(time.time() * 1000)
    path = Path(directory) / "confirmations" / f"{confirmation_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"confirmation_id": confirmation_id, "spec_hash": "a" * 64, "created_ms": now,
                                "expires_ms": now + 300_000, "decision": None, "decided_ms": None,
                                "consumed": False} | extra, indent=2), encoding="utf-8")
    return path


async def confirm_flow():
    script = [{"type": "confirm_request", "confirmation_id": GOOD, "summary": "PS6 backlash\n20,000 posts, about $0.36",
               "expires_ms": int(time.time() * 1000) + 300_000}, {"type": "done", "duration_ms": 4000}]
    async with running({CHAT, OTHER}, script, delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "start it"))
        await until(lambda: any(row.get("reply_markup") for row in box.sent("sendMessage")))
        card = next(row for row in box.sent("sendMessage") if row.get("reply_markup"))
        buttons = card["reply_markup"]["inline_keyboard"][0]
        check("a confirm_request becomes a message with Confirm and Cancel buttons",
              [button["text"] for button in buttons] == ["Confirm", "Cancel"]
              and [button["callback_data"] for button in buttons] == [f"c:{GOOD}:1", f"c:{GOOD}:0"]
              and all(len(button["callback_data"].encode()) < 64 for button in buttons) and "20,000 posts" in card["text"],
              str([button["callback_data"] for button in buttons]))
        await until(lambda: box.bot.chats[CHAT]["busy"] is False)

        directory = box.bot.chats[CHAT]["dir"]
        path = write_record(directory, GOOD)
        before = json.loads(path.read_text(encoding="utf-8"))
        box.fake.queue.append(callback(2, CHAT, f"c:{GOOD}:1", 501))
        await until(lambda: box.runners.turns() == 2)
        await asyncio.sleep(0.1)
        after = json.loads(path.read_text(encoding="utf-8"))
        untouched = [key for key in before if key not in ("decision", "decided_ms")]
        edits = [row for row in box.sent("editMessageText") if "Confirmed." in (row["text"] or "")]
        check("the button writes the decision exactly as the bridge does",
              after["decision"] == "approved" and after["decided_ms"] >= before["created_ms"]
              and {key: after[key] for key in untouched} == {key: before[key] for key in untouched}
              and not list(path.parent.glob("*.tmp")),
              f"decision {before['decision']} -> {after['decision']}, other fields unchanged, no .tmp left")
        check("the keyboard is removed and the card says Confirmed",
              edits and edits[-1].get("reply_markup") is None and "20,000 posts" in edits[-1]["text"]
              and box.sent("answerCallbackQuery"),
              f"{len(edits)} edit(s), {len(box.sent('answerCallbackQuery'))} answerCallbackQuery")
        check("the new turn is started by the harness, in the bridge's own words",
              box.runners.made[0].said[1] == SENTENCE.format(button="Confirm", confirmation_id=GOOD)
              and SENTENCE in (HARNESS / "bridge.py").read_text(encoding="utf-8"),
              repr(box.runners.made[0].said[1]))

        await until(lambda: box.bot.chats[CHAT]["busy"] is False)
        others = box.bot.chat(OTHER)
        expired = write_record(directory, EXPIRED, expires_ms=int(time.time() * 1000) - 1)
        decided = write_record(directory, DECIDED, decision="approved", decided_ms=int(time.time() * 1000))
        mine = write_record(directory, ELSEWHERE)
        keep = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in
                (("expired", expired), ("decided", decided), ("mine", mine))}
        turns, edits_before = box.runners.turns(), len(box.sent("editMessageText"))
        for update_id, chat_id, data in ((3, CHAT, f"c:{EXPIRED}:1"), (4, CHAT, f"c:{DECIDED}:1"),
                                         (5, OTHER, f"c:{ELSEWHERE}:1"), (6, CHAT, "c:../../x:1"),
                                         (7, CHAT, f"c:{GOOD}:0")):
            box.fake.queue.append(callback(update_id, chat_id, data, 600 + update_id))
        await until(lambda: len(box.sent("answerCallbackQuery")) >= 6)
        await polled(box, 3)
        await asyncio.sleep(0.2)
        said = [row["text"] for row in box.sent("editMessageText")[edits_before:]]
        now = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in
               (("expired", expired), ("decided", decided), ("mine", mine))}
        check("an expired, an already-decided and a re-pressed confirmation are all refused",
              any("expired. Ask for a new one" in text for text in said)
              and sum("already decided" in text for text in said) == 2 and now == keep,
              " | ".join(text.splitlines()[-1][:30] for text in said))
        check("a callback for another chat's confirmation is rejected and that record is untouched",
              any("no longer valid" in text for text in said) and now["mine"]["decision"] is None
              and not (others["dir"] / "confirmations" / f"{ELSEWHERE}.json").exists(),
              f"other session ...{others['session'][-8:]} has no copy of that id")
        check("a malformed confirmation id never reaches a path and starts no turn",
              sorted(item.name for item in (directory / "confirmations").iterdir())
              == sorted(f"{name}.json" for name in (GOOD, EXPIRED, DECIDED, ELSEWHERE))
              and box.runners.turns() == turns and len(said) == 4,
              f"{len(list((directory / 'confirmations').iterdir()))} files, {turns} turns before and after, {len(said)} edits for 5 callbacks")
        check("only the button can decide: decide() refuses everything else",
              tg.decide(directory, GOOD, True) == "That confirmation was already decided."
              and tg.decide(directory, "../../x", True) == tg.INVALID
              and tg.decide(directory, "nosuchid00000000", True) == tg.INVALID
              and tg.decide(directory, EXPIRED, True) == "That confirmation expired. Ask for a new one."
              and tg.decide(directory, ELSEWHERE, True, busy=True) == "Still working on your previous message.",
              "decided, traversal, unknown, expired and busy all refused")


# ---- the morning brief -----------------------------------------------------------------------------

def at(hour, minute, second=0):
    return lambda: datetime(2026, 9, 19, hour, minute, second)


def said(box, words):
    return [row["text"] for row in box.sent("sendMessage") if words in (row["text"] or "")]


async def schedule_commands():
    async with running({CHAT}, SCRIPT, wall=at(6, 30)) as box:
        path = box.root / "telegram/chats.json"
        box.fake.queue.append(message(1, CHAT, "/schedule"))
        await until(lambda: said(box, "You have no morning brief yet"))
        box.fake.queue.append(message(2, CHAT, "/schedule 7:30 2"))
        await until(lambda: said(box, "Every day at 07:30"))
        saved, answer = json.loads(path.read_text(encoding="utf-8")), (said(box, "Every day at 07:30") or [""])[0]
        check("/schedule 7:30 2 is kept per chat in chats.json and answered in plain words",
              saved["chats"][str(CHAT)]["schedule"] == {"at": "07:30", "minutes": 1.5, "last": None}
              and "about 1 minute 30 seconds" in answer and "last 24 hours" in answer and "comes today" in answer,
              json.dumps(saved["chats"][str(CHAT)]))

        box.fake.queue.append(message(3, CHAT, "/schedule 6am 5"))
        await until(lambda: said(box, "Every day at 06:00"))
        saved, answer = json.loads(path.read_text(encoding="utf-8")), (said(box, "Every day at 06:00") or [""])[0]
        check("a time already past starts tomorrow, and a length over ninety seconds is capped and said so",
              saved["chats"][str(CHAT)]["schedule"] == {"at": "06:00", "minutes": 1.5, "last": "2026-09-19"}
              and "comes tomorrow" in answer and "longest a brief can be is 1 minute 30 seconds" in answer,
              json.dumps(saved["chats"][str(CHAT)]["schedule"]))
        again = tg.Bot(TOKEN, {CHAT}, None, sessions=box.root / "sessions", chats_path=path)
        check("a restart keeps the schedule", again.plans == {CHAT: {"at": "06:00", "minutes": 1.5, "last": "2026-09-19"}},
              str(again.plans))

        box.fake.queue.extend([message(4, CHAT, "/schedule"), message(5, CHAT, "/schedule banana")])
        await until(lambda: said(box, "comes every day at 06:00") and said(box, "did not understand that time"))
        box.fake.queue.append(message(6, CHAT, "/schedule off"))
        await until(lambda: said(box, "will not send a morning brief"))
        await asyncio.sleep(0.7)  # two ticks of the timer, 30 virtual seconds each
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("/schedule shows it, refuses nonsense and turns it off, with no model call and no brief ordered",
              "schedule" not in saved["chats"].get(str(CHAT), {}) and not box.bot.plans
              and box.runners.turns() == 0 and not box.briefs.orders,
              f"{len(box.sent('sendMessage'))} replies, {box.runners.turns()} turns, {len(box.briefs.orders)} orders")


async def scheduled_delivery():
    plan = {"at": "07:00", "minutes": 3, "last": None}
    async with running({CHAT, OTHER}, wall=at(7, 0, 20), briefs=Briefs(busy=1),
                       plans={CHAT: dict(plan), OTHER: dict(plan), STRANGER: dict(plan)}) as box:
        await until(lambda: len(box.fake.got("sendAudio")) >= 2)
        await asyncio.sleep(0.7)  # two more ticks: a morning already sent must not be ordered again
        orders, audios = box.briefs.orders, box.fake.got("sendAudio")
        check("at seven the timer orders one 3-minute brief about the last 24 hours, as the page's own origin",
              len(orders) == 2 and all(order == {"origin": box.base, "hours": 24, "seconds": 90} for order in orders),
              f"{len(orders)} orders (the first met a busy server): {orders[-1]}")
        check("a server busy with another brief is waited for, 15 s at a time", 15.0 in box.clock.slept and 5.0 in box.clock.slept,
              f"slept 15 s {box.clock.slept.count(15.0)}x, polled every 5 s {box.clock.slept.count(5.0)}x")
        check("one recording is uploaded to every allowed chat that asked, and to nobody else",
              sorted(row["chat_id"] for row in audios) == sorted([str(CHAT), str(OTHER)]) and box.briefs.fetched == 1
              and all(row["audio"] == {"filename": "morning-brief.mp3", "content_type": "audio/mpeg", "bytes": len(MP3)} for row in audios),
              f"{len(audios)} uploads of {len(MP3):,} bytes from {box.briefs.fetched} download")
        check("the recording carries its title and its stories as a plain caption",
              all(row["title"] == "AI labs trade blows" and row["performer"] == "Morning Brief" and "parse_mode" not in row
                  and row["caption"].splitlines() == ["AI labs trade blows", "- A new model lands", "- It's a \"<big>\" & bold claim"]
                  for row in audios), repr(audios[0]["caption"]) if audios else "no upload")
        saved = json.loads((box.root / "telegram/chats.json").read_text(encoding="utf-8"))["chats"]
        check("the morning is marked as sent on disk, so it is never ordered twice, and no model was asked",
              saved[str(CHAT)]["schedule"]["last"] == saved[str(OTHER)]["schedule"]["last"] == "2026-09-19"
              and saved[str(STRANGER)]["schedule"]["last"] is None and len(box.briefs.orders) == 2 and box.runners.turns() == 0,
              f"last {saved[str(CHAT)]['schedule']['last']}, {len(box.briefs.orders)} orders after two more ticks")

        probe, windows = tg.Bot(TOKEN, {CHAT}, None, sessions=box.root / "probe", chats_path=box.root / "probe.json"), []
        probe.plans[CHAT] = dict(plan)
        for hour, minute in ((6, 59), (7, 0), (8, 59), (9, 1)):
            probe.clock = at(hour, minute)
            windows.append(bool(probe.due()))
        probe.clock = at(7, 5)
        probe.plans[CHAT]["last"] = "2026-09-19"
        sent_today = probe.due()
        probe.plans[CHAT]["last"] = "2026-09-18"
        check("a brief is due from seven until two hours later, once a day",
              windows == [False, True, True, False] and sent_today == [] and probe.due() == [CHAT], f"06:59 07:00 08:59 09:01 -> {windows}")


async def brief_on_demand():
    async with running({CHAT}, SCRIPT, briefs=Briefs(working=6)) as box:
        box.fake.queue.extend([message(1, CHAT, "/brief 2"), message(2, CHAT, "/brief")])
        await until(lambda: box.fake.got("sendAudio"))
        await polled(box)
        check("/brief caps a new recording at ninety seconds, and a second /brief meanwhile orders nothing",
              len(box.briefs.orders) == 1 and box.briefs.orders[0]["seconds"] == 90 and len(box.fake.got("sendAudio")) == 1
              and len(said(box, "Making a brief of about 1 minute 30 seconds")) == 1 and len(said(box, "already being made")) == 1
              and box.runners.turns() == 0 and not box.bot.making,
              f"{len(box.briefs.orders)} order of {box.briefs.orders[0]['seconds']} s, {len(box.fake.got('sendAudio'))} upload")


async def brief_failures():
    async with running({CHAT}, brief_base="http://127.0.0.1:1") as box:
        box.fake.queue.append(message(1, CHAT, "/brief"))
        await until(lambda: said(box, "I could not make your brief"), timeout=30)
        check("with Morning Brief not running the chat is told so, in plain words",
              bool(said(box, "Morning Brief is not running on the laptop")) and not box.fake.got("sendAudio"),
              repr((said(box, "I could not make") or [""])[0][:70]))
    failed = {"id": BRIEF, "status": "failed", "step": "No posts were collected in that window."}
    async with running({CHAT}, briefs=Briefs(final=failed)) as box:
        box.fake.queue.append(message(1, CHAT, "/brief"))
        await until(lambda: said(box, "I could not make your brief"))
        check("a brief that fails is reported with the server's own reason",
              bool(said(box, "No posts were collected in that window.")) and not box.fake.got("sendAudio"),
              repr((said(box, "I could not make") or [""])[0][:70]))
    refusal = (400, {"ok": False, "error_code": 400, "description": "Bad Request: file is too big"})
    for name, briefs, scripted in (("a brief with no recording", Briefs(final={**READY, "audio": None}), ()),
                                   ("a recording Telegram refuses", Briefs(), [("sendAudio", refusal)])):
        async with running({CHAT}, briefs=briefs, scripted=scripted) as box:
            box.fake.queue.append(message(1, CHAT, "/brief"))
            await until(lambda: said(box, "Good morning. Here is what happened overnight."))
            check(f"{name} arrives as text instead",
                  bool(said(box, "here is your brief to read")) and len(box.fake.got("sendAudio")) == len(scripted),
                  f"{len(box.fake.got('sendAudio'))} upload attempt(s), then the script as a message")


# ---- the details panel, as a command ----------------------------------------------------------------

async def details_command():
    async with running({CHAT}, SCRIPT, delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "/details"))
        await until(lambda: said(box, "I have not done anything yet"))
        check("/details before anything has happened says so, and asks no model",
              len(box.sent("sendMessage")) == 1 and box.runners.turns() == 0
              and "Ask me a question first" in box.sent("sendMessage")[0]["text"],
              repr(box.sent("sendMessage")[0]["text"][:60]))

        box.fake.queue.append(message(2, CHAT, QUESTION))
        await served(box)
        before = len(box.sent("sendMessage"))
        box.fake.queue.append(message(3, CHAT, "/details"))
        await until(lambda: any("Exact request" in (row["text"] or "") for row in box.sent("sendMessage")[before:]))
        await polled(box)
        panels = [row["text"] for row in box.sent("sendMessage")[before:]]
        whole = "\n".join(panels)
        check("/details replies with every step in full: title, why, result and the exact request",
              "<b>1. Searching X/Twitter for &quot;anthropic&quot;" in whole
              and "Why: to see how much there is before drafting" in whole
              and "Found 7,221 posts. Most were on Sep 9 (4,821)." in whole
              and "Words searched: &quot;anthropic&quot;, &quot;resignation&quot;" in whole and "Dates: Sep 9 to Sep 11" in whole
              and "Took 3.1 seconds" in whole and "<b>4. Searching the last 15 minutes of Bluesky</b>" in whole
              and "That did not work: Bluesky did not answer." in whole and box.runners.turns() == 1,
              f"{len(panels)} message(s), {whole.count('Exact request')} exact requests, {box.runners.turns()} turn")
        check("the exact request is a code block with the tool and its input, kept as it was sent",
              "<pre>mcp__harness__preview_keywords\n{\n  &quot;reason&quot;:" in whole
              and "&quot;date_from&quot;: &quot;2026-09-09&quot;" in whole and "em dash — kept verbatim" in whole
              and whole.count("<pre>") == whole.count("</pre>") == 4
              and all(len(text) <= 4096 for text in panels),
              repr(whole[whole.find("<pre>"):whole.find("<pre>") + 58]))
        check("a message is never cut in the middle of a step",
              all(text.startswith("<b>") and text.count("Exact request") == text.count("<pre>") for text in panels),
              f"blocks per message: {[text.count('Exact request') for text in panels]}")


# ---- cards -----------------------------------------------------------------------------------------

CHART = {"kind": "chart", "title": "Posts about AI a day", "caption": "Sep 9 to Sep 11, the archive",
         "png_url": "/api/charts/ai-a-day.png",
         "bars": [{"label": "2026-09-09", "value": 4821}, {"label": "2026-09-10", "value": 2400}]}


async def chart_cards():
    async with running({CHAT}, [{"type": "card", "card": CHART}, {"type": "done", "duration_ms": 1000}], delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "chart it"))
        await until(lambda: box.fake.got("sendPhoto"))
        await served(box)
        photos = box.fake.got("sendPhoto")
        check("a chart card with a picture is sent as a photo with its caption",
              len(photos) == 1 and photos[0]["chat_id"] == str(CHAT) and box.charts.asked == ["/api/charts/ai-a-day.png"]
              and photos[0]["photo"] == {"filename": "chart.png", "content_type": "image/png", "bytes": len(PNG)}
              and photos[0]["caption"] == "Posts about AI a day\nSep 9 to Sep 11, the archive"
              and "parse_mode" not in photos[0] and not any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")),
              f"{len(photos)} photo of {len(PNG):,} bytes, caption {photos[0]['caption'][:34]!r}" if photos else "no photo")

    bars_only = {key: value for key, value in CHART.items() if key != "png_url"}
    outside = {**CHART, "png_url": "http://evil.example/api/charts/x.png"}
    for name, card, asked in (("with no picture", bars_only, []), ("whose picture is not on this server", outside, [])):
        async with running({CHAT}, [{"type": "card", "card": card}, {"type": "done", "duration_ms": 1}], delay=0.02) as box:
            box.fake.queue.append(message(1, CHAT, "chart it"))
            await until(lambda: any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")))
            await served(box)
            drawn = next(row["text"] for row in box.sent("sendMessage") if tg.BLOCK in (row["text"] or ""))
            check(f"a chart card {name} is sent as bars, with its dates in words",
                  not box.fake.got("sendPhoto") and box.charts.asked == asked and "<b>Posts about AI a day</b>" in drawn
                  and "Sep 9" in drawn and "4,821" in drawn and "2026-09-09" not in drawn,
                  repr(next(line for line in drawn.splitlines() if tg.BLOCK in line)))

    async with running({CHAT}, [{"type": "card", "card": CHART}, {"type": "done", "duration_ms": 1}], delay=0.02,
                       charts=Charts(missing=True)) as box:
        box.fake.queue.append(message(1, CHAT, "chart it"))
        await until(lambda: any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")))
        check("a picture the server does not have falls back to the bars, not to nothing",
              not box.fake.got("sendPhoto") and box.charts.asked == ["/api/charts/ai-a-day.png"], "404, then bars")

    junk = [{"type": "card", "card": {"kind": "spaceship", "title": "?"}}, {"type": "card", "card": "not an object"},
            {"type": "card", "card": {"kind": "chart"}}, {"type": "card"}, {"type": "done", "duration_ms": 1}]
    async with running({CHAT}, junk, delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "surprise me"))
        await served(box)
        await polled(box)
        check("a card of a kind this front end does not draw is ignored in silence",
              not box.fake.got("sendPhoto") and not box.fake.got("sendAudio")
              and [row["text"] for row in box.sent("sendMessage")] == ["Thinking…"],
              repr([row["text"] for row in box.sent("sendMessage")]))


async def hostile_cards():
    """A card is written by the model, so it is treated as something a stranger wrote."""
    sneaky = {**CHART, "png_url": "/api/%2e%2e/%2e%2e/state/sessions/secret.json"}
    async with running({CHAT}, [{"type": "card", "card": sneaky}, {"type": "done", "duration_ms": 1}], delay=0.02) as box:
        box.fake.queue.append(message(1, CHAT, "chart it"))
        await until(lambda: any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")))
        await served(box)
        check("a picture path that escapes /api once it is a URL is never fetched at all",
              box.charts.elsewhere == [] and box.charts.asked == [] and not box.fake.got("sendPhoto")
              and tg.picture_path("/api/%2e%2e/%2e%2e/secret") == "",
              f"asked for {box.charts.elsewhere + box.charts.asked}, drew the bars instead")

    hangs = Charts(hangs=2.0)
    was = tg.CARD_FETCH_S
    tg.CARD_FETCH_S = 0.2  # a fifth of a second here stands for the fifteen the bot really waits
    try:
        script = [{"type": "card", "card": CHART}, {"type": "message", "text": "here is the answer"},
                  {"type": "done", "duration_ms": 1000}]
        async with running({CHAT}, script, delay=0.02, charts=hangs) as box:
            started = time.monotonic()
            box.fake.queue.append(message(1, CHAT, "chart it"))
            arrived = await until(lambda: any("here is the answer" in (row["text"] or "") for row in box.sent("sendMessage")),
                                  timeout=10)
            waited = time.monotonic() - started
            check("a chart server that hangs costs the picture, never the answer behind it",
                  arrived and waited < 1.2 and not box.fake.got("sendPhoto")
                  and any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")),
                  f"the answer arrived {waited:.2f}s in, with the bars instead of the picture")
    finally:
        tg.CARD_FETCH_S = was

    was = tg.MAX_PICTURE
    tg.MAX_PICTURE = 2048  # 2 KB here stands for the 10 MB Telegram refuses anyway
    try:
        async with running({CHAT}, [{"type": "card", "card": CHART}, {"type": "done", "duration_ms": 1}], delay=0.02,
                           charts=Charts(copies=8)) as box:
            box.fake.queue.append(message(1, CHAT, "chart it"))
            await until(lambda: any(tg.BLOCK in (row["text"] or "") for row in box.sent("sendMessage")))
            await served(box)
            check("a picture far bigger than we will hold is dropped for the bars, not uploaded",
                  not box.fake.got("sendPhoto") and box.charts.asked == ["/api/charts/ai-a-day.png"],
                  f"{len(PNG) * 8:,} bytes offered, cap {tg.MAX_PICTURE:,}, bars sent")
    finally:
        tg.MAX_PICTURE = was


async def details_flood():
    """A turn that called sixty tools must not answer /details with a dozen messages in a row."""
    async with running({CHAT}, [{"type": "done", "duration_ms": 1}]) as box:
        state, turn = box.bot.chat(CHAT), tg.Turn()
        for index in range(60):
            turn.event({"type": "step", "phase": "start", "id": f"s{index}", "n": index + 1,
                        "title": f"Searching X/Twitter for \"AI\", part {index}", "why": "to see what is there",
                        "detail": {"tool": "mcp__harness__preview_keywords", "input": {"keywords": ["AI"], "part": index}}})
            turn.event({"type": "step", "phase": "end", "id": f"s{index}", "ok": True, "ms": 1200,
                        "outcome": "Found 7,221 posts."})
        state["last"] = turn
        box.fake.queue.append(message(1, CHAT, "/details"))
        await until(lambda: said(box, "There were 60 steps in all"))
        await polled(box)
        panels = [row["text"] for row in box.sent("sendMessage")]
        check("a runaway turn is cut at twenty steps and the reader is told how many there were",
              len(panels) <= 4 and "part 19" in "\n".join(panels) and "part 20" not in "\n".join(panels)
              and panels[-1].endswith("These are the first 20.") and all(len(text) <= 4096 for text in panels),
              f"{len(panels)} messages for 60 steps, ending {panels[-1][-46:]!r}")


async def brief_cards():
    card = {"kind": "brief", "brief_id": BRIEF, "title": "AI labs trade blows"}
    async with running({CHAT}, [{"type": "card", "card": card}, {"type": "message", "text": "It is being made now."},
                               {"type": "done", "duration_ms": 1000}], delay=0.02, briefs=Briefs(working=3)) as box:
        await until(lambda: box.fake.got("getUpdates"))
        box.fake.queue.append(message(1, CHAT, "brief me on AI in one minute"))
        await until(lambda: any("It is being made now." in (row["text"] or "") for row in box.sent("sendMessage")))
        turn_done = box.clock.now()
        await until(lambda: said(box, "Your brief is ready"), timeout=20)
        audios = box.fake.got("sendAudio")
        check("a brief card is followed in the background, so the answer is not held up",
              box.clock.now() > turn_done and box.briefs.polls >= 4 and box.clock.slept.count(5.0) >= 3,
              f"{box.briefs.polls} polls 5 s apart, then a ready notice")
        check("a model brief card waits for human confirmation before uploading audio",
              not audios and box.briefs.fetched == 0 and box.runners.turns() == 1
              and "AI labs trade blows" in said(box, "Your brief is ready")[0]
              and "needs your confirmation" in said(box, "Your brief is ready")[0],
              "Ready notice only; no recording fetched or uploaded")

    slow = Briefs(working=10_000)  # never finishes: the wait has to end by itself
    async with running({CHAT}, [{"type": "card", "card": card}, {"type": "done", "duration_ms": 1}], delay=0.02,
                       briefs=slow) as box:
        box.fake.queue.append(message(1, CHAT, "brief me"))
        await until(lambda: said(box, "taking longer than usual"), timeout=30)
        check("a brief that never finishes is given three minutes, then the chat is told where it will be",
              not box.fake.got("sendAudio") and slow.polls <= 40 and "Morning Brief page" in said(box, "taking longer")[0],
              f"{slow.polls} polls over three virtual minutes, then {said(box, 'taking longer')[0][:38]!r}")

    failed = Briefs(working=0, final={"id": BRIEF, "status": "failed", "step": "No posts were collected in that window."})
    for name, card_now, briefs, expected in (
            ("a brief that fails is reported with the server's own reason", card, failed, "No posts were collected"),
            ("a brief with no recording says so instead of promising audio", card,
             Briefs(working=0, final={**READY, "audio": None}), "does not have an audio recording"),
            ("a brief server that answers nonsense still gets a whole sentence", card,
             Briefs(working=0, final={"id": BRIEF}), "Morning Brief could not finish it.")):
        async with running({CHAT}, [{"type": "card", "card": card_now}, {"type": "done", "duration_ms": 1}], delay=0.02,
                           briefs=briefs) as box:
            box.fake.queue.append(message(1, CHAT, "brief me"))
            await until(lambda: said(box, expected), timeout=20)
            check(name, bool(said(box, expected)) and not box.fake.got("sendAudio"), repr(said(box, expected)[0][:64]))

    bad = [{"type": "card", "card": {"kind": "brief", "brief_id": "../../etc/passwd"}},
           {"type": "card", "card": {"kind": "brief", "brief_id": BRIEF}},
           {"type": "card", "card": {"kind": "brief", "brief_id": BRIEF}},  # the same brief twice: followed once
           {"type": "done", "duration_ms": 1}]
    async with running({CHAT}, bad, delay=0.02, briefs=Briefs(working=4)) as box:
        box.fake.queue.append(message(1, CHAT, "brief me"))
        await until(lambda: said(box, "Your brief is ready"), timeout=20)
        await polled(box, 3)
        check("a brief id that is not one is never turned into a path, and one brief is waited for once",
              len(said(box, "Your brief is ready")) == 1 and not box.fake.got("sendAudio")
              and box.briefs.fetched == 0 and not box.bot.following,
              f"{len(said(box, 'Your brief is ready'))} ready notice for three cards, no audio download or upload")


# ---- the network this laptop is on ------------------------------------------------------------------

class Unreachable:
    """An http layer where Telegram cannot be reached: the first `times` calls to it raise, as a
    blocked network does. The Morning Brief server on the same host keeps answering."""

    def __init__(self, http, times=3):
        self.http, self.times, self.tried = http, times, 0

    def post(self, url, json=None, data=None):
        if "/bot" in url:
            self.tried += 1
            if self.tried <= self.times:
                raise aiohttp.ClientConnectorError(types.SimpleNamespace(ssl=None, host="api.telegram.org", port=443),
                                                   OSError("blocked"))
        return self.http.post(url, json=json) if data is None else self.http.post(url, data=data)

    def get(self, url):
        return self.http.get(url)


async def blocked_network():
    async with running({CHAT}, [{"type": "message", "text": "here is the answer"}, {"type": "done", "duration_ms": 1}],
                       delay=0.02, http_layer=lambda http: Unreachable(http, times=3)) as box:
        await until(lambda: box.fake.got("getMe"), timeout=20)
        await until(lambda: box.fake.got("getUpdates"), timeout=20)
        printed = captured()
        box.fake.queue.append(message(1, CHAT, "hello?"))
        await until(lambda: any("here is the answer" in (row["text"] or "") for row in box.sent("sendMessage")), timeout=20)
        check("a network that blocks Telegram is waited out, not a reason to stop",
              printed.count("Telegram cannot be reached from this network.") == 1
              and "use a phone hotspot" in printed and "@signal_test_bot" in printed
              and not box.task.done() and box.runners.turns() == 1,
              f"the sentence was printed once, then it connected and served {box.runners.turns()} turn")
        check("it tries again about every minute, and the waits grow to a minute at most",
              [wait for wait in box.clock.slept if wait in (10.0, 20.0, 40.0, 60.0)][:3] == [10.0, 20.0, 40.0]
              and max(box.clock.slept) <= 60.0,
              f"waited {[wait for wait in box.clock.slept if wait >= 10.0][:4]} virtual s between tries")


# ---- one voice -------------------------------------------------------------------------------------

# Everything a person reads, on the website and here, uses commas, full stops, "and" and "to" and
# nothing else. The exact request inside /details is the one exception, exactly as the page's own
# panel keeps it: it is what was really sent, and it is not ours to reword.
BANNED = re.compile(r"[‒–—―·•‣▪・←-⇙;]|(?<=\S) -{1,2} (?=\S)|-&gt;|=&gt;")
PRE = re.compile(r"<pre>.*?</pre>", re.S)
# The other half of one voice: characters a reader cannot see but the phone obeys. A post carrying a
# right to left override turns the rest of the message around it backwards, which is how a link or a
# file name is disguised as something else. Nothing we show has any use for one. Listed here by
# number, and on purpose: the bot's own table is a different list, so a mistake in one is not agreed
# to by the other, and this file stays readable in an editor.
UNSEEN = [chr(code) for code in (0x00, 0x07, 0x1b, 0x7f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2069)]
FLIP = chr(0x202e) + "gnp.exe" + chr(0x202c)  # reads as "exe.png" on a phone


def voice_problems(texts):
    problems = []
    for text in texts:
        body = html.unescape(re.sub(r"<[^>]*>", "", PRE.sub("", str(text or ""))))  # what the reader really sees
        for hit in BANNED.finditer(body):
            problems.append(f"{hit.group(0)!r} in {body[max(0, hit.start() - 30):hit.start() + 30]!r}")
    return problems


NASTY = [  # the punctuation a model and a server really do produce, in every place a user can read
    {"type": "progress", "text": "Counting the posts that match — this takes a moment"},
    {"type": "step", "phase": "start", "id": "s1", "n": 1, "tool": "preview_keywords",
     "title": 'Searching X/Twitter for "AI" · Sep 9 to Sep 11', "why": "to size it up first; then we draft",
     "detail": {"tool": "mcp__harness__preview_keywords", "input": {"keywords": ["AI"], "note": "kept — verbatim; here"}},
     "facts": [{"label": "Words searched", "value": '"AI" · "AGI"'}]},
    {"type": "step", "phase": "end", "id": "s1", "ok": False, "ms": 2200, "outcome": "That did not work: no data — sorry"},
    {"type": "preview", "title": "What we found — X/Twitter", "total": 87, "exact": False, "seconds": 2,
     "per_day": [{"day": "2026-09-09", "count": 87}],
     "examples": [{"body": f"AI safety — it matters; a lot, see {FLIP}", "like_count": 12, "day": "2026-09-09",
                   "url": "https://x.com/i/status/2"}],
     "note": "Most of them are one template · be careful"},
    {"type": "spec", "spec": {"name": "AI backlash"}, "spec_hash": "ab" * 32},
    {"type": "confirm_request", "confirmation_id": GOOD, "summary": "AI backlash — 20,000 posts; about $0.36",
     "expires_ms": int(time.time() * 1000) + 300_000},
    {"type": "card", "card": {"kind": "chart", "title": f"Posts a day — AI {FLIP}", "caption": "Sep 9 to Sep 11; the archive",
                              "bars": [{"label": "2026-09-09", "value": 87}]}},
    {"type": "error", "text": "the tool server said: broken — try again; later"},
    {"type": "message", "text": f"Here is what I found — **87 posts**; most on Sep 9 · a quiet day.{chr(0x2066)}{FLIP}"},
    {"type": "done", "duration_ms": 22000},
]


async def one_voice():
    # no recording this time, so the spoken script comes back as text and is scanned with everything else
    spoken = Briefs(working=1, final={**READY, "title": "AI labs trade blows — day two", "audio": None,
                                      "segments": [{"topic": "AI", "script": "Good morning — here is the news; enjoy.",
                                                    "stories": [{"title": "A new model lands — at last"}]}]})
    async with running({CHAT}, NASTY, delay=0.02, briefs=spoken, wall=at(6, 30)) as box:
        for update_id, text in enumerate(["/start", "/schedule", "/schedule 7:00 3", "/schedule banana", QUESTION], start=1):
            box.fake.queue.append(message(update_id, CHAT, text))
            await polled(box, 2)
        await served(box, timeout=20)
        box.fake.queue.extend([message(6, CHAT, "/details"), message(7, CHAT, "/brief 1")])
        await until(lambda: said(box, "Good morning"), timeout=30)
        await until(lambda: any("Exact request" in (row["text"] or "") for row in box.sent("sendMessage")))
        await polled(box, 2)
        texts = ([row["text"] for row in box.sent("sendMessage") + box.sent("editMessageText")]
                 + [row.get("caption") for row in box.fake.got("sendAudio") + box.fake.got("sendPhoto")])
        problems = voice_problems(texts)
        check(f"not one of the {len(texts)} things this bot said has a dash, a dot, an arrow or a semicolon in it",
              not problems and len(texts) > 15, problems[0] if problems else f"{len(texts)} messages and captions scanned")
        check("the words themselves survive: the commas and full stops replace the marks",
              any("no data, sorry" in text for text in texts) and any("87 posts" in text and "quiet day" in text for text in texts)
              and any("What we found, X/Twitter" in text for text in texts)
              and any("about 87 posts" in text for text in texts)
              and any("Good morning, here is the news. Enjoy." in text for text in texts),
              repr(next((text for text in texts if "quiet day" in (text or "")), "")[:70]))
        check("the exact request keeps what was really sent, marks and all",
              any("kept — verbatim; here" in (text or "") for text in texts)
              and voice_problems([text for text in texts if "<pre>" not in (text or "")]) == [],
              "the code block is the one place a dash survives")
        unseen = [text for text in texts if any(mark in str(text or "") for mark in UNSEEN)]
        check("nothing a reader cannot see gets through, not even inside the exact request",
              not unseen and any("gnp.exe" in (text or "") for text in texts),
              f"{len(unseen)} of {len(texts)} carried one" if unseen else "the overrides are gone, the words are not")
    every = [tg.HELP, tg.SETUP, tg.BAD_TOKEN, tg.CONFLICT, tg.UNREACHABLE, tg.PAIRING_HINT, tg.SCHEDULE_HELP,
             tg.BRIEF_OFFLINE, tg.NOTHING_YET, tg.INVALID, tg.pairing_body(CHAT), tg.length_of(1), tg.length_of(3)]
    check("every sentence written into this file is already in that voice",
          not voice_problems(every), (voice_problems(every) or ["all of them"])[0])


# ---- the fatal answers ------------------------------------------------------------------------------

async def conflict_and_bad_token():
    conflict = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
    async with running({CHAT}, start=False, scripted=[("getUpdates", (409, conflict))]) as box:
        code = await asyncio.wait_for(box.bot.run(), timeout=20)
        check("a 409 on getUpdates exits 1 with one clear sentence",
              code == 1 and "Another copy of this bot is already polling" in captured(), f"exit {code}")
    async with running({CHAT}, start=False,
                       scripted=[("getMe", (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}))]) as box:
        code = await asyncio.wait_for(box.bot.run(), timeout=20)
        check("a bad token exits 1 with one clear sentence",
              code == 1 and "Telegram rejected that token" in captured(), f"exit {code}")


def no_token():
    environment = {**os.environ, "TELEGRAM_BOT_TOKEN": " ", "TELEGRAM_ALLOWED_CHAT_IDS": ""}  # a space: setdefault keeps it, strip() empties it
    done = subprocess.run([sys.executable, str(HARNESS / "telegram_bot.py")], env=environment, cwd=str(HARNESS.parent),
                          capture_output=True, text=True, timeout=180)
    sys.stdout.text.append(done.stdout + done.stderr)
    check("with no token the program prints the BotFather steps and exits 1",
          done.returncode == 1 and "@BotFather" in done.stdout and "/newbot" in done.stdout
          and "TELEGRAM_BOT_TOKEN=" in done.stdout and str(HARNESS.parent / ".env") in done.stdout
          and "python -m uv run harness/telegram_bot.py" in done.stdout,
          f"exit {done.returncode}, {len(done.stdout.splitlines())} lines of guidance")


def hand_edited_files():
    """The two files a person opens in a text editor. A typo in either may cost a line, never the bot."""
    root = Path(tempfile.mkdtemp(prefix="tg-edited-"))
    path = root / "chats.json"
    path.write_text(json.dumps({"chats": {
        "--7": {"session_id": "b6d9f0a2-1c3e-4a5b-8d7f-0123456789ab"},  # int() would have raised on this key
        str(CHAT): {"session_id": "../../../pwned", "schedule": {"at": "07:00", "minutes": 2}},
        "nine": {"session_id": "b6d9f0a2-1c3e-4a5b-8d7f-0123456789ab"},
        str(OTHER): "not a record at all"}}), encoding="utf-8")
    try:
        bot = tg.Bot(TOKEN, {CHAT}, None, sessions=root / "sessions", chats_path=path, runner_factory=Runners())
        loaded = dict(bot.saved)  # before chat() maps this chat to a session of its own
        state = bot.chat(CHAT)
        inside = str(state["dir"].resolve()).startswith(str((root / "sessions").resolve()))
        check("a chats.json somebody edited starts the bot, and its session id never becomes a path",
              loaded == {} and bot.plans == {CHAT: {"at": "07:00", "minutes": 1.5, "last": None}}
              and inside and bool(tg.SESSION.fullmatch(state["session"])),
              f"the schedule survived, the traversal did not, and the new session is ...{state['session'][-8:]}")
    except Exception as error:
        check("a chats.json somebody edited starts the bot, and its session id never becomes a path", False, repr(error))
    shutil.rmtree(root, ignore_errors=True)

    before = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS")
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = f"--5, {CHAT}, , abc, -100777"
    try:
        token, allowed = tg.settings()
        check("an allow list with a typo in it loses the typo, not the bot", allowed == {CHAT, -100777}, str(sorted(allowed)))
    except Exception as error:
        check("an allow list with a typo in it loses the typo, not the bot", False, repr(error))
    finally:
        os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = before or ""


def console_encoding():
    """Git Bash prints through cp1252 here, and a detail carrying a preview block character killed the run."""
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    tee, crash = Tee(console), None
    try:
        tee.write(f"PASS  bars {tg.BLOCK * 3}, an ellipsis … and a middot ·\n")
        tee.flush()
    except Exception as error:
        crash = error
    console.flush()
    printed = raw.getvalue().decode("cp1252")
    check("output survives a cp1252 console with non-cp1252 detail in it",
          crash is None and isinstance(sys.stdout, Tee) and tg.BLOCK in "".join(tee.text) and "???" in printed,
          f"crash {crash!r}, console got {printed.strip()[-34:]!r}")


def pure_functions():
    quotes = tg.esc("it's " * 2000)
    cut = tg.clip("<b>t</b>\n" + quotes)
    check("clip trims an over-long escaped body without cutting an entity or leaving a tag open",
          tg.clip("<b>short</b>") == "<b>short</b>" and len(cut) <= tg.MAX_MESSAGE and "…" in cut
          and not re.search(r"&(?!(?:amp|lt|gt|quot|#39|#x27);)", cut) and cut.count("<b>") == cut.count("</b>")
          and tg.clip("<pre>" + quotes).endswith("</pre>") and tg.clip("&#x27;" * 900).endswith(";\n…"),
          f"{len(quotes):,} -> {len(cut):,} characters, ending {cut[-6:]!r}")
    check("split_text prefers paragraphs, then lines, then words",
          tg.split_text("a" * 50, 20) == ["a" * 20, "a" * 20, "a" * 10]
          and tg.split_text("one\n\ntwo", 6) == ["one", "two"] and tg.split_text("hello world again", 12) == ["hello world", "again"],
          str(tg.split_text("a" * 50, 20)))
    check("escaping survives a hostile string, and the plain fallback undoes it",
          tg.rich("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;" and tg.untagged(tg.rich("x **y** <i>")) == "x y <i>",
          repr(tg.untagged(tg.rich("x **y** <i>"))))
    check("bars are scaled to the maximum and capped at 12",
          [tg.bars(count, 100) for count in (0, 1, 50, 100, 400)] == [0, 1, 6, 12, 12],
          str([tg.bars(count, 100) for count in (0, 1, 50, 100, 400)]))
    check("empty and malformed events still render",
          tg.preview_body({"examples": [None], "per_day": "nope"}).startswith("<b>What we found</b>")
          and tg.Turn().body() == "Working…" and tg.number_of(True) is None and tg.text_of(None) == ""
          and tg.chart_body({"bars": "nope"}) == "" and tg.Turn().details() == [] and tg.pack([]) == [],
          repr(tg.preview_body({})))
    check("everything a person reads is written in plain words, mark by mark",
          tg.say("posts - most on Sep 10 · 4,821 · sentiment fell; then it rose") ==
          "posts, most on Sep 10, 4,821, sentiment fell. Then it rose"
          and tg.say("Sep 9 -> Sep 11") == "Sep 9 to Sep 11" and tg.rich("a — **b**") == "a, <b>b</b>",
          repr(tg.say("posts - most on Sep 10 · 4,821 · sentiment fell; then it rose")))
    check("a date reads as Sep 9, a live bucket label is left alone, and a count is plain",
          [tg.day_words(value) for value in ("2026-09-09", "2025-01-02", "19:05", "2026-13-01", None)]
          == ["Sep 9", "Jan 2 2025", "19:05", "2026-13-01", ""]
          and (tg.posts_words(1), tg.posts_words(7221)) == ("1 post", "7,221 posts"),
          str([tg.day_words(value) for value in ("2026-09-09", "2025-01-02", "19:05")]))
    check("a length is the same words as the page, and a step's own length has one decimal",
          [tg.duration_words(ms) for ms in (1000, 22000, 65000, 120000, None, -5)]
          == ["1 second", "22 seconds", "1 minute 5 seconds", "2 minutes", "", ""]
          and [tg.duration_words(ms, exact=True) for ms in (3100, 9960, 1000)] == ["3.1 seconds", "10 seconds", "1 second"],
          str([tg.duration_words(ms) for ms in (22000, 65000, 120000)]))
    paths = ("/api/charts/a.png", "/api/../../secret", "http://evil/api/x.png", "//evil/api/x.png", "api/x.png",
             "/api/%2e%2e/%2e%2e/secret", "/api/x/..%2f..%2fsecret", "/api/x?y=/api/z", "/api/x#/api/y",
             "/api/a:b@evil/x.png", "", None, 7)
    check("only our own API on our own host is ever fetched for a chart",
          [tg.picture_path(value) for value in paths] == ["/api/charts/a.png"] + [""] * 12,
          str([tg.picture_path(value) for value in paths[:3] + paths[5:7]]))
    family = "\U0001f468" + chr(0x200d) + "\U0001f469" + chr(0x200d) + "\U0001f466"  # one emoji, held together by joiners
    arabic = "".join(map(chr, (0x645, 0x631, 0x62d, 0x628, 0x627)))
    check("a character a reader cannot see never reaches the chat, and real writing is untouched",
          tg.say(f"see {FLIP} now") == "see gnp.exe now" and tg.clean(f"a{chr(0x2066)}b{chr(0x2069)}c\x00d\x07e") == "abcde"
          and tg.say(f"{family} {arabic}\ttab\nline") == f"{family} {arabic}\ttab\nline"
          and tg.chart_caption({"title": f"AI {FLIP}", "caption": ""}) == "AI gnp.exe"
          and tg.brief_caption({"title": f"AI {FLIP}", "segments": []}) == "AI gnp.exe",
          repr(tg.say(f"see {FLIP} now")))
    check("a file somebody edited by hand cannot stop the bot from starting",
          tg.CHAT_ID.fullmatch("-100123") and not tg.CHAT_ID.fullmatch("--5") and not tg.CHAT_ID.fullmatch("1e5")
          and bool(tg.SESSION.fullmatch("b6d9f0a2-1c3e-4a5b-8d7f-0123456789ab"))
          and not tg.SESSION.fullmatch("../../../pwned"),
          "a chat id is digits, a session is a uuid, and neither is trusted to be one")
    check("a chart with no picture becomes its own bars, from either shape of row",
          tg.BLOCK * 12 in tg.chart_body({"title": "Posts a day", "bars": [{"label": "2026-09-09", "value": 10},
                                                                           ["2026-09-10", 5]]})
          and "Sep 9" in tg.chart_body({"bars": [{"day": "2026-09-09", "count": 10}]})
          and "<b>Posts a day</b>" in tg.chart_body({"title": "Posts a day", "bars": [["a", 1]]}),
          repr([line for line in tg.chart_body({"bars": [{"label": "2026-09-09", "value": 10}, ["2026-09-10", 5]]}).splitlines()]))
    check("the exact request is printed like the page's own panel, whatever it holds",
          tg.request_text({"tool": "mcp__harness__preview_keywords", "input": {"keywords": ["AI"]}})
          == 'mcp__harness__preview_keywords\n{\n  "keywords": [\n    "AI"\n  ]\n}'
          and tg.request_text(None).startswith("(unknown tool)") and tg.request_text({"input": {1, 2}}).endswith("could not be shown)"),
          repr(tg.request_text({"tool": "x", "input": None})))
    check("details messages hold whole steps, and one too big for a message gets its own",
          tg.pack(["a" * 100, "b" * 100], limit=250) == ["a" * 100 + "\n\n" + "b" * 100]
          and tg.pack(["a" * 200, "b" * 200], limit=250) == ["a" * 200, "b" * 200]
          and tg.pack(["a" * 400], limit=250) == ["a" * 400],
          f"{len(tg.pack(['a' * 200, 'b' * 200], limit=250))} messages for two blocks of 200")
    times = ("7", "7:30 2", "7am", "12am", "9pm 9", "19:05 1min", "24:00", "7:60", "13pm", "soon", "")
    check("a schedule reads clock times, caps the length at ninety seconds and refuses what is not a time",
          [tg.parse_plan(text) for text in times] == [("07:00", 1.5, False), ("07:30", 1.5, True), ("07:00", 1.5, False), ("00:00", 1.5, False),
                                                      ("21:00", 1.5, True), ("19:05", 1, False), None, None, None, None, None],
          str([tg.parse_plan(text) for text in times[:6]]))
    long_brief = {"title": "t" * 400, "segments": [{"stories": [{"title": "s" * 400}] * 40}, "junk", {"stories": None}]}
    check("a caption fits Telegram's 1024 and a malformed brief still renders",
          len(tg.brief_caption(long_brief)) <= 1024 and tg.brief_caption({}) == "Your morning brief" and tg.brief_script({"segments": [None]}) == "",
          f"{len(tg.brief_caption(long_brief))} characters")
    check("a caption is in plain words too, and keeps its list marks",
          tg.brief_caption({"title": "AI labs — day two", "segments": [{"stories": [{"title": "A model lands; at last"}]}]})
          == "AI labs, day two\n- A model lands. At last",
          repr(tg.brief_caption({"title": "AI labs — day two", "segments": [{"stories": [{"title": "A model lands; at last"}]}]})))


async def main():
    await serving_turn()
    await progress_fallback()
    await long_answer()
    await escaping_and_plain_fallback()
    await wedge_recovery()
    await oversized_cards()
    await rate_limit()
    await busy_and_commands()
    await pairing_and_refusal()
    await confirm_flow()
    await details_command()
    await details_flood()
    await chart_cards()
    await hostile_cards()
    await brief_cards()
    await schedule_commands()
    await scheduled_delivery()
    await brief_on_demand()
    await brief_failures()
    await blocked_network()
    await one_voice()
    await conflict_and_bad_token()
    no_token()
    hand_edited_files()
    console_encoding()
    pure_functions()
    leaked = [part for part in (TOKEN, TOKEN.split(":")[1]) if part in captured()]
    check("the token never appears in anything printed", not leaked,
          f"{len(captured()):,} characters of output searched, {len(OUTCOMES)} checks before this one")
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
