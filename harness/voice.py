"""Voice for the chat: the page records a question, this turns it into words and speaks the answer.

bridge.load_plugins imports this file if it is there and calls setup(app), which adds three routes:
POST /api/voice/transcribe (a recorded clip in, plain text out), POST /api/voice/speak (text in, MP3
out) and GET /api/voice/status, which is how harness/web/voice.js decides whether to show the
microphone at all. Nothing else in the harness imports this file, so a laptop with no ElevenLabs key
simply has no voice.

Both providers are ElevenLabs: speech to text with scribe_v1, speech with the fast model and the
slower one morning-brief uses as a fallback. The key is read from the environment or from either .env
file and is never written into a response, a log line or an error message.
"""
import asyncio
import contextlib
import os
import re
from pathlib import Path

import aiohttp
from aiohttp import web

import steps

ROOT = Path(__file__).resolve().parent
ENV_FILES = (ROOT.parent / ".env", ROOT.parent / "morning-brief/.env")
DEFAULT_BASE = "https://api.elevenlabs.io"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # the same voice morning-brief/briefing.py calls DEFAULT_VOICE
STT_MODEL = "scribe_v1"
FAST_MODEL = "eleven_flash_v2_5"  # a few hundred milliseconds; the fallback below is what Morning Brief uses
SLOW_MODEL = "eleven_multilingual_v2"
MAX_AUDIO = 10 * 1024 * 1024  # bridge.local_only raises the body limit to the same number for /api/voice/
MIN_AUDIO = 256  # a clip smaller than this is a button pressed and let go, not a question
MAX_TEXT = 1200  # the page splits an answer into pieces well under this
MAX_BODY = 64 * 1024  # the chat composer's own limit: a speak request is a sentence, never a file
SPEAK_LIMIT = 900
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
AUDIO_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/m4a": "m4a",
               "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3"}
VOICE_ID = re.compile(r"[A-Za-z0-9_-]{8,48}")
NO_KEY = "Voice is switched off on this laptop, so the microphone is not available."
LANGUAGES = {"en": "English", "eng": "English", "ja": "Japanese", "jpn": "Japanese", "es": "Spanish", "spa": "Spanish",
             "pt": "Portuguese", "por": "Portuguese", "ko": "Korean", "kor": "Korean", "fr": "French", "fra": "French",
             "de": "German", "deu": "German", "tr": "Turkish", "tur": "Turkish", "ar": "Arabic", "ara": "Arabic",
             "it": "Italian", "ita": "Italian", "zh": "Chinese", "zho": "Chinese", "cmn": "Chinese",
             "ru": "Russian", "rus": "Russian", "nl": "Dutch", "nld": "Dutch", "hi": "Hindi", "hin": "Hindi"}

