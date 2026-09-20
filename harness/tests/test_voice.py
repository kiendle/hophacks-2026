# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_voice.py   (VOICE_TEST_LIVE=0 leaves out the real round trip)

Two servers on this laptop: the voice routes on port 5196, behind the real bridge middleware so the
same-origin rule is the real one, and a fake ElevenLabs the routes are pointed at with
ELEVENLABS_API_BASE. Every check but the last two spends nothing. The last two are one real round trip
of nineteen characters: the routes speak "Signal voice check." and read the MP3 they made back into
words, and the transcript is printed.

Nothing here spends a model turn, and the key is checked for absence from every response body and
every line this file prints.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import bridge  # noqa: E402
import voice  # noqa: E402

PORT, FAKE_PORT = 5196, 5197
BASE, FAKE_BASE = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{FAKE_PORT}"
LIVE = os.environ.get("VOICE_TEST_LIVE", "1") != "0"
OUTCOMES, BODIES, PRINTED = [], [], []
TEMP = Path(tempfile.mkdtemp(prefix="signal-voice-"))
BANNED = "‒–—―·•‣▪・←→↔⇒⇨➡;"
CLIP = b"\x1aE\xdf\xa3" + b"webm fake clip " * 200  # 3 KB, big enough to be a real recording
FAKE = {"stt_status": 200, "stt_text": "How do people talk about A I on Bluesky", "language": "eng",
        "stt_body": None, "tts_status": 200, "tts_mode": "ok", "reject": set(), "seen": []}
# The invisible characters a hostile or careless post can carry: a right-to-left override, a zero
# width space, a bell, a byte order mark and a null. None of them may survive into anything a person
# reads or hears, and the page and the server have to drop exactly the same ones.
INVISIBLE = "\u202e\u202c\u200b\u0007\ufeff\u0000\u2066\u2069"
SAMPLES = [
    "See **this** at https://example.com/x and the post at://did:plc:abc/app.bsky.feed.post/xyz3 as well",
    "Posts rose on Sep 9 - the biggest day; 1,522 of them mentioned safety",
    "Post 1892345678901234567 and hash a3f9c2b1d4e6f8a0b2c4 say the same thing",
    "Most posts \u202ecame on Sep 9\u202c\u200b\u0007 and \ufeffthat is\u0000 that.",
    "<script>alert('x')</script> Posts rose <b>a lot</b> on Sep 9. 5 > 3 and 2 < 4",
    "A\u00a0gap and\u2028a break and\u3000a wide space and a \u2066hidden\u2069 word",
]
STUB = """\
// Stands in for /chat.js: the real plainText and el, plus a way for the driver to reach the
// listener voice.js registered and the messages it sent.
export { plainText, el } from './chat.js';

export const listeners = [];
export const sent = [];

export function onEvent(fn) {
  listeners.push(fn);
  return () => {};
}

export function appendCard(node) {
  return node;
}

export function send(text) {
  sent.push(text);
}

export function emit(event) {
  for (const fn of listeners.slice()) fn(event);
}
"""

