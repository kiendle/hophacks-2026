"""Talk live: the ears and the mouth of a spoken conversation with the local Claude.

harness/web/live.js keeps one microphone open, works out when the person has stopped speaking and
posts that one turn to /api/live/listen to become words, which it then sends into the same chat
session as a typed message. While the assistant answers, the page posts each finished sentence to
/api/live/say and plays it, so the answer is heard while it is still being written. The assistant is
the only brain in the room: nothing here writes a word of what is said.

Everything about ElevenLabs is voice.py's and is imported from it, not copied: the key loading, the
voice, the plain sentence each kind of failure turns into. This file adds only what a live
conversation needs and the push-to-talk routes do not have: how long the clip was, the fastest model
the account will give us, bytes passed straight through to the page as they arrive, and an upstream
read that is dropped the moment the person interrupts.

bridge.load_plugins imports this file and calls setup(app) once "live_voice" is in bridge.PLUGINS.
"""
import asyncio
import contextlib

import aiohttp
from aiohttp import web

import steps
import voice

FLASH = "eleven_flash_v2_5"  # about 75 ms inside the service; an account without it falls back to voice.py's models
OUTPUT = "mp3_22050_32"  # the smallest mp3 the service streams: fewer bytes on the wire, sooner in the ear
SAY_LIMIT = 600  # one sentence at a time, so the page never waits on a paragraph
MAX_AUDIO = voice.MAX_AUDIO
MIN_AUDIO = voice.MIN_AUDIO
LISTEN_TIMEOUT = 60  # read at call time, so a test can shorten every one of these
SAY_TIMEOUT = 90
SAY_READ = 20
CONNECT_TIMEOUT = 10
GONE = (ConnectionResetError, RuntimeError, OSError, asyncio.CancelledError)  # the page went away mid sentence


def models():
    """The fastest first, then whatever voice.py would have used, with no name twice."""
    return list(dict.fromkeys([FLASH, *voice.models()]))


def local(request):
    """bridge.local_only's rule, kept here too so these routes are safe on any application."""
    return (request.url.host in ("127.0.0.1", "localhost")
            and request.headers.get("Origin", str(request.url.origin())) == str(request.url.origin()))


def not_ours():
    return voice.fail(403, "not_ours", "Only this page on this laptop can use the microphone.",
                      "Open the app at its own address and try again.")


def too_much():
    return voice.fail(400, "too_much", f"That is too much to read out in one go, over {SAY_LIMIT} characters.",
                      "Send one sentence at a time.")


def clip_seconds(heard):
    """How long the person spoke, from the word timings the service sends back. 0 if it sent none."""
    words = heard.get("words") if isinstance(heard, dict) else None
    ends = [word.get("end") for word in words if isinstance(word, dict)] if isinstance(words, list) else []
    return round(max((float(end) for end in ends if isinstance(end, (int, float))), default=0.0), 2)


# ------------------------------------------------------------------- routes
async def status(request):
    """Whether this laptop has a voice at all. Same rule as the other two: this page, or nobody."""
    if not local(request):
        return not_ours()
    ready = bool(voice.api_key())
    return web.json_response({"available": ready, "reason": "" if ready else voice.NO_KEY}, headers=voice.HEADERS)


async def listen(request):
    """One turn of speech becomes the words to send into the chat, and how long it took to say them."""
    if not local(request):
        return not_ours()
    if not voice.api_key():
        return voice.no_key()
    kind = (request.content_type or "").lower()
    if kind not in voice.AUDIO_TYPES:
        return voice.fail(415, "bad_recording", "That recording is in a sound format we cannot read.",
                          "Start the live talk again and speak after the panel says Listening.")
    if (request.content_length or 0) > MAX_AUDIO:
        return voice.too_long()  # refused before the body is read, so a huge upload costs this laptop nothing
    # /api/live/ is not /api/voice/, so bridge.local_only leaves the chat composer's 64 KB on this
    # request. The limit is read when the body is read, so raising it here raises it for this one.
    with contextlib.suppress(AttributeError):
        request._client_max_size = MAX_AUDIO  # noqa: SLF001
    try:
        clip = await request.read()
    except (web.HTTPException, ValueError, asyncio.TimeoutError):
        return voice.too_long()
    if len(clip) > MAX_AUDIO:
        return voice.too_long()
    if len(clip) < MIN_AUDIO:
        return voice.fail(400, "no_speech", "We did not hear anything in that recording.",
                          "Speak a little louder, or move closer to the microphone.")

    form = aiohttp.FormData()
    form.add_field("file", clip, filename=f"turn.{voice.AUDIO_TYPES[kind]}", content_type=kind)
    form.add_field("model_id", voice.STT_MODEL)
    try:
        async with voice.http(request.app).post(f"{voice.api_base()}/v1/speech-to-text", data=form,
                                                headers={"xi-api-key": voice.api_key()},
                                                timeout=aiohttp.ClientTimeout(total=LISTEN_TIMEOUT,
                                                                              sock_connect=CONNECT_TIMEOUT)) as answer:
            if answer.status != 200:
                await answer.read()
                return voice.upstream_failed(answer.status)
            heard = await answer.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return voice.unreachable()

    # Anything but an object with the words in it is a broken service, not a quiet room, and must
    # never become the question the page is about to ask the assistant.
    words = heard.get("text") if isinstance(heard, dict) else None
    if not isinstance(words, str):
        return voice.upstream_failed(0)
    said = steps.plain(" ".join(voice.readable(words).split()))[:4000]
    if not said:
        return voice.fail(422, "no_speech", "We could not make out any words there.",
                          "Say it again, a little closer to the microphone.")
    return web.json_response({"text": said, "language": voice.language_name((heard or {}).get("language_code")),
                              "seconds": clip_seconds(heard)}, headers=voice.HEADERS)


