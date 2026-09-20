# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/telegram_bot.py

Telegram is a second front end onto the same agent as the web chat (harness/DESIGN.md §0, §4): this
process long-polls the Bot API from the laptop (no server, no public URL, no API key), hands each
message to the local Claude Code through claude_runner.py, and renders its event stream back as one
live-edited status message plus the answer. The Confirm button posts here, never to the model: this
process writes the confirmation record with the same rules as bridge.py, and submit_project checks it.

It is also where the morning brief is delivered: /schedule keeps a time and a length per chat, a timer
orders the brief from the Morning Brief server on this laptop (the one brief_tools.py talks to) and the
recording is uploaded into the chat. No model call is involved, so it costs only the voice.

Everything a person reads here goes through steps.plain, exactly like the web chat, so the two front
ends speak with one voice. The status message is as quiet as the website's rows (a mark and a title,
nothing else); Telegram has no panel to open, so /details is that panel, sent on request.

mcp, duckdb and pytz are declared above although this file does not import them: the MCP server runs
on this interpreter (claude_runner.mcp_config), so uv must install them into it.
"""
import asyncio
import contextlib
import html
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines() if (REPO / ".env").exists() else []:
    key, separator, value = line.partition("=")
    if separator and value.strip() and not key.lstrip().startswith("#"):
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

import steps  # noqa: E402  (the one place plain punctuation is decided, shared with the web chat)
from claude_runner import Runner  # noqa: E402  (reads the environment loaded above, exactly as bridge.py drives it)
from steps import plain  # noqa: E402

SESSIONS = ROOT / "state/sessions"  # the layout the web bridge uses, so the MCP tools behave identically
CHATS = ROOT / "state/telegram/chats.json"
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
TURN_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT", 300))
MAX_TURNS = 2  # at most two Claude Code processes at once: one laptop, one subscription (DESIGN.md §0)
EDIT_INTERVAL = 1.2  # Telegram rate-limits edits; updates in between are coalesced
TYPING_INTERVAL = 4.0  # the typing bubble lasts about five seconds
REFUSAL_INTERVAL = 3600.0
POLL_TIMEOUT = 50
CONNECT_WAIT_S = 10.0  # the first wait after Telegram could not be reached; it doubles up to a minute
MAX_MESSAGE = 4000
DETAIL_STEPS = 20  # /details on a phone: past this it arrives as a flood of messages, not as a list
HOURGLASS, OK, FAILED = "⏳", "✓", "!"  # the marks the website draws on its own rows (chat.css .step-mark)
BLOCK = "█"
BAR_ROWS = 16  # as many bars as a phone shows without wrapping; the scale is the rows on screen
CONFIRMATION = re.compile(r"[A-Za-z0-9_-]{16}")  # checked before any path is built from it
CHAT_ID = re.compile(r"-?[0-9]{1,19}")  # int() on anything else raises, and a typo may not stop the bot
SESSION = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")  # a uuid, before it is a path
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
TAG = re.compile(r"<[^>]*>")
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# Characters nobody can see but every renderer obeys: a right to left override makes the rest of a
# message read backwards, so a post can disguise a link or a name as something else. The controls,
# then the bidi embeddings, overrides and isolates. Tabs, newlines and every real letter stay, and
# so do the joiners that hold an emoji together. Written as numbers: a file holding the characters
# themselves would read backwards in an editor, which is the very trick this table is here for.
INVISIBLE = dict.fromkeys((*range(0x00, 0x09), 0x0b, 0x0c, *range(0x0e, 0x20), 0x7f,
                           *range(0x202a, 0x202f), *range(0x2066, 0x206a)))
NETWORK = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

BRIEF_BASE = (os.environ.get("SIGNAL_BASE_URL") or "http://127.0.0.1:5194").rstrip("/")  # the server brief_tools.py talks to
# A chart's picture is served by the chat server, which is 5195 on its own and the same port as the
# briefs once the combined server is switched on; one SIGNAL_BASE_URL then points both at it.
CARD_BASE = (os.environ.get("SIGNAL_BASE_URL") or "http://127.0.0.1:5195").rstrip("/")
# Our own API on our own host, nothing else. No percent escape is allowed in it: aiohttp decodes and
# normalises the path it is given, so "/api/%2e%2e/%2e%2e/x" leaves /api/ and asks the server for /x.
CARD_PATH = re.compile(r"/api/[A-Za-z0-9._~/-]{1,300}")
CARD_BRIEF_WAIT_S = 180.0  # a brief card is followed for three minutes, then the chat is told where it will be
CARD_FETCH_S = 15.0  # a chart picture is worth a few seconds of waiting, never the answer behind it
MAX_PICTURE = 10 * 1024 * 1024  # Telegram refuses a photo over 10 MB anyway
BRIEF_AT, BRIEF_HOURS = "07:00", 24  # the daily brief: seven in the morning, about the last day
BRIEF_MINUTES = 3  # the default and the most: the voice is paid per character (brief_tools.CHAT_MAX_SECONDS)
BRIEF_GRACE_S = 2 * 3600  # a laptop asleep at seven still delivers if it wakes within two hours
BRIEF_WAIT_S = 600.0  # writing and recording take 30 to 90 seconds; past ten minutes something is stuck
BRIEF_POLL_S = 5.0
BRIEF_BUSY_S, BRIEF_BUSY_TRIES = 15.0, 12  # the server makes one brief at a time: wait for the other one
SCHEDULE_TICK = 30.0
BRIEF_ID = re.compile(r"\d{8}-\d{6}")  # the server's own patterns, checked before a path is built from them
BRIEF_FILE = re.compile(r"[a-z0-9-]+\.mp3")
PLAN = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s+(\d{1,2})\s*(?:m|min|mins|minute|minutes)?)?", re.I)

SETUP = f"""No TELEGRAM_BOT_TOKEN yet, so there is no bot to run. Making one takes about two minutes:

  1. Open Telegram (phone or desktop) and search for  @BotFather , the account with the blue tick.
  2. Send him:  /newbot
  3. He asks for a name. Type anything, for example:  Signal
  4. He asks for a username. It has to end in  bot , for example:  kaan_signal_bot
  5. He answers with a token that looks like  1234567890:AAH...  . Copy it.
  6. Open this file in any text editor:

       {REPO / ".env"}

     and add one line at the end, with no spaces around the = sign:

       TELEGRAM_BOT_TOKEN=paste-the-token-here

  7. Save the file and run this again:

       python -m uv run harness/telegram_bot.py