DRIVER = """\
// A browser without a browser: enough DOM for chat.js's el() and voice.js's own controls, a stub
// microphone and a stub player, so every click and every spoken line can be checked in node.
// Usage: node drive.mjs on|off   (off = the server says voice is unavailable). Prints one JSON line.
const mode = process.argv[2] || 'on';
const AUDIO_MS = 40;
const fetched = [];
const spoken = [];
const played = [];
const paused = [];
const stopped = [];
let urls = 0;

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.handlers = {};
    this.dataset = {};
    this.style = {};
    this.textContent = '';
    this.hidden = false;
    this.value = '';
  }

  setAttribute(key, value) {
    this.attrs[key] = String(value);
    if (key === 'id') byId.set(String(value), this);
  }

  getAttribute(key) { return key in this.attrs ? this.attrs[key] : null; }
  addEventListener(kind, fn) { (this.handlers[kind] = this.handlers[kind] || []).push(fn); }
  append(...kids) { for (const kid of kids) { kid.parent = this; this.children.push(kid); } }
  insertBefore(node, before) {
    const at = this.children.indexOf(before);
    node.parent = this;
    this.children.splice(at < 0 ? this.children.length : at, 0, node);
  }

  get parentNode() { return this.parent; }
  fire(kind, event = {}) { for (const fn of this.handlers[kind] || []) fn({ button: 0, ...event }); }
}

const byId = new Map();
const head = new Node('head');
const shell = new Node('section');
const composer = new Node('form');
const input = new Node('textarea');
const sendButton = new Node('button');
composer.setAttribute('id', 'composer');
input.setAttribute('id', 'chat-input');
sendButton.setAttribute('id', 'chat-send');
composer.append(input, sendButton);
shell.append(composer);

const docHandlers = {};
globalThis.document = {
  head,
  getElementById: (id) => byId.get(id) || null,
  createElement: (tag) => new Node(tag),
  addEventListener: (kind, fn) => { (docHandlers[kind] = docHandlers[kind] || []).push(fn); },
  querySelector: (selector) => {
    const wanted = /href="([^"]+)"/.exec(selector);
    return wanted ? head.children.find((kid) => kid.tag === 'link' && kid.attrs.href === wanted[1]) || null : null;
  },
};
const store = new Map();
globalThis.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};
globalThis.URL = { createObjectURL: () => `blob:${++urls}`, revokeObjectURL: () => {} };
globalThis.Audio = class {
  constructor(src) { this.src = src; played.push(src); }
  play() {
    this.timer = setTimeout(() => { if (this.onended) this.onended(); }, AUDIO_MS);
    return Promise.resolve();
  }

  pause() { paused.push(this.src); clearTimeout(this.timer); }  // as in a real browser: pausing fires nothing
};
globalThis.MediaRecorder = class {
  static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
  constructor(stream, options = {}) { this.stream = stream; this.mimeType = options.mimeType || ''; }
  start() { this.on = true; }
  stop() {
    if (this.ondataavailable) this.ondataavailable({ data: new Blob([new Uint8Array(4096)], { type: this.mimeType }) });
    if (this.onstop) this.onstop();
  }
};
// node's own navigator is read only, so it is replaced rather than assigned
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => stopped.push(1) }] }) } },
});
globalThis.fetch = async (url, options = {}) => {
  fetched.push({ url, type: (options.headers || {})['Content-Type'] || '' });
  if (url === '/api/voice/status') return json({ available: mode !== 'off', reason: '' });
  if (url === '/api/voice/speak') {
    spoken.push(JSON.parse(options.body).text);
    return { ok: true, status: 200, blob: async () => new Blob([new Uint8Array(2048)], { type: 'audio/mpeg' }) };
  }
  if (url === '/api/voice/transcribe') return json({ text: 'How do people talk about A I right now', language: 'English' });
  return { ok: false, status: 404, json: async () => ({ error: { message: 'no' } }) };
};
const json = (body) => ({ ok: true, status: 200, json: async () => body, blob: async () => new Blob([]) });
const tick = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));
const fireDoc = (kind, event) => { for (const fn of docHandlers[kind] || []) fn(event); };
const text = (id) => (byId.get(id) ? byId.get(id).textContent : null);
const pressed = (id) => (byId.get(id) ? byId.get(id).getAttribute('aria-pressed') : null);

const stub = await import('./stub-chat.js');
await import('./voice.js');
await tick(20);

const out = { mode, controls: Boolean(byId.get('voice-mic')) };
if (mode === 'off') {
  out.css = Boolean(document.querySelector('link[href="/voice.css"]'));
  out.listeners = stub.listeners.length;
  console.log(JSON.stringify(out));
  process.exit(0);
}

// 1. the controls, before anything has been pressed
out.micLabel = text('voice-mic');
out.micPressed = pressed('voice-mic');
out.aloudPressed = pressed('voice-aloud');
out.aloudLabel = text('voice-aloud');
out.stopHidden = byId.get('voice-stop').hidden;
out.css = Boolean(document.querySelector('link[href="/voice.css"]'));
out.live = byId.get('voice-state').getAttribute('aria-live');
out.inComposer = composer.children.map((kid) => kid.attrs.id || kid.tag);
out.barBeforeComposer = shell.children.map((kid) => kid.attrs.id || kid.tag);

// 2. nothing is spoken while the toggle is off
stub.emit({ type: 'turn_start', source: 'message' });
stub.emit({ type: 'step', phase: 'start', id: 's0', title: 'Checking what data we have' });
stub.emit({ type: 'message', text: 'Two sources. Ask away.' });
await tick(60);
out.quietWhenOff = spoken.length === 0;

// 3. the toggle turns it on and is remembered
byId.get('voice-aloud').fire('click');
out.aloudOn = pressed('voice-aloud') === 'true';
out.remembered = localStorage.getItem('signal.voice.aloud');

// 4. step titles are spoken, and only the newest waiting one
stub.emit({ type: 'turn_start', source: 'message' });
out.thinking = text('voice-state');
stub.emit({
  type: 'step', phase: 'start', id: 's1', title: 'Looking at the last 15 minutes of Bluesky',
  why: 'The user asked what is being said right now', facts: [{ label: 'Words searched', value: 'AI, safety' }],
});
await tick(5);
out.speaking = text('voice-state');
stub.emit({ type: 'step', phase: 'start', id: 's2', title: 'Counting posts about AI safety' });
stub.emit({ type: 'step', phase: 'start', id: 's3', title: 'Saving your project' });
stub.emit({ type: 'step', phase: 'end', id: 's1', outcome: 'Found 1,522 posts.', ok: true, ms: 2100 });
await tick(140);
out.steps = spoken.slice();
out.detailsQuiet = !spoken.some((line) => /user asked|Words searched|Found 1,522/.test(line));

// 5. the answer is spoken in order, in pieces
const long = `${'Most posts came on Sep 9. '.repeat(160)}That is the whole answer.`;
stub.emit({ type: 'message', text: long });
await tick(200);
out.answerPieces = spoken.slice(out.steps.length).length;
out.answerInOrder = spoken.slice(out.steps.length).join(' ').startsWith('Most posts came on Sep 9.');
out.answerCapped = spoken.slice(out.steps.length).every((piece) => piece.length <= 900);
stub.emit({ type: 'turn_end' });
await tick(20);

// 6. Stop silences at once: the rest of a long answer is never fetched, and the player is free again
const before = spoken.length;
stub.emit({ type: 'turn_start', source: 'message' });
stub.emit({ type: 'message', text: long });
await tick(10);
out.stopShown = byId.get('voice-stop').hidden === false;
byId.get('voice-stop').fire('click');
await tick(300);
out.stoppedAfter = spoken.length - before;
out.stopSilenced = paused.length > 0;
out.stopHiddenAgain = byId.get('voice-stop').hidden;
const afterStop = spoken.length;
stub.emit({ type: 'step', phase: 'start', id: 's4', title: 'Counting posts again' });
await tick(120);
out.speaksAfterStop = spoken.slice(afterStop);

// 7. Escape also stops speaking
const beforeEscape = spoken.length;
stub.emit({ type: 'message', text: long });
await tick(10);
fireDoc('keydown', { key: 'Escape' });
await tick(200);
out.escapeStopped = spoken.length - beforeEscape < 3;
stub.emit({ type: 'turn_end' });  // the turn opened in section 6 is over: nothing is running now
await tick(10);

// 8. press to talk, press again to send
const mic = byId.get('voice-mic');
mic.fire('pointerdown');
await tick(20);
out.listening = text('voice-state');
out.micRecording = pressed('voice-mic') === 'true';
out.micStopLabel = text('voice-mic');
mic.fire('pointerdown');
await tick(20);
out.transcribeType = (fetched.find((call) => call.url === '/api/voice/transcribe') || {}).type;
out.composerShowed = input.value;
out.sentTooEarly = stub.sent.length;
await tick(700);
out.sent = stub.sent.slice();
out.micBack = text('voice-mic');
out.trackStopped = stopped.length;

// 9. Escape while recording sends nothing
const calls = fetched.filter((call) => call.url === '/api/voice/transcribe').length;
mic.fire('pointerdown');
await tick(20);
fireDoc('keydown', { key: 'Escape' });
await tick(60);
out.cancelled = fetched.filter((call) => call.url === '/api/voice/transcribe').length === calls && stub.sent.length === out.sent.length;

// 10. a new recording stops whatever was playing
stub.emit({ type: 'message', text: long });
await tick(10);
const playing = spoken.length;
const wasPaused = paused.length;
mic.fire('pointerdown');
await tick(150);
out.recordingSilences = spoken.length - playing <= 1 && paused.length > wasPaused;
mic.fire('pointerdown');
await tick(700);
stub.emit({ type: 'turn_end' });
await tick(10);

// 11. a question asked while the assistant is still answering is not thrown away in silence
const beforeBusy = stub.sent.length;
stub.emit({ type: 'turn_start', source: 'message' });
mic.fire('pointerdown');
await tick(20);
mic.fire('pointerdown');
await tick(700);
out.busySent = stub.sent.length - beforeBusy;
out.busySaid = text('voice-state');
out.busyKept = input.value;
stub.emit({ type: 'turn_end' });

out.everyRequest = [...new Set(fetched.map((call) => call.url))].sort();
console.log(JSON.stringify(out));
"""


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else "")
    PRINTED.append(line)
    print(line, flush=True)  # ASCII: the Windows console is cp1252


