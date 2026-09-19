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

from claude_runner import Runner  # noqa: E402  (reads the environment loaded above, exactly as bridge.py drives it)

SESSIONS = ROOT / "state/sessions"  # the layout the web bridge uses, so the MCP tools behave identically
CHATS = ROOT / "state/telegram/chats.json"
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
TURN_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT", 300))
MAX_TURNS = 2  # at most two Claude Code processes at once: one laptop, one subscription (DESIGN.md §0)
EDIT_INTERVAL = 1.2  # Telegram rate-limits edits; updates in between are coalesced
TYPING_INTERVAL = 4.0  # the typing bubble lasts about five seconds
REFUSAL_INTERVAL = 3600.0
POLL_TIMEOUT = 50
MAX_MESSAGE = 4000
HOURGLASS = "⏳"
BLOCK = "█"
CONFIRMATION = re.compile(r"[A-Za-z0-9_-]{16}")  # checked before any path is built from it
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
TAG = re.compile(r"<[^>]*>")
NETWORK = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

BRIEF_BASE = (os.environ.get("SIGNAL_BASE_URL") or "http://127.0.0.1:5194").rstrip("/")  # the server brief_tools.py talks to
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

  1. Open Telegram (phone or desktop) and search for  @BotFather  - the account with the blue tick.
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
TELEGRAM_BOT_TOKEN is the whole line BotFather sent - the digits, the colon and the letters - with
nothing else on the line. If you are not sure, send @BotFather /revoke and use the new token."""

CONFLICT = """Another copy of this bot is already polling Telegram with the same token (HTTP 409).
Close that window - or stop the other machine - and start me again; only one may run at a time."""

PAIRING_HINT = f"""Pairing mode: nobody is allowed to talk to me yet, so I will not ask the model anything.
Send the bot any message in Telegram and it will reply with the line to add to {REPO / ".env"}."""

HELP = """I watch social media for you and turn a question into a real observation project.

Ask me things like:
- How did people react to PlayStation cancelling Physint on September 10?
- What is being said about AI safety on Bluesky right now?
- Which US politicians posted about student loans in August?

I will come back with counts, example posts and a Confirm button before anything is started.

/schedule  get a spoken brief here every morning, for example /schedule 7:00 3
/brief     make a brief now and send it here
/new       forget this conversation and start a fresh one
/id        show this chat's ID
/help      show this message"""

SCHEDULE_HELP = f"""You have no morning brief yet. Send a time and a length in minutes, for example:

/schedule 7:00 3

Every day at that time I will send a recording here about what happened in the last {BRIEF_HOURS} hours on the topics you follow. The length can be 1 to {BRIEF_MINUTES} minutes. Send /schedule off to stop."""

BRIEF_OFFLINE = ("Morning Brief is not running on the laptop, so there was nothing to record. "
                 "Start it, then send /brief to try again.")


def esc(value):
    """Untrusted text: every character escaped, no markup of any kind survives."""
    return html.escape(str(value))


def rich(value):
    """The model's own text: escaped first, then the only markup we honour, **bold**."""
    return BOLD.sub(lambda match: f"<b>{match.group(1)}</b>", esc(value))


def plain(body):
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


def preview_body(event):
    """The preview card as a message: the total, a bar per day, and up to three of the real posts."""
    lines = [f"<b>{esc(text_of(event.get('title'), 80) or 'Preview')}</b>"]
    total, seconds = number_of(event.get("total")), number_of(event.get("seconds"))
    if total is not None:
        lines.append(esc(f"{total:,.0f} matching posts" + ("" if event.get("exact") else " (estimated)")
                         + (f", scanned in {seconds:.0f} s" if seconds else "")))
    days = [(text_of(day.get("day"), 24) or "?", number_of(day.get("count")) or 0)
            for day in event.get("per_day") or [] if isinstance(day, dict)]
    if days:
        top, width = max(count for _, count in days), max(len(day) for day, _ in days)
        lines.append("<pre>" + esc("\n".join(f"{day:<{width}}  {BLOCK * bars(count, top):<12}  {count:>9,.0f}"
                                             for day, count in days[:16])) + "</pre>")
    for post in (event.get("examples") or [])[:3]:
        if not isinstance(post, dict):
            continue
        likes = number_of(post.get("like_count"))
        meta = " · ".join(part for part in (text_of(post.get("day"), 24), f"{likes:,.0f} likes" if likes is not None else "",
                                                 text_of(post.get("url"), 300)) if part)
        lines.append(esc(f"• {text_of(post.get('body'), 200)}" + (f"\n   {meta}" if meta else "")))
    note = text_of(event.get("note"), 300)
    if note:
        lines.append(esc(note))
    return "\n\n".join(lines)


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
    return "\n".join(lines)[:1000]