Keep that token to yourself: anyone who has it can act as your bot."""

BAD_TOKEN = f"""Telegram rejected that token (401 Unauthorized). Open {REPO / ".env"} and check that
TELEGRAM_BOT_TOKEN is the whole line BotFather sent, the digits, the colon and the letters, with
nothing else on the line. If you are not sure, send @BotFather /revoke and use the new token."""

CONFLICT = """Another copy of this bot is already polling Telegram with the same token (HTTP 409).
Close that window, or stop the other machine, and start me again. Only one may run at a time."""

UNREACHABLE = ("Telegram cannot be reached from this network. I will keep trying every minute. "
               "On the Hopkins Wi-Fi it is blocked: use a phone hotspot.")

PAIRING_HINT = f"""Pairing mode: nobody is allowed to talk to me yet, so I will not ask the model anything.
Send the bot any message in Telegram and it will reply with the line to add to {REPO / ".env"}."""

HELP = """I watch what people are saying about AI and answer questions about it in plain words.

Ask me things like:
- What are people saying about AI on Bluesky right now?
- How did people react to the Anthropic resignation post around Sep 9?
- What was said about GPT-6 Astra in early September?

I come back with counts, real posts and a Confirm button before anything is started.

/details show what I just did, step by step
/schedule get a spoken brief here every morning, for example /schedule 7:00 3
/brief make a brief now and send it here
/new forget this conversation and start a fresh one
/id show this chat's ID
/help show this message"""

NOTHING_YET = "I have not done anything yet. Ask me a question first, then send /details."

INVALID = "That confirmation is no longer valid. Ask for a new one."

SCHEDULE_HELP = f"""You have no morning brief yet. Send a time and a length in minutes, for example:

/schedule 7:00 3

Every day at that time I will send a recording here about what happened in the last {BRIEF_HOURS} hours on the topics you follow. The length can be 1 to {BRIEF_MINUTES} minutes. Send /schedule off to stop."""

BRIEF_OFFLINE = ("Morning Brief is not running on the laptop, so there was nothing to record. "
                 "Start it, then send /brief to try again.")


def clean(value):
    """Untrusted text: the characters that are not there to be read are dropped before anyone reads it."""
    return str(value).translate(INVISIBLE)


def esc(value):
    """Untrusted text: every character escaped, no markup of any kind survives."""
    return html.escape(clean(value))


def say(value):
    """Anything we write for a person: plain punctuation first (steps.plain), then escaped.

    The only text that skips this is someone else's exact request inside /details, which is the
    panel the website opens, and the padded bar block, whose columns are made of spaces.
    """
    return esc(plain(str(value)))


def rich(value):
    """The model's own text: plain punctuation, escaped, then the only markup we honour, **bold**."""
    return BOLD.sub(lambda match: f"<b>{match.group(1)}</b>", say(value))


def untagged(body):
    """The same message without any HTML, for when Telegram refuses to parse our entities."""
    return html.unescape(TAG.sub("", body))


def text_of(value, limit=200, flat=True):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()) if flat else "\n".join(line.rstrip() for line in value.strip().splitlines())
    return value[:limit]