def plain_enough(text):
    return isinstance(text, str) and bool(text.strip()) and not any(mark in text for mark in BANNED)


# ---------------------------------------------------------------- fake ElevenLabs
async def fake_stt(request):
    form = await request.post()
    field = form.get("file")
    FAKE["seen"].append({"route": "stt", "key": request.headers.get("xi-api-key", ""), "model": form.get("model_id"),
                         "filename": getattr(field, "filename", None), "type": getattr(field, "content_type", None),
                         "bytes": len(field.file.read()) if field is not None else 0})
    if FAKE["stt_status"] != 200:
        return web.json_response({"detail": "no"}, status=FAKE["stt_status"])
    if FAKE["stt_body"]:  # a one-item list, so that "answer with null" is not "answer normally"
        return web.json_response(FAKE["stt_body"][0])
    return web.json_response({"text": FAKE["stt_text"], "language_code": FAKE["language"], "language_probability": 0.98})


async def fake_tts(request):
    payload = await request.json()
    FAKE["seen"].append({"route": "tts", "key": request.headers.get("xi-api-key", ""), "model": payload.get("model_id"),
                         "voice": request.match_info["voice"], "text": payload.get("text"),
                         "format": request.query.get("output_format")})
    if payload.get("model_id") in FAKE["reject"]:
        return web.json_response({"detail": {"status": "model_not_found", "message": "no such model"}}, status=400)
    if FAKE["tts_status"] != 200:
        return web.json_response({"detail": "no"}, status=FAKE["tts_status"])
    if FAKE["tts_mode"] == "complaint":  # 200, and a JSON grumble where the sound should be
        return web.json_response({"detail": "quota exhausted"}, status=200)
    if FAKE["tts_mode"] == "silent":  # 200, audio/mpeg, and not one byte of it
        return web.Response(body=b"", content_type="audio/mpeg")
    response = web.StreamResponse()
    response.content_type = "audio/mpeg"
    await response.prepare(request)
    for _ in range(3):
        await response.write(b"ID3\x03\x00" + b"\xff\xfb\x90d" * 256)
    await response.write_eof()
    return response


async def serve():
    """The real middleware and the real setup(), so the body limit and the origin rule are not stubbed."""
    app = web.Application(middlewares=[bridge.local_only], client_max_size=64 * 1024)
    app["state"] = {}
    voice.setup(app)
    fake = web.Application()
    fake.add_routes([web.post("/v1/speech-to-text", fake_stt), web.post("/v1/text-to-speech/{voice}/stream", fake_tts)])
    runners = []
    for application, port in ((app, PORT), (fake, FAKE_PORT)):
        runner = web.AppRunner(application)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)
    return runners


# ------------------------------------------------------------------- one request
async def call(client, method, path, **keywords):
    async with client.request(method, BASE + path, **keywords) as response:
        raw = await response.read()
    BODIES.append(raw[:8000].decode("utf-8", "replace"))
    body = None
    if response.content_type == "application/json":
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
    return response.status, response.content_type, raw, body


def error_of(body):
    return (body or {}).get("error") or {}


async def audio(client, path, payload, **keywords):
    return await call(client, "POST", path, json=payload, **keywords)


# ----------------------------------------------------------------------- checks
async def status_checks(client):
    code, _, _, body = await call(client, "GET", "/api/voice/status")
    check("1a status says voice is available when a key is configured", code == 200 and body == {"available": True, "reason": ""}, str(body))
    key = os.environ.pop("ELEVENLABS_API_KEY")
    try:
        code, _, _, body = await call(client, "GET", "/api/voice/status")
        check("1b with no key, status says so in plain words", code == 200 and body.get("available") is False and plain_enough(body.get("reason"))
              and "microphone" in body.get("reason", ""), repr(body.get("reason")))
        code, _, _, body = await audio(client, "/api/voice/speak", {"text": "hello"})
        speak_error = error_of(body)
        code2, _, _, body2 = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
        check("1c with no key, both routes refuse in plain words and name no file paths",
              code == 503 and code2 == 503 and speak_error.get("code") == "no_key" and error_of(body2).get("code") == "no_key"
              and plain_enough(speak_error.get("message")) and "\\" not in speak_error.get("message", ""),
              f"{code}/{code2} {speak_error.get('message')!r}")
    finally:
        os.environ["ELEVENLABS_API_KEY"] = key