def brief_script(brief):
    """The words that would have been spoken, for the morning the voice could not be recorded."""
    parts = [text_of(segment.get("script"), 6000, flat=False) for segment in brief.get("segments") or [] if isinstance(segment, dict)]
    return "\n\n".join(part for part in parts if part)


def decide(directory, confirmation_id, approved, busy=False):
    """The button decides, never the model. Same rules and same order as bridge.post_confirm."""
    if not CONFIRMATION.fullmatch(confirmation_id or ""):
        return "That confirmation is no longer valid; ask for a new one."
    path = Path(directory) / "confirmations" / f"{confirmation_id}.json"
    if not path.exists():
        return "That confirmation is no longer valid; ask for a new one."
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "That confirmation is no longer valid; ask for a new one."
    if record.get("expires_ms", 0) <= int(time.time() * 1000):
        return "That confirmation expired; ask for a new one."
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
    """What the single status message says, rebuilt from the events that have arrived so far."""

    def __init__(self):
        self.steps, self.order, self.progress, self.draft, self.duration_ms = {}, [], None, None, None

    def event(self, event):
        kind = event.get("type")
        if kind == "progress":
            self.progress = text_of(event.get("text"), 160) or None
        elif kind == "step":
            self.step(event)
        elif kind == "spec":
            spec = event.get("spec") if isinstance(event.get("spec"), dict) else {}
            version = text_of(event.get("spec_hash"), 64)[:8]
            self.draft = (f"Draft saved: {text_of(spec.get('name'), 60) or 'the project'}"
                          + (f" (version {version})" if version else ""))
        elif kind == "done":
            self.duration_ms = number_of(event.get("duration_ms"))

    def step(self, event):
        if event.get("phase") not in ("start", "end"):  # a step event we cannot place carries nothing to show
            return
        identifier = str(event.get("id") or len(self.order) + 1)
        record = self.steps.setdefault(identifier, {"n": len(self.order) + 1, "title": "", "why": None,
                                                    "outcome": None, "ok": True, "ms": None, "running": True})
        if identifier not in self.order:
            self.order.append(identifier)
        if event.get("phase") == "start":
            number = number_of(event.get("n"))
            record.update(title=text_of(event.get("title"), 160), why=text_of(event.get("why"), 240) or None,
                          running=True, n=int(number) if number else record["n"])
        elif event.get("phase") == "end":
            record.update(running=False, ok=event.get("ok") is not False, ms=number_of(event.get("ms")),
                          outcome=text_of(event.get("outcome"), 200) or None)

    def body(self):
        lines = []
        for identifier in self.order:
            step = self.steps[identifier]
            head = f"{step['n']}. {esc(step['title'] or 'Working')}"
            lines.append(f"{HOURGLASS} {head}" if step["running"] else head)
            if step["why"]:
                lines.append(f"    Why: {esc(step['why'])}")
            if step["outcome"]:
                took = f" ({step['ms'] / 1000:.1f} s)" if step["ms"] is not None else ""
                lines.append(f"    -&gt; {esc(step['outcome'])}{esc(took)}")
        if not lines:
            lines.append(esc(self.progress or "Working…"))
        if self.draft:
            lines.append(esc(self.draft))
        while len(lines) > 1 and len("\n".join(lines)) > 3500:  # whole lines only: never cut an entity in half
            lines.pop(0)
        return "\n".join(lines)

    def summary(self, elapsed):
        seconds = self.duration_ms / 1000 if self.duration_ms is not None else elapsed
        head = f"{len(self.order)} step{'' if len(self.order) == 1 else 's'}" if self.order else "Done"
        return f"{head} · {seconds:.0f} s"