# What must never be read out loud: a web address, a post address, a long id, a tag, and the stars
# and backticks that are markup on screen and noise in the ear. _CONTROL goes first and matters most:
# a post can carry a right-to-left override or a zero width space, and a transcript built from one
# would reorder the chat line a person reads. Tab and newline survive it, because the bullet rule
# below still needs the line starts. harness/web/voice.js holds the same list, character for
# character, and a test compares the two on the same text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_TAG = re.compile(r"</?[A-Za-z][^<>]{0,200}>")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_AT_URI = re.compile(r"\bat://\S+", re.I)
_LONG_ID = re.compile(r"\b(?:[0-9a-f]{16,}|\d{12,})\b", re.I)
_MARKS = re.compile(r"[*_`#><]+")
_BULLET = re.compile(r"(?m)^[ \t]*[-*][ \t]+")
_EMPTY_BRACKET = re.compile(r"[(\[]\s*[,.]?\s*[)\]]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def load_env():
    """Keys live in the repo's own .env files, never in the code. First file to set a name wins."""
    for path in ENV_FILES:
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            key, separator, value = line.partition("=")
            if separator and value.strip() and not key.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()


def api_base():
    """Read per call, so a test can point the two routes at a fake ElevenLabs and spend nothing."""
    return (os.environ.get("ELEVENLABS_API_BASE") or DEFAULT_BASE).rstrip("/")


def api_key():
    return (os.environ.get("ELEVENLABS_API_KEY") or "").strip()


def voice_of(value):
    """The asked-for voice, then the one in the environment, then ours. Each is checked: the id goes
    into the web address of the request, so a name with a slash or a dot in it never gets that far."""
    for wanted in (value, os.environ.get("ELEVENLABS_VOICE_ID")):
        if isinstance(wanted, str) and VOICE_ID.fullmatch(wanted.strip()):
            return wanted.strip()
    return DEFAULT_VOICE


def models():
    """The fast one first. A voice account that has not been given it falls back to the slower one."""
    chosen = [FAST_MODEL, os.environ.get("ELEVENLABS_MODEL") or SLOW_MODEL]
    return list(dict.fromkeys(name for name in chosen if name))


def language_name(code):
    return LANGUAGES.get(str(code or "").strip().lower().replace("_", "-").split("-")[0], "")


def readable(text):
    """Text with the characters that are invisible on a page but change what it says taken out."""
    return _CONTROL.sub("", text) if isinstance(text, str) else ""


def speakable(text):
    """The answer with everything nobody wants read out loud taken off it."""
    if not isinstance(text, str):
        return ""
    clean = _TAG.sub(" ", readable(text))
    clean = _URL.sub(" ", clean)
    clean = _AT_URI.sub(" ", clean)
    clean = _LONG_ID.sub(" ", clean)
    clean = _BULLET.sub("", clean)
    clean = _MARKS.sub("", clean)
    clean = _EMPTY_BRACKET.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return steps.plain(clean).strip()


def chunks(text, limit=SPEAK_LIMIT):
    """Pieces of at most `limit` characters, split between sentences, in order. Never splits a word."""
    try:
        limit = max(1, int(limit))  # a limit of nothing would cut nothing off and loop for ever
    except (TypeError, ValueError):
        limit = SPEAK_LIMIT
    parts, current = [], ""
    for sentence in _SENTENCE.split(str(text or "").strip()):
        while len(sentence) > limit:  # one sentence longer than a whole piece: cut it at a space
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    return [part for part in parts + [current] if part]


def fail(status, code, message, hint):
    return web.json_response({"error": {"code": code, "message": message, "hint": hint}}, status=status, headers=HEADERS)


def no_key():
    return fail(503, "no_key", NO_KEY, "Put ELEVENLABS_API_KEY in the .env file next to the app, then start the server again.")


def too_long():
    return fail(413, "too_long", "That recording is too long. Keep a question under about ten minutes.",
                "Ask a shorter question and try again.")


def too_much():
    return fail(400, "too_much", f"That is too much to read out in one go, over {MAX_TEXT:,} characters.",
                f"Send it in pieces of about {SPEAK_LIMIT} characters.")


def upstream_failed(status):
    """One plain sentence per way the voice service can say no. The key itself is never quoted back."""
    if status in (401, 403):
        return fail(502, "rejected", "The voice service did not accept this laptop's key.",
                    "Check ELEVENLABS_API_KEY in the .env file, then start the server again.")
    if status == 429:
        return fail(502, "busy", "The voice service is out of credit for now, or too many requests arrived at once.",
                    "Wait a moment and try again, or read the answer on screen.")
    return fail(502, "unreachable", "The voice service could not do that just now.", "Try again in a moment.")


def unreachable():
    return fail(502, "unreachable", "We could not reach the voice service from this laptop.",
                "Check the internet connection and try again.")


async def body_of(request):
    if request.content_type != "application/json":
        return None, fail(415, "bad_request", "That request was not sent the way this page sends it.", "Send it as JSON.")
    try:
        payload = await request.json()
    except web.HTTPException:  # a body sent in pieces, with no length to check up front
        return None, too_much()
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return None, fail(400, "bad_request", "That request was not sent the way this page sends it.", "Send a JSON object.")
    return payload, None


def http(app):
    """One client per server. Made on the first call, so importing this file opens nothing."""
    pool = app["state"]["voice"]
    session = pool.get("session")
    if session is None or session.closed:
        session = pool["session"] = aiohttp.ClientSession()
    return session


# ------------------------------------------------------------------- routes
async def status(request):
    ready = bool(api_key())
    return web.json_response({"available": ready, "reason": "" if ready else NO_KEY}, headers=HEADERS)


async def transcribe(request):
    """A recorded clip becomes the words the person said, which the page then sends as a message."""
    if not api_key():
        return no_key()
    kind = (request.content_type or "").lower()
    if kind not in AUDIO_TYPES:
        return fail(415, "bad_recording", "That recording is in a sound format we cannot read.",
                    "Record it again with the microphone button in the chat.")
    if (request.content_length or 0) > MAX_AUDIO:
        return too_long()  # refused before the body is read, so a huge upload costs this laptop nothing
    try:
        clip = await request.read()
    except (web.HTTPException, ValueError, asyncio.TimeoutError):  # a body over the limit is raised as an HTTP 413
        return too_long()
    if len(clip) > MAX_AUDIO:
        return too_long()
    if len(clip) < MIN_AUDIO:
        return fail(400, "no_speech", "We did not hear anything in that recording.",
                    "Hold the microphone button while you talk, then let go.")

    form = aiohttp.FormData()
    form.add_field("file", clip, filename=f"question.{AUDIO_TYPES[kind]}", content_type=kind)
    form.add_field("model_id", STT_MODEL)
    try:
        async with http(request.app).post(f"{api_base()}/v1/speech-to-text", data=form,
                                          headers={"xi-api-key": api_key()},
                                          timeout=aiohttp.ClientTimeout(total=90)) as answer:
            if answer.status != 200:
                await answer.read()
                return upstream_failed(answer.status)
            heard = await answer.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return unreachable()

    # The service is supposed to answer with an object holding the words. Anything else is a broken
    # service, not a quiet recording, and must never become the question the person is about to ask.
    words = heard.get("text") if isinstance(heard, dict) else None
    if not isinstance(words, str):
        return upstream_failed(0)
    said = steps.plain(" ".join(readable(words).split()))[:4000]
    if not said:
        return fail(422, "no_speech", "We could not make out any words in that recording.",
                    "Try again in a quieter place, and speak for a second or two.")
    return web.json_response({"text": said, "language": language_name((heard or {}).get("language_code"))}, headers=HEADERS)


async def speak(request):
    """Text in, MP3 out, streamed while it is made so the first words start almost at once."""
    if not api_key():
        return no_key()
    # The recording routes share one raised body limit, so this one puts its own back: a sentence to
    # read is never a megabyte, whether it arrives with a length on it or in pieces without one.
    if (request.content_length or 0) > MAX_BODY:
        return too_much()
    with contextlib.suppress(AttributeError):
        request._client_max_size = MAX_BODY  # noqa: SLF001  (the same handle bridge.local_only uses)
    payload, refused = await body_of(request)
    if refused is not None:
        return refused
    text = speakable(payload.get("text"))
    if not text:
        return fail(400, "nothing_to_say", "There was nothing to read out.", "Send the words to speak.")
    if len(text) > MAX_TEXT:
        return too_much()

    voice, session, wanted, upstream = voice_of(payload.get("voice_id")), http(request.app), models(), None
    try:
        for index, model in enumerate(wanted):
            upstream = await session.post(
                f"{api_base()}/v1/text-to-speech/{voice}/stream", params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": api_key()}, json={"text": text, "model_id": model},
                timeout=aiohttp.ClientTimeout(total=180, sock_read=60))
            if upstream.status == 200:
                break
            refused_model, status = upstream.status in (400, 403, 404, 422), upstream.status
            upstream.close()
            if refused_model and index + 1 < len(wanted):
                continue  # this account cannot use the fast model: ask for the slower one
            return upstream_failed(status)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return unreachable()

    # A service having a bad day can answer 200 and then send a JSON complaint, or nothing at all.
    # Handing either of those to the player as sound is silence the person cannot explain, so the
    # answer is only called audio once real bytes are on their way, and otherwise it is a sentence.
    if (upstream.content_type or "").startswith(("application/json", "text/")):
        upstream.close()
        return upstream_failed(0)
    response = None
    try:
        async for piece in upstream.content.iter_chunked(16 * 1024):
            if response is None:
                response = web.StreamResponse(headers=HEADERS)
                response.content_type = "audio/mpeg"
                await response.prepare(request)
            await response.write(piece)
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError, RuntimeError, OSError):
        pass  # the page closed the player, or the service stopped mid sentence: the bytes so far still play
    finally:
        upstream.release()
    if response is None:
        return upstream_failed(0)
    await response.write_eof()
    return response


async def close(app):
    session = ((app.get("state") or {}).get("voice") or {}).get("session")
    if session is not None and not session.closed:
        await session.close()


def setup(app):
    """Called by bridge.load_plugins, and by signal_server.py through the same function."""
    state = app.get("state")
    if not isinstance(state, dict):
        state = {}
        app["state"] = state
    state.setdefault("voice", {})
    app.on_cleanup.append(close)
    app.add_routes([
        web.get("/api/voice/status", status),
        web.post("/api/voice/transcribe", transcribe),
        web.post("/api/voice/speak", speak),
    ])
    return app