async def transcribe_checks(client):
    FAKE["seen"].clear()
    code, kind, _, body = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm;codecs=opus"})
    sent = next((row for row in FAKE["seen"] if row["route"] == "stt"), {})
    check("2a a recording comes back as the words that were said, and the language in words",
          code == 200 and body.get("text") == FAKE["stt_text"] and body.get("language") == "English", f"{code} {body}")
    check("2b the clip is forwarded whole, as a file, with the scribe model and the key header",
          sent.get("model") == "scribe_v1" and sent.get("bytes") == len(CLIP) and sent.get("filename") == "question.webm"
          and sent.get("type") == "audio/webm" and sent.get("key") == os.environ["ELEVENLABS_API_KEY"],
          f"{sent.get('model')} {sent.get('filename')} {sent.get('bytes')} bytes, key forwarded: {sent.get('key') == os.environ['ELEVENLABS_API_KEY']}")

    FAKE["stt_status"] = 401
    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
    refused = error_of(body)
    check("2c a key the service rejects is one plain sentence, and never the key itself",
          code == 502 and refused.get("code") == "rejected" and plain_enough(refused.get("message"))
          and os.environ["ELEVENLABS_API_KEY"] not in json.dumps(body), f"{code} {refused.get('message')!r}")

    FAKE["stt_status"] = 429
    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
    busy = error_of(body)
    check("2d out of credit reads as out of credit", code == 502 and busy.get("code") == "busy" and plain_enough(busy.get("message"))
          and "credit" in busy.get("message", ""), f"{code} {busy.get('message')!r}")
    FAKE["stt_status"] = 200

    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=b"{}", headers={"Content-Type": "text/plain"})
    wrong = error_of(body)
    check("2e a body that is not sound is refused before anything is sent on",
          code == 415 and wrong.get("code") == "bad_recording" and plain_enough(wrong.get("message")), f"{code} {wrong.get('message')!r}")

    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=b"tiny", headers={"Content-Type": "audio/webm"})
    empty = error_of(body)
    check("2f a button pressed and let go is not sent on either", code == 400 and empty.get("code") == "no_speech"
          and plain_enough(empty.get("message")), f"{code} {empty.get('message')!r}")

    FAKE["stt_text"] = "   "
    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
    heard = error_of(body)
    check("2g a recording with no words in it says so", code == 422 and heard.get("code") == "no_speech"
          and plain_enough(heard.get("message")), f"{code} {heard.get('message')!r}")
    FAKE["stt_text"] = "How do people talk about A I on Bluesky"

    oversized, refusal = b"0" * (voice.MAX_AUDIO + 512 * 1024), None
    try:
        code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=oversized, headers={"Content-Type": "audio/webm"})
        refusal = (code, error_of(body).get("code"), error_of(body).get("message"))
    except aiohttp.ClientError as error:  # the server may close the connection before the upload ends
        refusal = ("closed", "too_long", repr(error)[:80])
    check("2h a recording over ten megabytes is refused", refusal[1] == "too_long" and refusal[0] in (413, "closed"),
          f"{refusal[0]} {refusal[2]!r}")
    check("2i nothing oversized ever reached the service", all(row.get("bytes", 0) <= voice.MAX_AUDIO for row in FAKE["seen"]),
          f"{len(FAKE['seen'])} calls, largest {max((row.get('bytes', 0) for row in FAKE['seen']), default=0)} bytes")

    # A service that answers 200 with the wrong shape used to reach .get() on a string and fall over,
    # so the person got a page of server English instead of a sentence.
    garbled = []
    for body in ([1, 2, 3], "nope", None, {"text": {"deep": "value"}}, {"text": 42}, {"nothing": "here"}):
        FAKE["stt_body"] = [body]
        code, kind, _, answer = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
        garbled.append((code, kind, error_of(answer).get("code"), error_of(answer).get("message")))
    FAKE["stt_body"] = None
    check("2j a service answering nonsense is one plain sentence, never a crash",
          all(code == 502 and kind == "application/json" and plain_enough(message) for code, kind, _, message in garbled),
          f"{[row[0] for row in garbled]} {garbled[0][3]!r}")

    FAKE["stt_text"] = f"Most posts{INVISIBLE} came on Sep 9"
    code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=CLIP, headers={"Content-Type": "audio/webm"})
    said = (body or {}).get("text", "")
    check("2k the invisible characters in a recording never reach the question",
          code == 200 and not any(mark in said for mark in INVISIBLE) and "Sep 9" in said, f"{code} {said!r}")
    FAKE["stt_text"] = "How do people talk about A I on Bluesky"