class Bot:
    def __init__(self, token, allowed, http, base=None, runner_factory=None, sleep=asyncio.sleep,
                 now=time.monotonic, sessions=SESSIONS, chats_path=CHATS, clock=datetime.now, brief_base=None):
        self.api = Api(token, base or API_BASE, http, sleep)
        self.allowed, self.sleep, self.now = {int(chat_id) for chat_id in allowed}, sleep, now
        self.sessions, self.chats_path = Path(sessions), Path(chats_path)
        self.make_runner = runner_factory or default_runner
        self.chats, self.saved, self.refused, self.tasks = {}, {}, {}, set()
        self.gate, self.offset, self.username = asyncio.Semaphore(MAX_TURNS), None, None
        self.clock, self.brief_base = clock, (brief_base or BRIEF_BASE).rstrip("/")  # the wall clock: a schedule is a time of day
        self.plans, self.making, self.recording = {}, set(), asyncio.Lock()
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
            if not str(key).lstrip("-").isdigit() or not isinstance(record, dict):
                continue
            session_id, plan = record.get("session_id"), record.get("schedule")
            if isinstance(session_id, str) and session_id:
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
                 "busy": False, "edited": self.now() - EDIT_INTERVAL}
        self.chats[chat_id], self.saved[chat_id] = state, session_id
        self.save()
        return state

    # ---- transport ------------------------------------------------------------------------------

    async def deliver(self, method, body, **payload):
        """Everything goes out as escaped HTML, trimmed to the cap; if Telegram still refuses to parse it,
        resend it flat, and if it is still too long, short - a preview the user has to judge the project by
        may be trimmed but never silently dropped."""
        body = clip(body)
        for text, mode in ((body, "HTML"), (plain(body), None), (clip(plain(body), 900), None)):
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
            status = await self.send(state["id"], esc("Thinking…"))
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
                    elif kind == "message":
                        for piece in split_text(text_of(event.get("text"), 40000, flat=False)):
                            await self.send(state["id"], rich(piece))
                    elif kind == "error":
                        await self.send(state["id"], esc(f"Sorry, that did not work. {text_of(event.get('text'), 600)}"))
        except asyncio.TimeoutError:
            await self.send(state["id"], esc("That took longer than five minutes, so I stopped it. Try a smaller question."))
        except Exception as error:
            self.log(f"turn failed: {error!r}")
            await self.send(state["id"], esc("Something broke on my side. Try again, or send /new to start over."))
        finally:
            state["busy"] = False  # first, so nothing below can leave this chat answering "Still working" for ever
            await stop(typing)
            await stop(ticker)
            with contextlib.suppress(Exception):
                if message_id:
                    await self.edit(state, message_id, esc(turn.summary(self.now() - started)))

    async def ask_confirm(self, state, event):
        confirmation_id = text_of(event.get("confirmation_id"), 64)
        if not CONFIRMATION.fullmatch(confirmation_id):
            return
        body = "<b>Ready to start</b>\n\n" + esc(text_of(event.get("summary"), 1200, flat=False) or "Start this project?")
        await self.send(state["id"], body, markup={"inline_keyboard": [[
            {"text": "Confirm", "callback_data": f"c:{confirmation_id}:1"},
            {"text": "Cancel", "callback_data": f"c:{confirmation_id}:0"}]]})

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

    async def ask(self, path, payload=None):
        """One request to the Morning Brief server on this laptop. Origin is what its same-origin check reads,
        exactly as brief_tools.call sends it."""
        url = f"{self.brief_base}{path}"
        request = self.api.http.get(url) if payload is None else self.api.http.post(url, json=payload, headers={"Origin": self.brief_base})
        async with request as response:
            if path.endswith(".mp3"):
                return response.status, await response.read()
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
            return await self.send(chat_id, esc(f"I could not make your brief. {made['problem']}"))
        brief = made["brief"]
        if made.get("audio") and await self.send_audio(chat_id, brief, made["audio"]):
            return
        script = brief_script(brief)
        await self.send(chat_id, esc("The voice recording did not work this time, so here is your brief to read." if script
                                     else "Your brief was made, but I could not send it here. It is on the Morning Brief page."))
        for piece in split_text(script):
            await self.send(chat_id, esc(piece))

    async def send_audio(self, chat_id, brief, audio):
        try:
            return await self.api.call("sendAudio", upload=("audio", ("morning-brief.mp3", audio, "audio/mpeg")), chat_id=chat_id,
                                       title=text_of(brief.get("title"), 120) or "Morning brief", performer="Morning Brief",
                                       caption=brief_caption(brief))
        except ApiError as error:
            self.log(f"sendAudio refused: {error.status} {error.description[:160]}")
        except NETWORK as error:
            self.log(f"sendAudio failed: {type(error).__name__}")
        return None

    async def on_schedule(self, chat_id, words):
        rest, plan = " ".join(words).lower(), self.plans.get(chat_id)
        if not rest:
            return await self.send(chat_id, esc(
                f"Your morning brief comes every day at {plan['at']} and is about {length_of(plan['minutes'])} long. "
                "Send a new time to change it, or /schedule off to stop." if plan else SCHEDULE_HELP))
        if rest in ("off", "stop"):
            self.plans.pop(chat_id, None)
            self.save()
            return await self.send(chat_id, esc("Done. I will not send a morning brief any more."))
        found = parse_plan(BRIEF_AT if rest == "on" else rest)
        if not found:
            return await self.send(chat_id, esc("I did not understand that time. Try it like this: /schedule 7:00 3"))
        plan = {"at": found[0], "minutes": found[1], "last": None}
        if self.late_by(plan) >= 0:  # today's time has passed: the first one is tomorrow's, not a surprise right now
            plan["last"] = self.clock().date().isoformat()
        self.plans[chat_id] = plan
        self.save()
        await self.send(chat_id, esc(
            (f"The longest a brief can be is {length_of(BRIEF_MINUTES)}, so I set it to that. " if found[2] else "")
            + f"Done. Every day at {plan['at']} I will send you a brief of about {length_of(plan['minutes'])} on what happened "
            f"in the last {BRIEF_HOURS} hours. The first one comes {'tomorrow' if plan['last'] else 'today'}. "
            "The laptop has to be on at that time. Send /brief to hear one now."))

    async def on_brief(self, chat_id, words):
        if chat_id in self.making:
            return await self.send(chat_id, esc("Your brief is already being made. It will arrive here soon."))
        self.making.add(chat_id)  # claimed with no await in between, so two /brief cannot both order a paid recording
        try:
            found = parse_plan(f"{BRIEF_AT} {words[0]}") if words else None
            minutes = found[1] if found else (self.plans.get(chat_id) or {}).get("minutes", BRIEF_MINUTES)
            await self.send(chat_id, esc(f"Making a brief of about {length_of(minutes)} on the last {BRIEF_HOURS} hours. "
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
        await self.send(chat_id, esc(f"This bot is private. Its owner can let you in by adding {chat_id} to "
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
            return await self.send(chat_id, esc(HELP))
        if command == "/id":
            return await self.send(chat_id, esc(f"This chat's ID is {chat_id}"))
        if command == "/schedule":  # neither of these is a model call, so neither waits for a running turn
            return await self.on_schedule(chat_id, text.split()[1:])
        if command == "/brief":
            return await self.on_brief(chat_id, text.split()[1:])
        state = self.chat(chat_id)
        if state["busy"]:
            return await self.send(chat_id, esc("Still working on your previous message."))
        if command == "/new":
            self.chat(chat_id, fresh=True)
            return await self.send(chat_id, esc("Fresh start: I have forgotten what we said before."))
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
        body = esc(text_of(message.get("text"), 1200, flat=False)) + f"\n\n<b>{esc(note)}</b>"
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

    async def run(self):
        try:
            me = await self.api.call("getMe", attempts=1)
            await self.api.call("deleteWebhook", attempts=1)  # long polling and a webhook are mutually exclusive
        except ApiError as error:
            if error.status == 401:
                print(BAD_TOKEN, flush=True)
            else:  # through log(): a description is remote text, and a cp1252 console cannot print all of it
                self.log(f"Telegram refused the first call: {error.description[:200]}")
            return 1
        except NETWORK as error:
            print(f"Could not reach Telegram ({type(error).__name__}). Check this laptop's internet connection.", flush=True)
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
    return token, {int(part) for part in raw if part.lstrip("-").isdigit()}


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
