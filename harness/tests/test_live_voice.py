# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4"]
# ///
"""Run: python -m uv run harness/tests/test_live_voice.py   (LIVE_TEST_REAL=1 adds one paid check)

Two servers on this laptop: the live talk routes on port 5218, behind the real bridge middleware so
the same-origin rule is the real one, and a fake ElevenLabs the routes are pointed at with
ELEVENLABS_API_BASE. Every check here spends nothing: no real voice service is called unless
LIVE_TEST_REAL is set, and the measurement script is a separate file.

What is checked: every route's happy path and every way it can fail, the cap on how much is read out
in one go, the raised body limit for a recording, the same-origin rule, that a page which hangs up
mid sentence stops us reading the rest of the clip from the service, and that the key appears in no
response body and no line this file prints.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import bridge  # noqa: E402
import live_voice  # noqa: E402
import voice  # noqa: E402

PORT = 5218
BASE = f"http://127.0.0.1:{PORT}"
REAL = os.environ.get("LIVE_TEST_REAL") == "1"
OUTCOMES, BODIES, PRINTED = [], [], []
BANNED = "‒–—―·•‣▪・←→↔⇒➡;"
CLIP = b"\x1aE\xdf\xa3" + b"webm fake clip " * 200  # 3 KB, the size of a second or two of speech
BIG_CLIP = b"\x1aE\xdf\xa3" + b"webm fake clip " * 14000  # 200 KB, well over the chat's own 64 KB limit
FAKE = {"stt_status": 200, "stt_text": "How are people talking about the launch tonight",
        "stt_body": None, "language": "eng", "words": True, "hang": 0.0,
        "tts_status": 200, "tts_mode": "ok", "reject": set(), "seen": [],
        "tts_written": 0, "tts_stopped": False}
SLOW_CHUNKS = 40


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  --  {detail}" if detail else "")
    PRINTED.append(line)
    print(line, flush=True)  # ASCII only: the Windows console is cp1252


def long_sentence(length):
    """Ordinary words of exactly `length` characters, which the cleaner leaves exactly as they are."""
    words = ("posts rose again in every subtopic we watch " * 20)[:length]
    return (words[:-1] + "x") if words.endswith(" ") else words


def plain_enough(text):
    return isinstance(text, str) and bool(text.strip()) and not any(mark in text for mark in BANNED)


def error_of(body):
    return (body or {}).get("error") or {}


# ---------------------------------------------------------------- fake ElevenLabs
async def fake_stt(request):
    form = await request.post()
    field = form.get("file")
    FAKE["seen"].append({"route": "stt", "key": request.headers.get("xi-api-key", ""), "model": form.get("model_id"),
                         "filename": getattr(field, "filename", None), "type": getattr(field, "content_type", None),
                         "bytes": len(field.file.read()) if field is not None else 0})
    if FAKE["hang"]:
        await asyncio.sleep(FAKE["hang"])
    if FAKE["stt_status"] != 200:
        return web.json_response({"detail": "no"}, status=FAKE["stt_status"])
    if FAKE["stt_body"]:  # a one-item list, so "answer with null" is not "answer normally"
        return web.json_response(FAKE["stt_body"][0])
    body = {"text": FAKE["stt_text"], "language_code": FAKE["language"], "language_probability": 0.98}
    if FAKE["words"]:
        body["words"] = [{"text": "How", "start": 0.0, "end": 0.4}, {"text": "tonight", "start": 2.6, "end": 3.12}]
    return web.json_response(body)


async def fake_tts(request):
    payload = await request.json()
    FAKE["seen"].append({"route": "tts", "key": request.headers.get("xi-api-key", ""), "model": payload.get("model_id"),
                         "voice": request.match_info["voice"], "text": payload.get("text"),
                         "format": request.query.get("output_format")})
    if payload.get("model_id") in FAKE["reject"]:
        return web.json_response({"detail": {"status": "model_not_found"}}, status=400)
    if FAKE["hang"]:
        await asyncio.sleep(FAKE["hang"])
    if FAKE["tts_status"] != 200:
        return web.json_response({"detail": "no"}, status=FAKE["tts_status"])
    if FAKE["tts_mode"] == "complaint":  # 200, and a JSON grumble where the sound should be
        return web.json_response({"detail": "quota exhausted"}, status=200)
    if FAKE["tts_mode"] == "silent":  # 200, audio/mpeg, and not one byte of it
        return web.Response(body=b"", content_type="audio/mpeg")
    response = web.StreamResponse()
    response.content_type = "audio/mpeg"
    try:
        await response.prepare(request)
    except (ConnectionResetError, OSError, RuntimeError, aiohttp.ClientError):
        return response  # the page let go before this one even started: nothing to send it
    if FAKE["tts_mode"] == "slow":
        # A long clip, sent a little at a time. A page that hangs up should stop this early.
        FAKE["tts_written"], FAKE["tts_stopped"] = 0, False
        try:
            for _ in range(SLOW_CHUNKS):
                await response.write(b"\xff\xfb\x90d" * 256)
                FAKE["tts_written"] += 1
                await asyncio.sleep(0.05)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError, OSError, RuntimeError):
            FAKE["tts_stopped"] = True
        return response
    for _ in range(3):
        await response.write(b"ID3\x03\x00" + b"\xff\xfb\x90d" * 256)
    await response.write_eof()
    return response


async def serve():
    """The real middleware and the real setup(), so the body limit and the origin rule are not stubbed."""
    app = web.Application(middlewares=[bridge.local_only], client_max_size=64 * 1024)
    app["state"] = {}
    live_voice.setup(app)
    bare = web.Application(client_max_size=64 * 1024)  # no middleware: the module's own rule, alone
    bare["state"] = {}
    live_voice.setup(bare)
    fake = web.Application(client_max_size=32 * 1024 * 1024)
    fake.add_routes([web.post("/v1/speech-to-text", fake_stt), web.post("/v1/text-to-speech/{voice}/stream", fake_tts)])
    runners, ports = [], {}
    for name, application, port in (("app", app, PORT), ("bare", bare, 0), ("fake", fake, 0)):
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        runners.append(runner)
        ports[name] = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    return runners, ports


# ------------------------------------------------------------------- one request
async def call(client, method, path, base=BASE, **keywords):
    async with client.request(method, base + path, **keywords) as response:
        raw = await response.read()
    BODIES.append(raw[:8000].decode("utf-8", "replace"))
    body = None
    if response.content_type == "application/json":
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
    return response.status, response.content_type, raw, body


async def say(client, text, **keywords):
    return await call(client, "POST", "/api/live/say", json={"text": text}, **keywords)


async def listen(client, clip=CLIP, kind="audio/webm", **keywords):
    return await call(client, "POST", "/api/live/listen", data=clip, headers={"Content-Type": kind}, **keywords)


# ----------------------------------------------------------------------- checks
async def status_checks(client):
    code, _, _, body = await call(client, "GET", "/api/live/status")
    check("1a status says live talk is available when a key is configured",
          code == 200 and body.get("available") is True and body.get("reason") == "", str(body)[:120])
    check("1d and says that and nothing else, no voice and no settings of this laptop",
          set(body) == {"available", "reason"}, str(sorted(body)))
    key = os.environ.pop("ELEVENLABS_API_KEY")
    try:
        code, _, _, body = await call(client, "GET", "/api/live/status")
        check("1b with no key it says so in plain words, and the page can hide the button",
              code == 200 and body.get("available") is False and plain_enough(body.get("reason")),
              repr(body.get("reason")))
        code, _, _, spoke = await say(client, "hello")
        code2, _, _, heard = await listen(client)
        check("1c with no key both routes refuse in plain words, naming no file path",
              code == 503 and code2 == 503 and error_of(spoke).get("code") == "no_key"
              and error_of(heard).get("code") == "no_key" and plain_enough(error_of(spoke).get("message"))
              and "\\" not in error_of(spoke).get("message", ""), f"{code}/{code2} {error_of(spoke).get('message')!r}")
    finally:
        os.environ["ELEVENLABS_API_KEY"] = key


async def listen_checks(client):
    FAKE["seen"].clear()
    code, _, _, body = await listen(client, kind="audio/webm;codecs=opus")
    sent = next((row for row in FAKE["seen"] if row["route"] == "stt"), {})
    check("2a one turn of speech comes back as words, the language and how long it was",
          code == 200 and body.get("text") == FAKE["stt_text"] and body.get("language") == "English"
          and body.get("seconds") == 3.12, f"{code} {body}")
    check("2b the turn is forwarded whole, as a file, with the scribe model and the key header",
          sent.get("model") == "scribe_v1" and sent.get("bytes") == len(CLIP) and sent.get("filename") == "turn.webm"
          and sent.get("key") == os.environ["ELEVENLABS_API_KEY"],
          f"{sent.get('model')} {sent.get('filename')} {sent.get('bytes')} bytes")

    code, _, _, body = await listen(client, clip=BIG_CLIP)
    check("2c a long turn is allowed past the chat's own 64 KB limit", code == 200, f"{code} for {len(BIG_CLIP)} bytes")

    code, _, _, body = await listen(client, clip=b"tiny")
    check("2d a button pressed and let go is not a question", code == 400 and error_of(body).get("code") == "no_speech"
          and plain_enough(error_of(body).get("message")), f"{code} {error_of(body).get('message')!r}")

    code, _, _, body = await listen(client, kind="application/json")
    check("2e a sound format we cannot read is one plain sentence",
          code == 415 and error_of(body).get("code") == "bad_recording" and plain_enough(error_of(body).get("message")),
          f"{code} {error_of(body).get('message')!r}")

    code, _, _, body = await listen(client, clip=b"x" * (live_voice.MAX_AUDIO + 1))
    check("2f a recording over ten megabytes is refused before it is read",
          code == 413 and error_of(body).get("code") == "too_long", f"{code} {error_of(body).get('code')}")

    FAKE["stt_status"] = 401
    code, _, _, body = await listen(client)
    check("2g a key the service rejects is one plain sentence, and never the key itself",
          code == 502 and error_of(body).get("code") == "rejected"
          and os.environ["ELEVENLABS_API_KEY"] not in json.dumps(body), f"{code} {error_of(body).get('message')!r}")

    FAKE["stt_status"] = 429
    code, _, _, body = await listen(client)
    check("2h out of credit reads as out of credit", code == 502 and error_of(body).get("code") == "busy"
          and "credit" in error_of(body).get("message", ""), f"{code} {error_of(body).get('message')!r}")
    FAKE["stt_status"] = 200

    FAKE["stt_body"] = [{"detail": "something else entirely"}]
    code, _, _, body = await listen(client)
    check("2i an answer with no words in it never becomes the question we ask the assistant",
          code == 502 and error_of(body).get("code") == "unreachable", f"{code} {error_of(body).get('code')}")
    FAKE["stt_body"] = [{"text": "   "}]
    code, _, _, body = await listen(client)
    check("2j a turn nobody spoke in says so", code == 422 and error_of(body).get("code") == "no_speech",
          f"{code} {error_of(body).get('code')}")
    FAKE["stt_body"] = None

    FAKE["words"] = False
    code, _, _, body = await listen(client)
    check("2k a service that sends no word timings still answers, with no length",
          code == 200 and body.get("seconds") == 0, str(body)[:80])
    FAKE["words"] = True

    FAKE["hang"], live_voice.LISTEN_TIMEOUT = 5.0, 1
    started = time.monotonic()
    code, _, _, body = await listen(client)
    took = time.monotonic() - started
    check("2l a service that never answers is given up on, in one plain sentence",
          code == 502 and error_of(body).get("code") == "unreachable" and took < 4,
          f"{code} after {took:.1f}s: {error_of(body).get('message')!r}")
    FAKE["hang"], live_voice.LISTEN_TIMEOUT = 0.0, 60


async def say_checks(client):
    FAKE["seen"].clear()
    code, kind, raw, _ = await say(client, "Sentiment is steady tonight.")
    sent = next((row for row in FAKE["seen"] if row["route"] == "tts"), {})
    check("3a one sentence comes back as sound", code == 200 and kind == "audio/mpeg" and len(raw) > 1000,
          f"{code} {kind} {len(raw)} bytes")
    check("3b asked for with the fastest model and the smallest mp3 the service streams",
          sent.get("model") == "eleven_flash_v2_5" and sent.get("format") == "mp3_22050_32"
          and sent.get("text") == "Sentiment is steady tonight.", f"{sent.get('model')} {sent.get('format')}")

    FAKE["reject"] = {"eleven_flash_v2_5"}
    FAKE["seen"].clear()
    code, kind, raw, _ = await say(client, "Traction doubled overnight.")
    models = [row.get("model") for row in FAKE["seen"] if row["route"] == "tts"]
    check("3c an account without the fastest model falls back to the one Morning Brief uses",
          code == 200 and kind == "audio/mpeg" and models == ["eleven_flash_v2_5", "eleven_multilingual_v2"],
          f"{code} {models}")
    FAKE["reject"] = set()

    full = long_sentence(live_voice.SAY_LIMIT)
    code, _, _, body = await say(client, full)
    check("3d six hundred characters is still one sentence to read",
          code == 200 and len(voice.speakable(full)) == live_voice.SAY_LIMIT, f"{code} for {len(full)} characters")
    code, _, _, body = await say(client, long_sentence(live_voice.SAY_LIMIT + 1))
    check("3e more than that is refused in plain words, because sentences are short",
          code == 400 and error_of(body).get("code") == "too_much" and plain_enough(error_of(body).get("message")),
          f"{code} {error_of(body).get('message')!r}")

    code, _, _, body = await say(client, "   ")
    check("3f nothing to say is nothing to say", code == 400 and error_of(body).get("code") == "nothing_to_say",
          f"{code} {error_of(body).get('code')}")
    code, _, _, body = await call(client, "POST", "/api/live/say", data=b"text=hello",
                                  headers={"Content-Type": "text/plain"})
    check("3g a request that is not the one this page sends is refused", code == 415, str(code))

    FAKE["tts_status"] = 401
    code, _, _, body = await say(client, "Sentiment is steady.")
    check("3h a key the service rejects is one plain sentence, and never the key itself",
          code == 502 and error_of(body).get("code") == "rejected"
          and os.environ["ELEVENLABS_API_KEY"] not in json.dumps(body), f"{code} {error_of(body).get('message')!r}")
    FAKE["tts_status"] = 429
    code, _, _, body = await say(client, "Sentiment is steady.")
    check("3i out of credit reads as out of credit", code == 502 and error_of(body).get("code") == "busy",
          f"{code} {error_of(body).get('code')}")
    FAKE["tts_status"] = 200

    FAKE["tts_mode"] = "complaint"
    code, kind, raw, body = await say(client, "Sentiment is steady.")
    check("3j a complaint dressed up as sound is never handed to the player as silence",
          code == 502 and kind == "application/json" and error_of(body).get("code") == "unreachable",
          f"{code} {kind}")
    FAKE["tts_mode"] = "silent"
    code, kind, raw, body = await say(client, "Sentiment is steady.")
    check("3k and neither is an answer with no sound in it at all", code == 502 and len(raw) < 400, f"{code} {len(raw)}")
    FAKE["tts_mode"] = "ok"

    FAKE["hang"], live_voice.SAY_TIMEOUT, live_voice.SAY_READ = 5.0, 1, 1
    started = time.monotonic()
    code, _, _, body = await say(client, "Sentiment is steady.")
    took = time.monotonic() - started
    check("3l a service that never answers is given up on, in one plain sentence",
          code == 502 and error_of(body).get("code") == "unreachable" and took < 4,
          f"{code} after {took:.1f}s")
    FAKE["hang"], live_voice.SAY_TIMEOUT, live_voice.SAY_READ = 0.0, 90, 20


async def barge_in_check(client):
    """The person talks over the answer: the page drops the request, and we stop reading the clip."""
    FAKE["tts_mode"] = "slow"
    FAKE["tts_written"], FAKE["tts_stopped"] = 0, False
    response = await client.post(f"{BASE}/api/live/say", json={"text": "A long sentence nobody will hear the end of."})
    first = await response.content.read(64)
    response.close()  # what the page does when the person interrupts: the connection goes
    await asyncio.sleep(1.2)
    written = FAKE["tts_written"]
    check("4a the sound starts arriving before the whole clip is made",
          response.status == 200 and len(first) > 0, f"{response.status}, first {len(first)} bytes")
    check("4b and hanging up stops us reading the rest, so no credit is spent on sound nobody hears",
          written < SLOW_CHUNKS, f"{written} of {SLOW_CHUNKS} pieces read before we let go")
    FAKE["tts_mode"] = "ok"


async def origin_checks(client, bare_port):
    code, _, _, body = await say(client, "Sentiment is steady.", headers={"Origin": "https://example.com"})
    check("5a a page on another site cannot use this laptop's microphone or voice",
          code == 403 and plain_enough((body or {}).get("error") if isinstance((body or {}).get("error"), str)
                                       else error_of(body).get("message")), f"{code} {str(body)[:80]}")
    code, _, _, body = await call(client, "POST", "/api/live/listen", data=CLIP,
                                  headers={"Content-Type": "audio/webm", "Origin": "https://example.com"})
    check("5b and neither can it send a recording", code == 403, str(code))
    # The same rule again without the bridge's middleware, because these routes are also mounted on
    # the combined server and must be safe on any application they are given to.
    bare = f"http://127.0.0.1:{bare_port}"
    code, _, _, body = await call(client, "POST", "/api/live/say", base=bare, json={"text": "hello"},
                                  headers={"Origin": "https://example.com"})
    check("5c the routes keep that rule themselves, on any server they are added to",
          code == 403 and error_of(body).get("code") == "not_ours" and plain_enough(error_of(body).get("message")),
          f"{code} {error_of(body).get('message')!r}")
    code, _, _, body = await call(client, "POST", "/api/live/say", base=bare, json={"text": "Sentiment is steady."})
    check("5d while the page on this laptop is let through", code == 200, str(code))
    # Status is the route that says whether this laptop has a voice, so it keeps the same rule: a
    # page on another site learns nothing at all about this machine from it.
    code, _, _, body = await call(client, "GET", "/api/live/status", headers={"Origin": "https://example.com"})
    check("5e and another site cannot even ask whether this laptop has a voice",
          code == 403 and error_of(body).get("code") == "not_ours", f"{code} {str(body)[:80]}")
    code, _, _, body = await call(client, "GET", "/api/live/status", base=bare)
    check("5f while this page is answered by the routes on their own", code == 200 and "available" in (body or {}),
          f"{code} {str(body)[:60]}")


async def real_check(client):
    """One real round trip, for people who ask for it. The measurement script is the usual way."""
    saved = os.environ["ELEVENLABS_API_BASE"]
    del os.environ["ELEVENLABS_API_BASE"]
    try:
        code, kind, raw, _ = await say(client, "Live check.")
        check("6a the real service speaks", code == 200 and kind == "audio/mpeg" and len(raw) > 2000,
              f"{code} {kind} {len(raw)} bytes")
    finally:
        os.environ["ELEVENLABS_API_BASE"] = saved


def leak_checks():
    key = os.environ["ELEVENLABS_API_KEY"]
    tail = key[-8:]
    check("7a the key is in no answer this server gave", not any(key in body or tail in body for body in BODIES),
          f"{len(BODIES)} bodies read")
    check("7b and in nothing this run printed", not any(key in line or tail in line for line in PRINTED),
          f"{len(PRINTED)} lines")


def prompt_check():
    words = (ROOT / "prompts" / "live.md").read_text(encoding="utf-8")
    check("7c the prompt tells the assistant how a spoken answer has to sound",
          "half a minute" in words and "first sentence" in words.lower() and not any(mark in words for mark in BANNED)
          and len(words.splitlines()) < 40, f"{len(words.splitlines())} lines")
    # Nothing in a question says whether it was spoken or typed, so a rule that only applies when
    # the button is on is a rule the assistant can never know to follow.
    lower = words.lower()
    check("7d and asks for it on every answer, not only when the button happens to be on",
          "every answer" in lower and "when it is on, the person is in a spoken conversation" not in lower,
          f"every answer: {'every answer' in lower}")


async def main():
    if not voice.api_key():
        print("FAIL  no ELEVENLABS_API_KEY in the environment or either .env file", flush=True)
        return 1
    runners, ports = await serve()
    os.environ["ELEVENLABS_API_BASE"] = f"http://127.0.0.1:{ports['fake']}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as client:
            await status_checks(client)
            await listen_checks(client)
            await say_checks(client)
            await barge_in_check(client)
            await origin_checks(client, ports["bare"])
            if REAL:
                await real_check(client)
            leak_checks()
            prompt_check()
    finally:
        for runner in runners:
            await runner.cleanup()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