async def speak_checks(client):
    FAKE["seen"].clear()
    code, kind, raw, _ = await audio(client, "/api/voice/speak", {"text": "Most posts landed on Sep 9. See https://example.com/x for the rest."})
    sent = next((row for row in FAKE["seen"] if row["route"] == "tts"), {})
    check("3a text comes back as streamed audio", code == 200 and kind == "audio/mpeg" and len(raw) > 1000 and raw.startswith(b"ID3"),
          f"{code} {kind} {len(raw)} bytes")
    check("3b the fast model, the default voice and mp3 are what was asked for",
          sent.get("model") == voice.FAST_MODEL and sent.get("voice") == voice.DEFAULT_VOICE and sent.get("format") == "mp3_44100_128",
          f"{sent.get('model')} voice {sent.get('voice')} {sent.get('format')}")
    check("3c the web address is taken off before anything is spoken",
          "https" not in (sent.get("text") or "") and "Sep 9" in (sent.get("text") or ""), repr(sent.get("text")))

    briefing = (ROOT.parent / "morning-brief/briefing.py").read_text(encoding="utf-8")
    named = re.search(r'^DEFAULT_VOICE = "([^"]+)"', briefing, re.M)
    check("3d the default voice is the same one Morning Brief speaks with", bool(named) and named.group(1) == voice.DEFAULT_VOICE,
          f"briefing.py {named.group(1) if named else None} vs voice.py {voice.DEFAULT_VOICE}")

    FAKE["seen"].clear()
    code, _, raw, _ = await audio(client, "/api/voice/speak", {"text": "Ask again.", "voice_id": "AbC123_-xyzQWERTY"})
    asked = [row["voice"] for row in FAKE["seen"] if row["route"] == "tts"]
    code2, _, _, _ = await audio(client, "/api/voice/speak", {"text": "Ask again.", "voice_id": "../../etc/passwd"})
    asked2 = [row["voice"] for row in FAKE["seen"] if row["route"] == "tts"]
    check("3e a voice can be chosen, and a made-up one falls back to the default",
          code == 200 and asked[0] == "AbC123_-xyzQWERTY" and code2 == 200 and asked2[-1] == voice.DEFAULT_VOICE, f"{asked2}")

    FAKE["seen"].clear()
    FAKE["reject"] = {voice.FAST_MODEL}
    code, kind, raw, _ = await audio(client, "/api/voice/speak", {"text": "Read this out."})
    tried = [row["model"] for row in FAKE["seen"] if row["route"] == "tts"]
    check("3f an account without the fast model still speaks, with the slower one",
          code == 200 and kind == "audio/mpeg" and tried == [voice.FAST_MODEL, voice.SLOW_MODEL], f"{code} tried {tried}")
    FAKE["reject"] = set()

    code, _, _, body = await audio(client, "/api/voice/speak", {"text": "x" * (voice.MAX_TEXT + 50)})
    long = error_of(body)
    check("3g more than the cap in one request is refused in plain words",
          code == 400 and long.get("code") == "too_much" and plain_enough(long.get("message")), f"{code} {long.get('message')!r}")

    code, _, _, body = await audio(client, "/api/voice/speak", {"text": "   "})
    nothing = error_of(body)
    code2, _, _, body2 = await audio(client, "/api/voice/speak", {"text": "https://example.com/only-a-link"})
    check("3h nothing to say is refused, including text that was only a link",
          code == 400 and nothing.get("code") == "nothing_to_say" and code2 == 400 and error_of(body2).get("code") == "nothing_to_say",
          f"{code}/{code2} {nothing.get('message')!r}")

    FAKE["tts_status"] = 429
    code, _, _, body = await audio(client, "/api/voice/speak", {"text": "Read this out."})
    FAKE["tts_status"] = 200
    check("3i a service out of credit is one sentence a person can act on", code == 502 and error_of(body).get("code") == "busy"
          and plain_enough(error_of(body).get("hint")), f"{code} {error_of(body).get('hint')!r}")

    saved = os.environ["ELEVENLABS_API_BASE"]
    os.environ["ELEVENLABS_API_BASE"] = "http://127.0.0.1:5999"  # nothing listens there
    code, _, _, body = await audio(client, "/api/voice/speak", {"text": "Read this out."})
    os.environ["ELEVENLABS_API_BASE"] = saved
    check("3j a service that cannot be reached is not a stack trace", code == 502 and error_of(body).get("code") == "unreachable"
          and plain_enough(error_of(body).get("message")), f"{code} {error_of(body).get('message')!r}")

    # 200 and no sound is the worst failure of the lot: the player would simply say nothing and the
    # person would never learn why. Both shapes have to come back as words instead.
    quiet = []
    for mode in ("complaint", "silent"):
        FAKE["tts_mode"] = mode
        code, kind, raw, body = await audio(client, "/api/voice/speak", {"text": "Read this out."})
        quiet.append((mode, code, kind, len(raw), error_of(body).get("message")))
    FAKE["tts_mode"] = "ok"
    check("3k a service that answers with no sound at all says so in words",
          all(code == 502 and kind == "application/json" and plain_enough(message) for _, code, kind, _, message in quiet),
          " | ".join(f"{mode} {code} {kind}" for mode, code, kind, _, _ in quiet))

    FAKE["seen"].clear()
    code, _, _, _ = await audio(client, "/api/voice/speak",
                                {"text": f"<script>alert('x')</script> Most{INVISIBLE} posts came on <b>Sep 9</b>."})
    asked = next((row["text"] for row in FAKE["seen"] if row["route"] == "tts"), "")
    check("3l tags and invisible characters are never sent on to be read out",
          code == 200 and "script" not in asked and "<" not in asked and ">" not in asked
          and not any(mark in asked for mark in INVISIBLE) and "Sep 9" in asked, repr(asked))

    started = time.monotonic()
    code, _, _, body = await call(client, "POST", "/api/voice/speak", data=json.dumps({"text": "a " * 400_000}).encode(),
                                  headers={"Content-Type": "application/json"})
    took = (time.monotonic() - started) * 1000
    check("3m a body far bigger than a sentence is refused before it is read",
          code == 400 and error_of(body).get("code") == "too_much" and took < 1500, f"{code} in {took:.0f} ms")

    async def dribble():  # sent in pieces, so there is no length on it to look at
        for _ in range(40):
            yield b'{"text": "' + b"a " * 20_000 + b'",'
    code, _, _, body = await call(client, "POST", "/api/voice/speak", data=dribble(),
                                  headers={"Content-Type": "application/json"})
    check("3n and so is one sent in pieces, with no length on it at all",
          code == 400 and error_of(body).get("code") == "too_much" and plain_enough(error_of(body).get("message")),
          f"{code} {error_of(body).get('message')!r}")