def number_of(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def split_text(text, limit=MAX_MESSAGE):
    """Telegram's hard cap is 4096 rendered characters; prefer a paragraph break, then a line, then a word."""
    pieces, rest = [], text.strip("\n")
    while rest:
        if len(rich(rest)) <= limit:
            pieces.append(rest)
            break
        window = rest[:limit]
        while len(rich(window)) > limit:  # escaping expands, so shrink until the rendered piece really fits
            window = window[:int(len(window) * 0.9)] or rest[:1]
        cut, skip = window.rfind("\n\n"), 2
        for separator in ("\n", " "):
            if cut < len(window) // 2:
                cut, skip = window.rfind(separator), 1
        if cut < len(window) // 2:  # one very long word: cut it where it fits
            cut, skip = len(window), 0
        pieces.append(rest[:cut])
        rest = rest[cut + skip:]
    return [piece for piece in pieces if piece]


def clip(body, limit=MAX_MESSAGE):
    """A built, already-escaped body trimmed to fit: escaping expands text up to sixfold (' -> &#x27;), so a
    card of untrusted posts can pass Telegram's 4096 and be refused outright. Cut only at a space, a newline
    or the end of an entity or tag - none of them can occur inside one - then close what the cut left open."""
    if len(body) <= limit:
        return body
    head = body[:limit - 40]
    edge = max(head.rfind("\n"), head.rfind(" "), head.rfind(";") + 1, head.rfind(">") + 1)
    head = (head[:edge] if edge > 0 else head).rstrip() + "\n…"
    return head + "".join(f"</{tag}>" for tag in ("pre", "b") if head.count(f"<{tag}>") > head.count(f"</{tag}>"))


def bars(count, top):
    return 0 if count <= 0 or top <= 0 else max(1, min(12, round(count / top * 12)))


def bar_block(rows):
    """A padded bar per row, inside <pre> so the columns line up. Spaces, so it never sees plain().

    The rows that fit are chosen first: a label or a peak from a row nobody sees would pad every line
    out past the width of a phone, or flatten every bar on screen to nothing.
    """
    rows = [(label, count) for label, count in rows if label][:BAR_ROWS]
    if not rows:
        return ""
    top, width = max(count for _, count in rows), max(len(label) for label, _ in rows)
    return "<pre>" + esc("\n".join(f"{label:<{width}}  {BLOCK * bars(count, top):<12}  {count:>9,.0f}"
                                   for label, count in rows)) + "</pre>"


def day_words(value):
    """"2026-09-09" reads as "Sep 9". A live bucket's own label ("19:05") is left as it is."""
    text = text_of(value, 24)
    match = DATE.fullmatch(text)
    if not match:
        return text
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12:
        return text
    return f"{steps.MONTHS[month - 1]} {day}" + ("" if year == steps.BASE_YEAR else f" {year}")


def posts_words(count):
    return f"{count:,.0f} post" + ("" if count == 1 else "s")


def duration_words(ms, exact=False):
    """"22 seconds", "1 minute 5 seconds", and "3.1 seconds" for one step inside /details.

    The same words as the page (chat.js formatDuration and durationWords), so the two front ends
    say a length the same way.
    """
    value = number_of(ms)
    if value is None or value < 0:
        return ""
    if exact and value < 9950:
        seconds = round(value / 100) / 10
        return f"{seconds:g} second" + ("" if seconds == 1 else "s")
    total = round(value / 1000)
    if total < 60:
        return f"{total} second" + ("" if total == 1 else "s")
    minutes, seconds = divmod(total, 60)
    said = f"{minutes} minute" + ("" if minutes == 1 else "s")
    return said + (f" {seconds} second" + ("" if seconds == 1 else "s") if seconds else "")


def preview_body(event):
    """The preview card as a message: the total, a bar per day, and up to three of the real posts."""
    lines = [f"<b>{say(text_of(event.get('title'), 80) or 'What we found')}</b>"]
    total = number_of(event.get("total"))
    if total is not None:
        lines.append(say(("" if event.get("exact") else "about ") + posts_words(total)))
    block = bar_block([(day_words(day.get("day")), number_of(day.get("count")) or 0)
                       for day in event.get("per_day") or [] if isinstance(day, dict)])
    if block:
        lines.append(block)
    for post in (event.get("examples") or [])[:3]:
        if not isinstance(post, dict):
            continue
        likes = number_of(post.get("like_count"))
        meta = ", ".join(part for part in (day_words(post.get("day")), f"{likes:,.0f} likes" if likes is not None else "",
                                           text_of(post.get("url"), 300)) if part)
        lines.append(say(f"- {text_of(post.get('body'), 200)}" + (f"\n{meta}" if meta else "")))
    note = text_of(event.get("note"), 300)
    if note:
        lines.append(say(note))
    return "\n\n".join(lines)


def chart_body(card):
    """A chart with no picture, as words: its title, its caption and a bar per value."""
    rows = []
    for bar in card.get("bars") if isinstance(card.get("bars"), list) else []:
        if isinstance(bar, (list, tuple)) and len(bar) == 2:
            bar = {"label": bar[0], "value": bar[1]}
        if not isinstance(bar, dict):
            continue
        label = next((text_of(bar.get(key), 24) for key in ("label", "name", "day", "key") if text_of(bar.get(key), 24)), "")
        value = next((number_of(bar.get(key)) for key in ("value", "count", "n", "posts") if number_of(bar.get(key)) is not None), None)
        if label and value is not None:
            rows.append((day_words(label), value))
    title, caption = text_of(card.get("title"), 120), text_of(card.get("caption"), 600, flat=False)
    lines = [f"<b>{say(title)}</b>" if title else "", say(caption) if caption else "", bar_block(rows)]
    return "\n\n".join(line for line in lines if line)


def chart_caption(card):
    """A caption carries no markup of its own (no parse_mode), so it is cleaned but never escaped."""
    title, caption = text_of(card.get("title"), 120), text_of(card.get("caption"), 700, flat=False)
    return clean(plain("\n".join(part for part in (title, caption) if part)))[:1000]


def picture_path(value):
    """Only our own API on our own host may be fetched, and never a path that climbs out of it."""
    path = text_of(value, 300)
    return path if CARD_PATH.fullmatch(path) and ".." not in path else ""


def request_text(detail):
    """The exact request, the way the page's own details panel prints it (chat.js detailText)."""
    detail = detail if isinstance(detail, dict) else {}
    name = text_of(detail.get("tool"), 80) or "(unknown tool)"
    try:  # RecursionError too: a panel may never be the reason nothing arrives
        body = json.dumps(detail.get("input"), indent=2, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        body = "(the exact request could not be shown)"
    return f"{name}\n{body[:2400]}"


def pack(blocks, limit=MAX_MESSAGE):
    """As many whole blocks per message as fit, in order. A block too big for one message gets one."""
    messages, current = [], ""
    for block in blocks:
        joined = f"{current}\n\n{block}" if current else block
        if current and len(joined) > limit:
            messages.append(current)
            current = block
        else:
            current = joined
    return messages + ([current] if current else [])


def pairing_body(chat_id):
    return esc(f"Hello. Nobody is allowed to use me yet, so this is the only thing I can say.\n\n"
               f"This chat's ID is {chat_id}\n\n"
               f"Open this file:\n\n    {REPO / '.env'}\n\n"
               f"add this line:\n\n    TELEGRAM_ALLOWED_CHAT_IDS={chat_id}\n\n"
               f"and start me again:\n\n    python -m uv run harness/telegram_bot.py")


def parse_plan(text):
    """"7:30 2", "7am" or "19" -> ("07:30", 2, False), or None when it is not a time of day. The length
    defaults to BRIEF_MINUTES and is capped there rather than refused - the cap is about cost, not about
    the user - and the last value says that it was capped, so the user can be told."""
    match = PLAN.fullmatch(str(text).strip())
    if not match:
        return None
    hour, minute, half = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    asked = int(match[4] or BRIEF_MINUTES)
    return f"{hour:02d}:{minute:02d}", max(1, min(asked, BRIEF_MINUTES)), asked > BRIEF_MINUTES


def length_of(minutes):
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def brief_caption(brief):
    """What the recording covers, in plain words: its title and the stories in it. Telegram caps a caption at 1024."""
    stories = [text_of(story.get("title"), 90) for segment in brief.get("segments") or [] if isinstance(segment, dict)
               for story in segment.get("stories") or [] if isinstance(story, dict)]
    lines = [text_of(brief.get("title"), 120) or "Your morning brief"] + [f"- {title}" for title in stories[:8] if title]
    return clean(plain("\n".join(lines)))[:1000]


def brief_script(brief):
    """The words that would have been spoken, for the morning the voice could not be recorded."""
    parts = [text_of(segment.get("script"), 6000, flat=False) for segment in brief.get("segments") or [] if isinstance(segment, dict)]
    return "\n\n".join(part for part in parts if part)


def decide(directory, confirmation_id, approved, busy=False):
    """The button decides, never the model. Same rules and same order as bridge.post_confirm."""
    if not CONFIRMATION.fullmatch(confirmation_id or ""):
        return INVALID
    path = Path(directory) / "confirmations" / f"{confirmation_id}.json"
    if not path.exists():
        return INVALID
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return INVALID
    if record.get("expires_ms", 0) <= int(time.time() * 1000):
        return "That confirmation expired. Ask for a new one."
    if record.get("decision") is not None:
        return "That confirmation was already decided."
    if busy:
        return "Still working on your previous message."
    record.update(decision="approved" if approved else "declined", decided_ms=int(time.time() * 1000))
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temp.replace(path)
    return None


def default_runner(session_id, directory):
    """Exactly how bridge.py starts a session: one Runner, its own mcp.json, only our tools."""
    runner = Runner(session_id, directory)
    runner.mcp_config()
    return runner


async def stop(task):
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class ApiError(Exception):
    """Telegram's own refusal. The description is safe to show; the URL carries the token and is never logged."""

    def __init__(self, status, description, parameters=None):
        super().__init__(f"{status}: {description}")
        self.status, self.description, self.parameters = status, description or "", parameters if isinstance(parameters, dict) else {}


class Api:
    def __init__(self, token, base, http, sleep=asyncio.sleep):
        self.token, self.base, self.http, self.sleep = token, str(base).rstrip("/"), http, sleep

    def mask(self, value):
        return str(value).replace(self.token, f"****{self.token[-4:]}")

    @staticmethod
    def form(body, upload):
        """A file travels as a multipart form, not as JSON, and a form is spent once it is sent."""
        field, (filename, data, content_type) = upload
        form = aiohttp.FormData()
        for key, value in body.items():
            form.add_field(key, value if isinstance(value, str) else json.dumps(value))
        form.add_field(field, data, filename=filename, content_type=content_type)
        return form

    async def call(self, method, attempts=3, upload=None, **payload):
        body, error = {key: value for key, value in payload.items() if value is not None}, None
        for attempt in range(attempts):
            sending = {"data": self.form(body, upload)} if upload else {"json": body}
            async with self.http.post(f"{self.base}/bot{self.token}/{method}", **sending) as response:
                try:
                    answer = await response.json(content_type=None)
                except ValueError:
                    answer = None
            answer = answer if isinstance(answer, dict) else {}
            if answer.get("ok"):
                return answer.get("result")
            error = ApiError(response.status, answer.get("description"), answer.get("parameters"))
            if error.status == 429 and attempt + 1 < attempts:
                await self.sleep(min(float(error.parameters.get("retry_after") or 1), 60))
                continue
            raise error
        raise error


class Turn:
    """What the single status message says, rebuilt from the events that have arrived so far.

    As quiet as the website's rows: a mark and the title, and for a step that did not work its one
    short result line. Everything else (the why, the result, the facts, the exact request) is kept
    here and sent only when the user asks for it with /details, which is Telegram's details panel.
    """

    def __init__(self):
        self.steps, self.order, self.progress, self.duration_ms = {}, [], None, None

    def event(self, event):
        kind = event.get("type")
        if kind == "progress":
            self.progress = text_of(event.get("text"), 160) or None
        elif kind == "step":
            self.step(event)
        elif kind == "done":
            self.duration_ms = number_of(event.get("duration_ms"))

    def step(self, event):
        if event.get("phase") not in ("start", "end"):  # a step event we cannot place carries nothing to show
            return
        identifier = str(event.get("id") or len(self.order) + 1)
        record = self.steps.setdefault(identifier, {"n": len(self.order) + 1, "title": "", "why": None, "outcome": None,
                                                    "ok": True, "ms": None, "running": True, "detail": {}, "facts": []})
        if identifier not in self.order:
            self.order.append(identifier)
        if event.get("phase") == "start":
            number = number_of(event.get("n"))
            record.update(title=text_of(event.get("title"), 160), why=text_of(event.get("why"), 240) or None,
                          running=True, n=int(number) if number else record["n"],
                          detail=event.get("detail") if isinstance(event.get("detail"), dict) else {},
                          facts=[fact for fact in event.get("facts") or [] if isinstance(fact, dict)][:8])
        elif event.get("phase") == "end":
            record.update(running=False, ok=event.get("ok") is not False, ms=number_of(event.get("ms")),
                          outcome=text_of(event.get("outcome"), 200) or None)

    def body(self):
        lines = []
        for identifier in self.order:
            step = self.steps[identifier]
            mark = HOURGLASS if step["running"] else (OK if step["ok"] else FAILED)
            lines.append(f"{mark} {say(step['title'] or 'Working on it')}")
            if not step["running"] and not step["ok"] and step["outcome"]:
                lines.append(f"   {say(step['outcome'])}")  # the one thing a closed row adds, as on the page
        if not lines:
            lines.append(say(self.progress or "Working…"))
        while len(lines) > 1 and len("\n".join(lines)) > 3500:  # whole lines only: never cut an entity in half
            lines.pop(0)
        return "\n".join(lines)

    def details(self):
        """What the page shows when someone opens a row: why, what came back, the facts, the request."""
        blocks = []
        for number, identifier in enumerate(self.order, 1):
            step = self.steps[identifier]
            lines = [f"<b>{number}. {say(step['title'] or 'Working on it')}</b>"]
            if step["why"]:
                lines.append(f"Why: {say(step['why'])}")
            lines.append(say(step["outcome"] or ("Still working…" if step["running"] else "Done.")))
            for fact in step["facts"]:
                label, value = say(text_of(fact.get("label"), 60)), say(text_of(fact.get("value"), 240))
                if value:
                    lines.append(f"{label}: {value}" if label else value)
            took = duration_words(step["ms"], exact=True)
            if took:
                lines.append(say(f"Took {took}"))
            lines.append("Exact request\n<pre>" + esc(request_text(step["detail"])) + "</pre>")
            blocks.append("\n".join(lines))
        return blocks

    def summary(self, elapsed):
        """The one line the status collapses into, in the page's own words: "4 steps, 22 seconds"."""
        failed = sum(1 for step in self.steps.values() if not step["running"] and not step["ok"])
        count = len(self.order)
        parts = [f"{count} step" + ("" if count == 1 else "s")] if count else ["Done"]
        if failed:
            parts.append(f"{failed} did not finish")
        took = duration_words(self.duration_ms if self.duration_ms is not None else elapsed * 1000)
        return ", ".join(parts + ([took] if took else []))


class Bot:
    def __init__(self, token, allowed, http, base=None, runner_factory=None, sleep=asyncio.sleep,
                 now=time.monotonic, sessions=SESSIONS, chats_path=CHATS, clock=datetime.now, brief_base=None,
                 card_base=None):
        self.api = Api(token, base or API_BASE, http, sleep)
        self.allowed, self.sleep, self.now = {int(chat_id) for chat_id in allowed}, sleep, now
        self.sessions, self.chats_path = Path(sessions), Path(chats_path)
        self.make_runner = runner_factory or default_runner
        self.chats, self.saved, self.refused, self.tasks = {}, {}, {}, set()
        self.gate, self.offset, self.username = asyncio.Semaphore(MAX_TURNS), None, None
        self.clock, self.brief_base = clock, (brief_base or BRIEF_BASE).rstrip("/")  # the wall clock: a schedule is a time of day
        self.card_base = (card_base or brief_base or CARD_BASE).rstrip("/")  # one server once the combined one is on
        self.plans, self.making, self.recording = {}, set(), asyncio.Lock()
        self.following = set()  # the brief cards already being followed, so one brief is waited for once
        self.load()

    def log(self, message):
        """Masked, and forced to ASCII: the Windows console is cp1252 and a log line may not kill the poll loop."""
        print(self.api.mask(message).encode("ascii", "replace").decode("ascii"), flush=True)

    # ---- sessions -------------------------------------------------------------------------------

    def load(self):
        try:
            saved = json.loads(self.chats_path.read_text(encoding="utf-8")).get("chats")
        except (OSError, ValueError, AttributeError):
            saved = None
        for key, record in (saved if isinstance(saved, dict) else {}).items():
            if not CHAT_ID.fullmatch(str(key)) or not isinstance(record, dict):
                continue  # a file somebody edited by hand may not stop the bot from starting
            session_id, plan = record.get("session_id"), record.get("schedule")
            if isinstance(session_id, str) and SESSION.fullmatch(session_id):  # checked before it is a path
                self.saved[int(key)] = session_id
            found = parse_plan(f"{plan.get('at')} {plan.get('minutes')}") if isinstance(plan, dict) else None
            if found:
                self.plans[int(key)] = {"at": found[0], "minutes": found[1], "last": text_of(plan.get("last"), 10) or None}

    def save(self):
        chats = {str(chat): {"session_id": session} for chat, session in self.saved.items()}
        for chat, plan in self.plans.items():
            chats.setdefault(str(chat), {})["schedule"] = plan
        self.chats_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.chats_path.with_name(self.chats_path.name + ".tmp")
        temp.write_text(json.dumps({"chats": chats}, indent=2), encoding="utf-8")
        temp.replace(self.chats_path)

    def chat(self, chat_id, fresh=False):
        state = self.chats.get(chat_id)
        if state is not None and not fresh:
            return state
        session_id = None if fresh else self.saved.get(chat_id)
        resumed, session_id = bool(session_id), session_id or str(uuid.uuid4())
        directory = self.sessions / session_id
        (directory / "confirmations").mkdir(parents=True, exist_ok=True)
        runner = self.make_runner(session_id, directory)
        if resumed and hasattr(runner, "started"):
            runner.started = True  # Claude Code already knows this uuid, so the next turn must --resume, not --session-id
        state = {"id": chat_id, "session": session_id, "dir": directory, "runner": runner,
                 "busy": False, "edited": self.now() - EDIT_INTERVAL, "last": None}  # last: the turn /details shows
        self.chats[chat_id], self.saved[chat_id] = state, session_id
        self.save()
        return state

    # ---- transport ------------------------------------------------------------------------------

    async def deliver(self, method, body, **payload):
        """Everything goes out as escaped HTML, trimmed to the cap; if Telegram still refuses to parse it,
        resend it flat, and if it is still too long, short - a preview the user has to judge the project by
        may be trimmed but never silently dropped."""
        body = clip(body)
        for text, mode in ((body, "HTML"), (untagged(body), None), (clip(untagged(body), 900), None)):
            try:
                return await self.api.call(method, text=text, parse_mode=mode, **payload)
            except ApiError as error:
                description = error.description.lower()
                if error.status != 400 or not ("parse" in description or "too long" in description):
                    if "not modified" not in description:
                        self.log(f"{method} refused: {error.status} {error.description[:160]}")
                    return None
            except NETWORK as error:
                self.log(f"{method} failed: {type(error).__name__}")
                return None
        self.log(f"{method} refused every version of that message")
        return None

    async def send(self, chat_id, body, markup=None):
        return await self.deliver("sendMessage", body, chat_id=chat_id, reply_markup=markup,
                                  link_preview_options={"is_disabled": True})

    async def edit(self, state, message_id, body):
        """One edit per EDIT_INTERVAL per chat; omitting reply_markup is what removes a keyboard."""
        wait = EDIT_INTERVAL - (self.now() - state["edited"])
        if wait > 0:
            await self.sleep(wait)
        state["edited"] = self.now()
        return await self.deliver("editMessageText", body, chat_id=state["id"], message_id=message_id,
                                  link_preview_options={"is_disabled": True})

    async def typing(self, chat_id):
        while True:
            with contextlib.suppress(ApiError, *NETWORK):
                await self.api.call("sendChatAction", attempts=1, chat_id=chat_id, action="typing")
            await self.sleep(TYPING_INTERVAL)

    # ---- one turn -------------------------------------------------------------------------------

    async def run_turn(self, state, text):
        turn, started = Turn(), self.now()
        shown, typing, ticker, message_id = None, None, None, None

        async def live():
            nonlocal shown
            while True:
                await self.sleep(EDIT_INTERVAL)
                body = turn.body()
                if body and body != shown:
                    shown = body
                    await self.edit(state, message_id, body)

        try:
            # Inside the try: an exception out here used to leave the chat busy for ever and the typing loop running.
            typing = asyncio.create_task(self.typing(state["id"]))
            status = await self.send(state["id"], say("Thinking…"))
            message_id = status.get("message_id") if isinstance(status, dict) else None
            ticker = asyncio.create_task(live()) if message_id else None
            async with self.gate, asyncio.timeout(TURN_TIMEOUT_S):
                async for event in state["runner"].turn(text):
                    if not isinstance(event, dict):
                        continue
                    turn.event(event)
                    kind = event.get("type")
                    if kind == "preview":
                        await self.send(state["id"], preview_body(event))
                    elif kind == "confirm_request":
                        await self.ask_confirm(state, event)
                    elif kind == "card":
                        await self.on_card(state["id"], event.get("card"))
                    elif kind == "message":
                        for piece in split_text(text_of(event.get("text"), 40000, flat=False)):
                            await self.send(state["id"], rich(piece))
                    elif kind == "error":
                        await self.send(state["id"], say(f"Sorry, that did not work. {text_of(event.get('text'), 600)}"))
        except asyncio.TimeoutError:
            await self.send(state["id"], say("That took longer than five minutes, so I stopped it. Try a smaller question."))
        except Exception as error:
            self.log(f"turn failed: {error!r}")
            await self.send(state["id"], say("Something broke on my side. Try again, or send /new to start over."))
        finally:
            state["busy"] = False  # first, so nothing below can leave this chat answering "Still working" for ever
            state["last"] = turn  # what /details shows, until the next turn replaces it
            await stop(typing)
            await stop(ticker)
            with contextlib.suppress(Exception):
                if message_id:
                    await self.edit(state, message_id, say(turn.summary(self.now() - started)))

    async def ask_confirm(self, state, event):
        confirmation_id = text_of(event.get("confirmation_id"), 64)
        if not CONFIRMATION.fullmatch(confirmation_id):
            return
        body = "<b>Ready to start</b>\n\n" + say(text_of(event.get("summary"), 1200, flat=False) or "Start this project?")
        await self.send(state["id"], body, markup={"inline_keyboard": [[
            {"text": "Confirm", "callback_data": f"c:{confirmation_id}:1"},
            {"text": "Cancel", "callback_data": f"c:{confirmation_id}:0"}]]})

    async def on_details(self, state):
        """Telegram has no panel to open, so the whole of the last turn's steps comes on request.

        A turn of two hundred steps would arrive as a dozen messages and be rate-limited into the next
        minute, so a long one is cut and the reader is told how many there really were.
        """
        turn = state.get("last")
        blocks = turn.details() if turn else []
        if not blocks:
            return await self.send(state["id"], say(NOTHING_YET))
        if len(blocks) > DETAIL_STEPS:
            blocks = blocks[:DETAIL_STEPS] + [say(f"There were {len(blocks)} steps in all. "
                                                 f"These are the first {DETAIL_STEPS}.")]
        for piece in pack(blocks):
            await self.send(state["id"], piece)

    # ---- cards ------------------------------------------------------------------------------------

    async def on_card(self, chat_id, card):
        """A tool's own card, the same event the page's plug-ins draw. An unknown kind is ignored."""
        if not isinstance(card, dict):
            return
        kind = text_of(card.get("kind"), 24)
        if kind == "chart":
            await self.send_chart(chat_id, card)
        elif kind == "brief":
            self.spawn(self.follow_brief(chat_id, card))  # minutes of polling: never in front of the answer

    async def send_chart(self, chat_id, card):
        """The picture with its caption when there is one, the same numbers as bars when there is not."""
        picture = await self.picture(picture_path(card.get("png_url")))
        if picture and await self.send_photo(chat_id, chart_caption(card), picture):
            return
        body = chart_body(card)
        if body:
            await self.send(chat_id, body)

    async def picture(self, path):
        """Bounded in both directions: the answer behind this may not wait on a server that hangs, and
        a server that answers with something enormous may not be held in memory whole."""
        if not path:
            return None
        try:
            async with asyncio.timeout(CARD_FETCH_S):
                status, data = await self.ask(path, base=self.card_base, binary=True, limit=MAX_PICTURE)
        except NETWORK as error:
            self.log(f"the chart picture did not arrive: {type(error).__name__}")
            return None
        return data if status == 200 and 0 < len(data) <= MAX_PICTURE else None

    async def send_photo(self, chat_id, caption, picture):
        try:
            return await self.api.call("sendPhoto", upload=("photo", ("chart.png", picture, "image/png")),
                                       chat_id=chat_id, caption=caption or None)
        except ApiError as error:
            self.log(f"sendPhoto refused: {error.status} {error.description[:160]}")
        except NETWORK as error:
            self.log(f"sendPhoto failed: {type(error).__name__}")
        return None

    async def follow_brief(self, chat_id, card):
        """Wait for a brief the model ordered, then upload the recording. Three minutes at the most."""
        brief_id, title = text_of(card.get("brief_id"), 40), text_of(card.get("title"), 120)
        if not BRIEF_ID.fullmatch(brief_id) or (chat_id, brief_id) in self.following:
            return
        self.following.add((chat_id, brief_id))
        try:
            brief, deadline = {}, self.now() + CARD_BRIEF_WAIT_S
            while True:
                status, brief = await self.ask(f"/api/briefs/{brief_id}")
                if brief.get("status") != "working" or self.now() >= deadline:
                    break
                await self.sleep(BRIEF_POLL_S)
            if brief.get("status") == "ready":
                name = (brief.get("audio") or {}).get("full")
                if isinstance(name, str) and BRIEF_FILE.fullmatch(name):
                    status, data = await self.ask(f"/api/briefs/{brief_id}/audio/{name}", binary=True)
                    if status == 200 and data and await self.send_audio(chat_id, brief, data,
                                                                       caption=plain(title) or brief_caption(brief)):
                        return
                return await self.send(chat_id, say("Your brief is ready, but I could not send the recording here. "
                                                    "It is on the Morning Brief page."))
            if brief.get("status") == "working":
                return await self.send(chat_id, say("Your brief is taking longer than usual. "
                                                    "It will be on the Morning Brief page when it is done."))
            await self.send(chat_id, say("I could not make your brief. "  # a server that says nothing still gets a sentence
                                         + (text_of(brief.get("step"), 300) or "Morning Brief could not finish it.")))
        except NETWORK as error:
            self.log(f"Morning Brief did not answer on {self.brief_base}: {type(error).__name__}")
            await self.send(chat_id, say(BRIEF_OFFLINE))
        finally:
            self.following.discard((chat_id, brief_id))

    # ---- the morning brief ----------------------------------------------------------------------

    def late_by(self, plan):
        """Seconds since today's scheduled time on the wall clock; negative while it is still to come."""
        now = self.clock()
        hour, minute = map(int, plan["at"].split(":"))
        return (now - now.replace(hour=hour, minute=minute, second=0, microsecond=0)).total_seconds()

    def due(self):
        today = self.clock().date().isoformat()
        return [chat_id for chat_id, plan in self.plans.items()
                if chat_id in self.allowed and plan.get("last") != today and 0 <= self.late_by(plan) <= BRIEF_GRACE_S]

    async def mornings(self):
        """The timer. A morning is marked as sent before its brief is ordered: a crash or a failure costs one
        brief, never a second paid recording half a minute later."""
        while True:
            try:
                groups = {}
                for chat_id in self.due():
                    self.plans[chat_id]["last"] = self.clock().date().isoformat()
                    groups.setdefault(self.plans[chat_id]["minutes"], []).append(chat_id)
                if groups:
                    self.save()
                for minutes, chats in groups.items():  # one recording per length, shared by every chat that wants it
                    await self.brief(chats, minutes)
            except Exception as error:
                self.log(f"morning brief failed: {error!r}")
            await self.sleep(SCHEDULE_TICK)

    async def ask(self, path, payload=None, base=None, binary=False, limit=None):
        """One request to a server on this laptop. Origin is what its same-origin check reads, exactly
        as brief_tools.call sends it. The base is the Morning Brief server unless a card names the other one.

        `limit` is how much of a body we are willing to hold: the read stops one byte past it, so the
        caller sees a body too big to send rather than a laptop filling up.
        """
        base = (base or self.brief_base).rstrip("/")
        url = f"{base}{path}"
        request = self.api.http.get(url) if payload is None else self.api.http.post(url, json=payload, headers={"Origin": base})
        async with request as response:
            if binary or path.endswith(".mp3"):
                if limit is None:
                    return response.status, await response.read()
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limit:
                        break
                return response.status, bytes(data)
            try:
                answer = await response.json(content_type=None)
            except ValueError:
                answer = None
            return response.status, answer if isinstance(answer, dict) else {}

    async def make(self, minutes):
        """Order one brief and wait for it: the finished brief with its recording, or a sentence about what went wrong."""
        try:
            for _ in range(BRIEF_BUSY_TRIES):
                status, answer = await self.ask("/api/briefs", {"hours": BRIEF_HOURS, "seconds": minutes * 60})
                if status != 409:  # 409: the page or another chat is having one made, and the server makes one at a time
                    break
                await self.sleep(BRIEF_BUSY_S)
            brief_id = answer.get("id")
            if status == 409:
                return {"problem": "Another brief was being made the whole time. Send /brief to try again."}
            if status != 200 or not isinstance(brief_id, str) or not BRIEF_ID.fullmatch(brief_id):
                return {"problem": text_of(answer.get("error"), 300) or "Morning Brief did not accept the order."}
            brief, deadline = {}, self.now() + BRIEF_WAIT_S
            while self.now() < deadline:
                await self.sleep(BRIEF_POLL_S)
                status, brief = await self.ask(f"/api/briefs/{brief_id}")
                if brief.get("status") != "working":
                    break
            if brief.get("status") == "working":
                return {"problem": "It took longer than ten minutes, so I stopped waiting. Send /brief to try again."}
            if brief.get("status") != "ready":
                return {"problem": text_of(brief.get("step"), 300) or "Morning Brief could not finish it."}
            name, audio = (brief.get("audio") or {}).get("full"), None
            if isinstance(name, str) and BRIEF_FILE.fullmatch(name):
                status, data = await self.ask(f"/api/briefs/{brief_id}/audio/{name}")
                audio = data if status == 200 and data else None
            return {"brief": brief, "audio": audio}
        except NETWORK as error:
            self.log(f"Morning Brief did not answer on {self.brief_base}: {type(error).__name__}")
            return {"problem": BRIEF_OFFLINE}

    async def brief(self, chats, minutes):
        """One brief, made once and handed to every chat in the list."""
        async with self.recording:  # the server makes one at a time, and so do we
            made = await self.make(minutes)
        for chat_id in chats:
            await self.hand_over(chat_id, made)

    async def hand_over(self, chat_id, made):
        if made.get("problem"):
            return await self.send(chat_id, say(f"I could not make your brief. {made['problem']}"))
        brief = made["brief"]
        if made.get("audio") and await self.send_audio(chat_id, brief, made["audio"]):
            return
        script = brief_script(brief)
        await self.send(chat_id, say("The voice recording did not work this time, so here is your brief to read." if script
                                     else "Your brief was made, but I could not send it here. It is on the Morning Brief page."))
        for piece in split_text(script):
            await self.send(chat_id, say(piece))

    async def send_audio(self, chat_id, brief, audio, caption=None):
        try:
            return await self.api.call("sendAudio", upload=("audio", ("morning-brief.mp3", audio, "audio/mpeg")), chat_id=chat_id,
                                       title=plain(text_of(brief.get("title"), 120)) or "Morning brief", performer="Morning Brief",
                                       caption=caption or brief_caption(brief))
        except ApiError as error:
            self.log(f"sendAudio refused: {error.status} {error.description[:160]}")
        except NETWORK as error:
            self.log(f"sendAudio failed: {type(error).__name__}")
        return None

    async def on_schedule(self, chat_id, words):
        rest, plan = " ".join(words).lower(), self.plans.get(chat_id)
        if not rest:
            return await self.send(chat_id, say(
                f"Your morning brief comes every day at {plan['at']} and is about {length_of(plan['minutes'])} long. "
                "Send a new time to change it, or /schedule off to stop." if plan else SCHEDULE_HELP))
        if rest in ("off", "stop"):
            self.plans.pop(chat_id, None)
            self.save()
            return await self.send(chat_id, say("Done. I will not send a morning brief any more."))
        found = parse_plan(BRIEF_AT if rest == "on" else rest)
        if not found:
            return await self.send(chat_id, say("I did not understand that time. Try it like this: /schedule 7:00 3"))
        plan = {"at": found[0], "minutes": found[1], "last": None}
        if self.late_by(plan) >= 0:  # today's time has passed: the first one is tomorrow's, not a surprise right now
            plan["last"] = self.clock().date().isoformat()
        self.plans[chat_id] = plan
        self.save()
        await self.send(chat_id, say(
            (f"The longest a brief can be is {length_of(BRIEF_MINUTES)}, so I set it to that. " if found[2] else "")
            + f"Done. Every day at {plan['at']} I will send you a brief of about {length_of(plan['minutes'])} on what happened "
            f"in the last {BRIEF_HOURS} hours. The first one comes {'tomorrow' if plan['last'] else 'today'}. "
            "The laptop has to be on at that time. Send /brief to hear one now."))

    async def on_brief(self, chat_id, words):
        if chat_id in self.making:
            return await self.send(chat_id, say("Your brief is already being made. It will arrive here soon."))
        self.making.add(chat_id)  # claimed with no await in between, so two /brief cannot both order a paid recording
        try:
            found = parse_plan(f"{BRIEF_AT} {words[0]}") if words else None
            minutes = found[1] if found else (self.plans.get(chat_id) or {}).get("minutes", BRIEF_MINUTES)
            await self.send(chat_id, say(f"Making a brief of about {length_of(minutes)} on the last {BRIEF_HOURS} hours. "
                                         "It takes a minute or two."))
            await self.brief([chat_id], minutes)
        finally:
            self.making.discard(chat_id)

    # ---- updates --------------------------------------------------------------------------------

    async def refuse(self, chat_id):
        last = self.refused.get(chat_id)
        if last is not None and self.now() - last < REFUSAL_INTERVAL:
            return
        self.refused[chat_id] = self.now()
        await self.send(chat_id, say(f"This bot is private. Its owner can let you in by adding {chat_id} to "
                                     "TELEGRAM_ALLOWED_CHAT_IDS."))

    async def on_message(self, message):
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id, text = chat.get("id"), text_of(message.get("text"), MAX_MESSAGE, flat=False)
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not text:
            return
        if not self.allowed:
            return await self.send(chat_id, pairing_body(chat_id))  # pairing mode: never a model call
        if chat_id not in self.allowed:  # a group is served only when its own ID is on the list
            return await self.refuse(chat_id)
        command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        if command in ("/start", "/help"):
            return await self.send(chat_id, say(HELP))
        if command == "/id":
            return await self.send(chat_id, say(f"This chat's ID is {chat_id}"))
        if command == "/schedule":  # none of these is a model call, so none of them waits for a running turn
            return await self.on_schedule(chat_id, text.split()[1:])
        if command == "/brief":
            return await self.on_brief(chat_id, text.split()[1:])
        state = self.chat(chat_id)
        if command == "/details":  # the last turn, whether or not this one is still running
            return await self.on_details(state)
        if state["busy"]:
            return await self.send(chat_id, say("Still working on your previous message."))
        if command == "/new":
            self.chat(chat_id, fresh=True)
            return await self.send(chat_id, say("Fresh start: I have forgotten what we said before."))
        state["busy"] = True  # claimed with no await in between, so two messages cannot both pass
        await self.run_turn(state, text)

    async def on_callback(self, query):
        with contextlib.suppress(ApiError, *NETWORK):
            await self.api.call("answerCallbackQuery", attempts=1, callback_query_id=query.get("id"))
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id, data = chat.get("id"), str(query.get("data") or "")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not self.allowed or chat_id not in self.allowed:
            return
        head, _, rest = data.partition(":")
        confirmation_id, _, flag = rest.rpartition(":")
        if head != "c" or flag not in ("0", "1") or not CONFIRMATION.fullmatch(confirmation_id):
            return
        state, approved = self.chat(chat_id), flag == "1"
        problem = decide(state["dir"], confirmation_id, approved, busy=state["busy"])
        if not problem:
            state["busy"] = True  # claimed before the edit awaits, so a message cannot slip in behind the button
        note = problem or ("Confirmed." if approved else "Cancelled.")
        body = say(text_of(message.get("text"), 1200, flat=False)) + f"\n\n<b>{say(note)}</b>"
        await self.edit(state, message.get("message_id"), body)
        if problem:
            return
        await self.run_turn(state, f"The user pressed the {'Confirm' if approved else 'Cancel'} button "
                                   f"for confirmation {confirmation_id}.")

    async def handle(self, update):
        try:
            if isinstance(update.get("message"), dict):
                await self.on_message(update["message"])
            elif isinstance(update.get("callback_query"), dict):
                await self.on_callback(update["callback_query"])
        except Exception as error:
            self.log(f"update {update.get('update_id')} failed: {error!r}")

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def poll(self):
        backoff = 1.0
        while True:
            try:
                updates = await self.api.call("getUpdates", offset=self.offset, timeout=POLL_TIMEOUT,
                                              allowed_updates=["message", "callback_query"])
            except ApiError as error:
                if error.status == 409:
                    print(CONFLICT, flush=True)
                    return 1
                if error.status == 401:
                    print(BAD_TOKEN, flush=True)
                    return 1
                self.log(f"getUpdates refused: {error.status} {error.description[:120]}")
            except NETWORK as error:
                self.log(f"getUpdates failed: {type(error).__name__}; retrying in {backoff:.0f}s")
            else:
                backoff = 1.0
                for update in updates or []:
                    identifier = number_of(update.get("update_id")) if isinstance(update, dict) else None
                    if identifier is None:
                        continue
                    self.offset = max(self.offset or 0, int(identifier) + 1)  # advanced before handling: never processed twice
                    self.spawn(self.handle(update))
                continue
            await self.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def connect(self):
        """getMe until Telegram answers, however long that takes.

        A network that blocks Telegram is the normal case on the campus Wi-Fi, so it is something to
        wait out, not a reason to stop: the sentence is printed once and then it keeps trying, and the
        morning brief and every command work again the moment the hotspot is on. Only a token Telegram
        rejects and a second copy of this bot are fatal, because waiting cannot fix either.
        """
        said, wait = False, CONNECT_WAIT_S
        while True:
            try:
                me = await self.api.call("getMe", attempts=1)
                await self.api.call("deleteWebhook", attempts=1)  # long polling and a webhook are mutually exclusive
                return me or {}
            except ApiError as error:
                if error.status == 401:
                    print(BAD_TOKEN, flush=True)
                    return None
                if error.status == 409:
                    print(CONFLICT, flush=True)
                    return None
                # through log(): a description is remote text, and a cp1252 console cannot print all of it
                self.log(f"Telegram refused the first call, trying again: {error.description[:200]}")
            except NETWORK as error:
                if not said:
                    print(UNREACHABLE, flush=True)
                    said = True
                self.log(f"still trying to reach Telegram ({type(error).__name__})")
            await self.sleep(wait)
            wait = min(wait * 2, 60)

    async def run(self):
        me = await self.connect()
        if me is None:
            return 1
        self.username = text_of((me or {}).get("username"), 64)
        print(f"Connected as @{self.username or 'unknown'}.", flush=True)
        print(PAIRING_HINT if not self.allowed else f"Serving {len(self.allowed)} chat(s), {len(self.plans)} with a morning brief. "
                                                    "Ctrl+C stops me.", flush=True)
        try:
            if self.allowed:
                self.spawn(self.mornings())
            return await self.poll()
        finally:
            for task in list(self.tasks):
                task.cancel()


def settings():
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    raw = re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "")
    return token, {int(part) for part in raw if CHAT_ID.fullmatch(part)}  # a typo is skipped, never a crash


async def main():
    token, allowed = settings()
    if not token:
        print(SETUP, flush=True)
        return 1
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT + 40, sock_connect=20)) as http:
        return await Bot(token, allowed, http).run()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