async def say(request):
    """One sentence in, mp3 out, written to the page as it arrives so the first words start at once."""
    if not local(request):
        return not_ours()
    if not voice.api_key():
        return voice.no_key()
    if (request.content_length or 0) > voice.MAX_BODY:
        return too_much()
    with contextlib.suppress(AttributeError):
        request._client_max_size = voice.MAX_BODY  # noqa: SLF001  (a sentence is never a megabyte)
    payload, refused = await voice.body_of(request)
    if refused is not None:
        return refused
    text = voice.speakable(payload.get("text"))
    if not text:
        return voice.fail(400, "nothing_to_say", "There was nothing to read out.", "Send the words to speak.")
    if len(text) > SAY_LIMIT:
        return too_much()

    chosen, session, wanted, upstream = voice.voice_of(payload.get("voice_id")), voice.http(request.app), models(), None
    try:
        for index, model in enumerate(wanted):
            upstream = await session.post(
                f"{voice.api_base()}/v1/text-to-speech/{chosen}/stream", params={"output_format": OUTPUT},
                headers={"xi-api-key": voice.api_key()}, json={"text": text, "model_id": model},
                timeout=aiohttp.ClientTimeout(total=SAY_TIMEOUT, sock_connect=CONNECT_TIMEOUT, sock_read=SAY_READ))
            if upstream.status == 200:
                break
            refused_model, status = upstream.status in (400, 403, 404, 422), upstream.status
            upstream.close()
            upstream = None
            if refused_model and index + 1 < len(wanted):
                continue  # this account cannot use the fastest model: ask for the next one
            return voice.upstream_failed(status)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return voice.unreachable()

    # A service having a bad day answers 200 and then sends a JSON complaint, or nothing at all.
    # Either handed to the player is silence the person cannot explain, so it is only called audio
    # once real bytes are on their way, and is a sentence they can read otherwise.
    if (upstream.content_type or "").startswith(("application/json", "text/")):
        upstream.close()
        return voice.upstream_failed(0)
    response, interrupted = None, False
    try:
        async for piece in upstream.content.iter_any():  # iter_any, not a fixed size: bytes go out as they land
            transport = request.transport
            if transport is None or transport.is_closing():
                interrupted = True  # the person barged in and the page dropped the request
                break
            if response is None:
                response = web.StreamResponse(headers=voice.HEADERS)
                response.content_type = "audio/mpeg"
                await response.prepare(request)
            await response.write(piece)
    except GONE:
        interrupted = True
    finally:
        # A dropped connection is the point: draining the rest of the clip would go on reading sound
        # nobody will hear, so the interrupted case closes the socket instead of releasing it.
        if interrupted or not upstream.content.at_eof():
            upstream.close()
        else:
            upstream.release()
    if response is None:
        return web.Response(status=499, headers=voice.HEADERS) if interrupted else voice.upstream_failed(0)
    with contextlib.suppress(*GONE):
        await response.write_eof()
    return response


def setup(app):
    """Called by bridge.load_plugins once "live_voice" is in bridge.PLUGINS, and by signal_server.py through it."""
    state = app.get("state")
    if not isinstance(state, dict):
        state = {}
        app["state"] = state
    state.setdefault("voice", {})  # one ClientSession for both voice routes and live ones
    app.on_cleanup.append(voice.close)  # harmless twice: the second call finds the session already closed
    app.add_routes([
        web.get("/api/live/status", status),
        web.post("/api/live/listen", listen),
        web.post("/api/live/say", say),
    ])
    return app