async def origin_checks(client):
    code, _, _, body = await audio(client, "/api/voice/speak", {"text": "Read this out."}, headers={"Origin": "http://evil.example"})
    check("4a another site cannot use this laptop's voice", code == 403, f"{code} {str(body)[:80]}")
    code, _, _, _ = await call(client, "POST", "/api/voice/transcribe", data=CLIP,
                               headers={"Content-Type": "audio/webm", "Origin": "http://evil.example"})
    check("4b and cannot send it a recording either", code == 403, str(code))


def pure_checks():
    text = " ".join(f"Sentence number {index} about A I posts." for index in range(40))
    pieces = voice.chunks(text, 120)
    check("5a long text is cut between sentences, in order, with nothing lost",
          all(len(piece) <= 120 for piece in pieces) and " ".join(pieces) == text and len(pieces) > 4,
          f"{len(pieces)} pieces, longest {max(len(piece) for piece in pieces)}")
    check("5b every piece ends on a full stop", all(piece.endswith(".") for piece in pieces), repr(pieces[0][-30:]))
    giant = voice.chunks("word " * 60, 50)
    check("5c one sentence longer than a piece is cut at a space, never inside a word",
          all(len(piece) <= 50 for piece in giant) and all("word" == part for piece in giant for part in piece.split()),
          f"{len(giant)} pieces, longest {max(len(piece) for piece in giant)}")
    check("5d nothing in, nothing out", voice.chunks("") == [] and voice.chunks(None) == [], repr(voice.chunks("")))

    spoken = voice.speakable(SAMPLES[0])
    check("5e links, post addresses and stars are taken off before speaking",
          "https" not in spoken and "at://" not in spoken and "*" not in spoken and "this" in spoken, repr(spoken))
    ids = voice.speakable(SAMPLES[2])
    check("5f long ids and hashes are never read out", not re.search(r"\d{12,}|[0-9a-f]{16,}", ids), repr(ids))
    dashes = voice.speakable(SAMPLES[1])
    check("5g what is spoken obeys the plain punctuation rule", plain_enough(dashes) and "Sep 9" in dashes, repr(dashes))
    check("5h a language code becomes a language name", voice.language_name("eng") == "English"
          and voice.language_name("ja") == "Japanese" and voice.language_name("zz9") == "", repr(voice.language_name("eng")))

    hidden = voice.speakable(SAMPLES[3])
    check("5i the characters that hide themselves and turn a line around are taken out",
          not any(mark in hidden for mark in INVISIBLE) and "Sep 9" in hidden and plain_enough(hidden), repr(hidden))
    tagged = voice.speakable(SAMPLES[4])
    check("5j a page's own markup is never read out as words", "script" not in tagged and "<" not in tagged
          and ">" not in tagged and "Sep 9" in tagged, repr(tagged))
    start = time.monotonic()
    silly = [voice.chunks("word " * 20, limit) for limit in (0, -5, 1, None, "eight")]
    check("5k a splitting width of nothing gives up instead of going round for ever",
          all(isinstance(pieces, list) and pieces for pieces in silly) and (time.monotonic() - start) < 2,
          f"{[len(pieces) for pieces in silly]} pieces in {(time.monotonic() - start) * 1000:.0f} ms")
    os.environ["ELEVENLABS_VOICE_ID"] = "../../v1/history"
    poisoned = voice.voice_of(None)
    os.environ["ELEVENLABS_VOICE_ID"] = "Av6DEFgh_-1234"
    chosen = voice.voice_of(None)
    del os.environ["ELEVENLABS_VOICE_ID"]
    check("5l a voice name from the settings is checked like any other, so none of it lands in a web address",
          poisoned == voice.DEFAULT_VOICE and chosen == "Av6DEFgh_-1234", f"{poisoned} then {chosen}")


def stage():
    """voice.js, the real chat.js beside it and a stub in between, so node can import all three.

    chat.js is the real file on purpose: it starts nothing without a document, which is how the page's
    own plainText and el are the ones under test. Only the import specifier is rewritten.
    """
    if not shutil.which("node"):
        return False
    (TEMP / "chat.js").write_bytes((ROOT / "web/chat.js").read_bytes())
    (TEMP / "stub-chat.js").write_text(STUB, encoding="utf-8")
    (TEMP / "voice.js").write_text((ROOT / "web/voice.js").read_text(encoding="utf-8")
                                   .replace("from '/chat.js'", "from './stub-chat.js'"), encoding="utf-8")
    return True


def node_checks():
    source = (ROOT / "web/voice.js").read_text(encoding="utf-8")
    imported = re.search(r"import \{([^}]*)\} from '/chat\.js'", source)
    names = sorted(name.strip() for name in (imported.group(1) if imported else "").split(",") if name.strip())
    check("6a voice.js builds on the contract's exports and nothing else",
          names and set(names) <= {"onEvent", "appendCard", "send", "plainText", "el"}, str(names))
    check("6b no HTML strings anywhere in it", "innerHTML" not in source and "outerHTML" not in source
          and "insertAdjacentHTML" not in source, "textContent only")
    check("6c it loads its own stylesheet instead of editing index.html", "href: '/voice.css'" in source, "/voice.css")
    check("6d it hides itself when the server says voice is off", "/api/voice/status" in source and "if (!ready" in source, "status first")

    if not stage():
        check("6e node runs the two pure helpers", False, "node is not on the PATH")
        return
    (TEMP / "run.mjs").write_text(
        "import { speakable, splitForSpeech, SPEAK_LIMIT } from './voice.js';\n"
        f"const samples = {json.dumps(SAMPLES)};\n"
        "const long = Array.from({ length: 40 }, (unused, index) => `Sentence number ${index} about A I posts.`).join(' ');\n"
        "const pieces = splitForSpeech(long, 120);\n"
        "const giant = splitForSpeech('word '.repeat(60), 50);\n"
        "const silly = [0, -5, 1, null, 'eight'].map((width) => splitForSpeech('word '.repeat(20), width).length);\n"
        "console.log(JSON.stringify({ limit: SPEAK_LIMIT, pieces, giant, silly, spoken: samples.map(speakable),\n"
        "  rejoined: pieces.join(' ') === long, empty: splitForSpeech('').length }));\n", encoding="utf-8")
    try:  # a splitter that never finishes must fail this run, not hang it
        done = subprocess.run([shutil.which("node"), "run.mjs"], cwd=str(TEMP), capture_output=True, text=True,
                              encoding="utf-8", timeout=60)  # node writes UTF-8; the console here is cp1252
    except subprocess.TimeoutExpired:
        check("6e node runs the two pure helpers", False, "node never came back")
        return
    if done.returncode != 0:
        check("6e node runs the two pure helpers", False, (done.stderr or done.stdout)[:300])
        return
    out = json.loads(done.stdout.strip().splitlines()[-1])
    check("6e node runs the two pure helpers", out["limit"] == voice.SPEAK_LIMIT and out["rejoined"] and out["empty"] == 0
          and all(len(piece) <= 120 for piece in out["pieces"]), f"{len(out['pieces'])} pieces, limit {out['limit']}")
    check("6f the page splits text exactly as the server does",
          out["pieces"] == voice.chunks(" ".join(f"Sentence number {index} about A I posts." for index in range(40)), 120)
          and out["giant"] == voice.chunks("word " * 60, 50), f"{len(out['giant'])} pieces in the giant sentence")
    check("6g the page strips what must not be spoken exactly as the server does",
          out["spoken"] == [voice.speakable(sample) for sample in SAMPLES],
          " | ".join(f"{a!r} vs {b!r}" for a, b in zip(out["spoken"], [voice.speakable(s) for s in SAMPLES]) if a != b)[:300] or "identical")
    check("6h the page's splitter also gives up on a width of nothing",
          all(count > 0 for count in out["silly"]), f"{out['silly']} pieces")


def frozen_checks():
    """The approved shape of an activity row, read out of chat.js, which this stream never edits.

    A closed row is the state mark, the title and a "details" link, and nothing else. Why, the
    result sentence, the plain facts and the exact request only exist once details is pressed. This
    is here because a voice plug-in inserts nodes into the same page, and because a second pair of
    eyes should notice the day another stream takes the link away.
    """
    source = (ROOT / "web/chat.js").read_text(encoding="utf-8")
    row = source[source.find("function stepRow("):source.find("function endStepRow(")]
    panel = source[source.find("function openStepPanel("):source.find("function closeStepPanel(")]
    opens = row.find("el('div', { class: 'step-head' }")  # "failure," appears earlier, in the row object
    head = row[opens:row.find("details)", opens) + len("details)")] if opens >= 0 else ""
    check("11a every step row still gets a details link",
          "class: 'link-button step-details'" in row and "text: 'details'" in row and "aria-expanded" in row,
          "details button built in stepRow")
    check("11b a closed row is the mark, the title and that link, and nothing more",
          "step-mark" in head and "step-title" in head and "details)" in head
          and not any(word in head for word in ("step-why", "step-outcome", "step-fact", "step-raw")),
          " ".join(re.findall(r"class: '([a-z-]+)'", head)) + " + details")
    check("11c why, the result, the facts and the exact request open under details",
          all(word in panel for word in ("step-why", "step-outcome", "step-facts", "Exact request")),
          "all four are inside openStepPanel")
    check("11d a step that did not work still says so on the row itself",
          "row.ok === false ? row.outcome" in source and "panel.hidden" in source, "showFailure keeps the one result line")
    check("11e nothing this stream owns touches an activity row",
          not any(word in (ROOT / "web/voice.js").read_text(encoding="utf-8")
                  for word in ("step-details", "step-panel", "activity", "step-title")),
          "voice.js names no part of the activity list")


def prompt_checks():
    path = ROOT / "prompts/voice.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    check("7a the agent is told that some people listen instead of reading", bool(text) and "listen" in text.lower()
          and "short sentences" in text.lower(), f"{len(text.split())} words")
    check("7b the fragment obeys the plain punctuation rule itself", plain_enough(text) and len(text.splitlines()) < 40,
          f"{len(text.splitlines())} lines")


def drive(mode):
    (TEMP / "drive.mjs").write_text(DRIVER, encoding="utf-8")
    try:
        done = subprocess.run([shutil.which("node"), "drive.mjs", mode], cwd=str(TEMP), capture_output=True, text=True,
                              encoding="utf-8", timeout=180)  # node writes UTF-8; the console here is cp1252
    except subprocess.TimeoutExpired:
        return {"broke": "the page never finished the run"}
    if done.returncode != 0:
        return {"broke": (done.stderr or done.stdout or "node gave no reason")[:400]}
    return json.loads(done.stdout.strip().splitlines()[-1])


def browser_checks():
    """voice.js driven for real, with a stub page, a stub microphone and a stub player.

    Every button is pressed and every event of a turn is handed to it, so what the page does with a
    step, an answer, Stop and Escape is checked rather than read.
    """
    if not stage():
        check("8 voice.js is driven under node", False, "node is not on the PATH")
        return
    off = drive("off")
    check("8a with voice switched off the page grows no controls at all",
          off.get("controls") is False and off.get("css") is False and off.get("listeners") == 0, str(off))
    out = drive("on")
    if out.get("broke"):
        check("8 voice.js is driven under node", False, out["broke"])
        return
    check("8b the microphone sits between the box and Send, the bar above the composer, the stylesheet loaded",
          out["inComposer"] == ["chat-input", "voice-mic", "chat-send"] and out["barBeforeComposer"] == ["voice-bar", "composer"]
          and out["css"] and out["live"] == "polite", f"{out['inComposer']} / {out['barBeforeComposer']}")
    check("8c the buttons read as buttons, pressed or not", out["micLabel"] == "Talk" and out["micPressed"] == "false"
          and out["aloudLabel"] == "Read answers aloud" and out["aloudPressed"] == "false" and out["stopHidden"] is True,
          f"{out['micLabel']}, {out['aloudLabel']}")
    check("8d with the toggle off, a whole turn is silent", out["quietWhenOff"] is True, "no speak request")
    check("8e the toggle turns reading aloud on and is remembered", out["aloudOn"] and out["remembered"] == "yes", str(out["remembered"]))
    check("8f each step is read out, and only the newest one that was waiting",
          out["steps"] == ["Looking at the last 15 minutes of Bluesky", "Saving your project"], str(out["steps"]))
    check("8g nothing from a details panel is ever read out, only the step's own title",
          out["detailsQuiet"] is True, "no why, no facts, no result line")
    check("8h the wait says Thinking, then Speaking", out["thinking"] == "Thinking" and out["speaking"] == "Speaking",
          f"{out['thinking']} to {out['speaking']}")
    check("8i a long answer is read in order, in pieces under the cap",
          out["answerPieces"] >= 4 and out["answerInOrder"] and out["answerCapped"], f"{out['answerPieces']} pieces")
    check("8j Stop silences it at once and never fetches the rest",
          out["stopShown"] and out["stopSilenced"] and out["stoppedAfter"] <= 2 and out["stopHiddenAgain"],
          f"{out['stoppedAfter']} of {out['answerPieces']} pieces were fetched")
    check("8k and the voice still works on the next step after a Stop", out["speaksAfterStop"] == ["Counting posts again"],
          str(out["speaksAfterStop"]))
    check("8l Escape stops the reading too", out["escapeStopped"] is True, "silenced")
    check("8m press to talk, press again, and the question is sent as a message",
          out["listening"] == "Listening" and out["micRecording"] and out["micStopLabel"] == "Stop"
          and out["composerShowed"] == "How do people talk about A I right now" and out["sentTooEarly"] == 0
          and out["sent"] == ["How do people talk about A I right now"] and out["micBack"] == "Talk",
          f"heard {out['composerShowed']!r}, sent {len(out['sent'])}")
    check("8n the clip goes up as the sound format the browser recorded", out["transcribeType"] == "audio/webm;codecs=opus",
          out["transcribeType"])
    check("8o the microphone is handed back after every recording", out["trackStopped"] >= 1, f"{out['trackStopped']} times")
    check("8p Escape while recording sends nothing at all", out["cancelled"] is True, "nothing transcribed, nothing sent")
    check("8q starting a recording silences whatever was playing", out["recordingSilences"] is True, "silenced")
    check("8r the page calls nothing but its own three routes",
          out["everyRequest"] == ["/api/voice/speak", "/api/voice/status", "/api/voice/transcribe"], str(out["everyRequest"]))
    check("8s a question asked while the assistant is still answering is kept, not lost in silence",
          out["busySent"] == 0 and plain_enough(out["busySaid"]) and "still answering" in out["busySaid"]
          and out["busyKept"] == "How do people talk about A I right now",
          f"{out['busySaid']!r}, still in the box: {out['busyKept']!r}")


async def live_checks(client):
    """One real round trip: nineteen characters spoken, then read back. This is the only paid check."""
    saved = os.environ["ELEVENLABS_API_BASE"]
    del os.environ["ELEVENLABS_API_BASE"]
    try:
        code, kind, raw, _ = await audio(client, "/api/voice/speak", {"text": "Signal voice check."})
        check("9a the real service speaks", code == 200 and kind == "audio/mpeg" and len(raw) > 4000 and raw[:3] in (b"ID3", b"\xff\xfb\x90"),
              f"{code} {kind} {len(raw)} bytes")
        if code != 200:
            return
        code, _, _, body = await call(client, "POST", "/api/voice/transcribe", data=raw, headers={"Content-Type": "audio/mpeg"})
        said = (body or {}).get("text", "")
        check("9b and hears it back", code == 200 and "signal" in said.lower() and "voice" in said.lower(),
              f"{code} heard {said!r} in {(body or {}).get('language')!r}")
    finally:
        os.environ["ELEVENLABS_API_BASE"] = saved


def leak_checks():
    key = os.environ["ELEVENLABS_API_KEY"]
    tail = key[-8:]
    check("10a the key is in no answer this server gave", not any(key in body or tail in body for body in BODIES),
          f"{len(BODIES)} bodies read")
    check("10b and in nothing this run printed", not any(key in line or tail in line for line in PRINTED), f"{len(PRINTED)} lines")


async def main():
    if not voice.api_key():
        print("FAIL  no ELEVENLABS_API_KEY in the environment or either .env file", flush=True)
        return 1
    os.environ["ELEVENLABS_API_BASE"] = FAKE_BASE
    runners = await serve()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as client:
            await status_checks(client)
            await transcribe_checks(client)
            await speak_checks(client)
            await origin_checks(client)
            pure_checks()
            node_checks()
            frozen_checks()
            prompt_checks()
            browser_checks()
            if LIVE:
                await live_checks(client)
            else:
                print("SKIP  9 the live round trip (VOICE_TEST_LIVE=0)", flush=True)
            leak_checks()
    finally:
        for runner in runners:
            await runner.cleanup()
        shutil.rmtree(TEMP, ignore_errors=True)
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
