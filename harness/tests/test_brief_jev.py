# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "duckdb>=1.4,<2", "mcp>=2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_brief_jev.py [--live]

Two streams of work in one file: the audio brief inside the chat (harness/web/brief.js) and the live
reading of posts by Jev (harness/jev_tools.py).

Nothing here talks to the real Jev or the real Bluesky unless --live is given: a fake reader on port
5201 answers the real request shape, and a fake scan feeds score_live the posts a real scan would.
--live adds one real reading of Bluesky at the end, which costs a few pennies of real money.
"""
import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 5201  # 5199 is a mock, 5194 is the product, 5195 is the chat
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HARNESS_REPO", str(ROOT.parent))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

from aiohttp import web  # noqa: E402

import bluesky  # noqa: E402
import jev_tools  # noqa: E402
import steps  # noqa: E402

OUTCOMES = []
TEMP = Path(tempfile.mkdtemp(prefix="signal-brief-jev-"))
REAL_SAMPLE, REAL_SCAN = jev_tools.sample_posts, jev_tools.scan_recent  # kept, because tests put fakes in their place
BANNED = "‒–—―·•‣▪・←→↔⇒⇨➡;"
# Whole words only: "capitals" contains "api" and "phone" contains "hone", and a false alarm here would
# send the next person hunting for a fault that is not there.
JARGON = ("JSON", "UTC", "HTTP", "parquet", "DuckDB", "AppView", "Jetstream", "null", "API", "endpoint",
          "token", "tokens", "boolean", "dict", "parameter", "field", "timestamp")
POST_KEYS = {"text", "short", "full_text", "url", "who", "uri", "handle", "words", "keywords", "id"}
GROUP = re.compile(r"\[g=([a-z_]+)\]")
SCORE = re.compile(r"\[s=(\d)\]")
RELEVANT = re.compile(r"\[r=([\d.]+)\]")


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""), flush=True)


def strings(value, path=""):
    """Every string the code produced, minus the posts themselves, which are the posters' own words."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) not in POST_KEYS:
                yield from strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


# --------------------------------------------------------------- the fake reader
class FakeJev:
    """Answers the real Jev shape, and serves whatever statuses the current test asked for."""

    def __init__(self):
        self.calls, self.bodies, self.plan, self.delay, self.broken = 0, [], [], 0.0, ""

    def expect(self, *statuses, delay=0.0):
        self.calls, self.bodies, self.plan, self.delay, self.broken = 0, [], list(statuses), delay, ""

    def break_with(self, kind):
        """Answer 200 with something that is not the shape we asked for."""
        self.expect(200)
        self.broken = kind

    async def handle(self, request):
        body = await request.json()
        self.calls += 1
        self.bodies.append(body)
        if self.delay:
            await asyncio.sleep(self.delay)
        status = self.plan.pop(0) if len(self.plan) > 1 else (self.plan[0] if self.plan else 200)
        if status != 200:
            return web.json_response({"error": "no"}, status=status)
        if self.broken == "half an answer":
            return web.Response(text='{"answers": {"stance"', content_type="application/json")
        if self.broken == "a web page":
            return web.Response(text="<html><body>too busy</body></html>", content_type="text/html")
        if self.broken == "an odd bill":
            return web.json_response({"answers": {"relevant": {"noul": 0.9}, "stance": {"choice": "worried", "confidence": 0.8},
                                                  "feeling": {"score": 1}}, "usage": [1, 2, 3]})
        state = str(body.get("state") or "")
        answers = {}
        for name, question in (body.get("questions") or {}).items():
            if question["type"] == "noul":
                found = RELEVANT.search(state)
                answers[name] = {"noul": float(found[1]) if found else 0.95}
            elif question["type"] == "choice":
                found = GROUP.search(state)
                options = list((question.get("criteria") or {}))
                answers[name] = {"choice": found[1] if found and found[1] in options else (options[0] if options else None),
                                 "confidence": 0.35 if "[unsure]" in state else 0.85}
            else:
                found = SCORE.search(state)
                answers[name] = {"score": int(found[1]) if found else 2, "confidence": 0.8}
        return web.json_response({"answers": answers, "usage": {"input_tokens": 100}})


async def start_fake(server):
    app = web.Application()
    app.add_routes([web.post("/v1/systemone", server.handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    return runner


# --------------------------------------------------------------- posts a real scan would hand over
def post(key, text, likes=0, extra=""):
    return {"uri": f"at://did:plc:{key}/app.bsky.feed.post/{key}", "url": f"https://bsky.app/profile/did:plc:{key}/post/{key}",
            "handle": f"{key}.bsky.social", "time_label": "10:04", "like_count": likes, "repost_count": 0, "reply_count": 0,
            "langs": ["en"], "text": f"{text} {extra}".strip(), "full_text": f"{text} {extra}".strip()}


LIVE_POSTS = [
    post("w1", "AI is going to take everything and nobody is ready [g=worried][s=0]"),
    post("w2", "AI worries me every single day [g=worried][s=0]", likes=9),
    post("w3", "not sure about this AI thing [g=worried][s=1]"),
    post("h1", "AI just wrote my whole test suite, incredible [g=hopeful][s=4]"),
    post("h2", "AI is getting genuinely good [g=hopeful][s=3]"),
    post("j1", "my AI wrote a poem about my cat [g=joking][s=2]"),
    post("x1", "ai ai captain, sailing today [r=0.05][g=worried][s=0]", likes=100),
]


def fake_scan(posts, matched=None, covered=1.0, notes=None, pause=0.0):
    async def scan(keywords, *, minutes=15, language=None, budget_s=40, connections=12):
        if pause:
            await asyncio.sleep(pause)
        return {"source": "bluesky_live", "mode": "recent", "keywords": keywords, "language": language,
                "window": {"minutes": minutes}, "covered_fraction": covered, "scanned": 12_000,
                "matched": len(posts) if matched is None else matched, "per_bucket": [],
                "examples": list(posts), "seconds": 3.4, "notes": list(notes or [])}
    return scan


# --------------------------------------------------------------- 1. asking the reader
async def asking(server):
    questions = jev_tools.live_questions(["AI"])
    server.expect(200)
    answers, reasons, tokens = await jev_tools.score_texts(["AI is fine [g=hopeful][s=3]", "AI is scary [g=worried][s=0]"], questions)
    check("1a every post gets one request carrying every question",
          server.calls == 2 and all(sorted(body["questions"]) == ["feeling", "relevant", "stance"] for body in server.bodies)
          and all(body["model"] == "jev-latest" for body in server.bodies),
          f"{server.calls} requests")
    check("1b the answers come back in the order the posts went in, with no failures",
          len(answers) == 2 and jev_tools.choice_of(answers[0], "stance")[0] == "hopeful"
          and jev_tools.choice_of(answers[1], "stance")[0] == "worried" and not reasons,
          f"{[jev_tools.choice_of(answer, 'stance')[0] for answer in answers]}")
    check("1c a five level feeling is reported from minus 1 to plus 1",
          jev_tools.feeling_of(answers[0], "feeling") == 0.5 and jev_tools.feeling_of(answers[1], "feeling") == -1.0,
          f"{jev_tools.feeling_of(answers[0], 'feeling')} and {jev_tools.feeling_of(answers[1], 'feeling')}")
    check("1d the cost is the tokens the reader reported, at 0.042 dollars per million",
          tokens == 200 and jev_tools.cost_of(tokens) == round(200 * 0.042 / 1e6, 6),
          f"{tokens} tokens, ${jev_tools.cost_of(tokens)}")

    server.expect(429, 200)
    answers, reasons, _tokens = await jev_tools.score_texts(["AI is fine [g=hopeful]"], questions)
    check("1e a busy reader is tried again", server.calls == 2 and answers[0] is not None and not reasons,
          f"{server.calls} attempts")

    server.expect(529, 529, 529)
    answers, reasons, _tokens = await jev_tools.score_texts(["AI is fine [g=hopeful]"], questions)
    check("1f three attempts and then an honest failure",
          server.calls == 3 and answers[0] is None and reasons == ["the reader was busy"], f"{reasons}")

    server.expect(401)
    answers, reasons, _tokens = await jev_tools.score_texts(["AI is fine"], questions)
    check("1g a refusal is not tried again, and reads as plain words",
          server.calls == 1 and answers[0] is None and reasons == ["the reader refused our request"], f"{reasons}")

    server.expect(200, delay=1.5)
    started = time.monotonic()
    answers, reasons, _tokens = await jev_tools.score_texts(["AI is fine"], questions, budget_s=0.4)
    check("1h the whole scoring stops at its budget instead of waiting",
          time.monotonic() - started < 3 and answers[0] is None and len(reasons) == 1,
          f"{time.monotonic() - started:.1f} seconds, {reasons}")
    server.expect(200)


# --------------------------------------------------------------- 2. how people feel right now
def live_scoring(server):
    jev_tools.scan_recent = fake_scan(LIVE_POSTS)
    server.expect(200)
    result = jev_tools.score_live(["AI"], minutes=15, max_posts=150)
    groups = {group["group"]: group for group in result.get("groups") or []}
    check("2a every matching post is read, and the counts are honest",
          result["posts_read"] == 7 and result["posts_about_the_topic"] == 6 and result["posts_about_something_else"] == 1
          and result["posts_we_could_not_read"] == 0,
          f"read {result['posts_read']}, on topic {result['posts_about_the_topic']}")
    check("2b a post that only looks like the topic is left out of the shares",
          groups["Worried"]["posts"] == 3 and groups["Worried"]["percent"] == 50 and groups["Hopeful"]["percent"] == 33,
          ", ".join(f"{name} {group['percent']}%" for name, group in groups.items()))
    check("2c the feeling is averaged twice, plainly and by how liked the posts were",
          result["feeling"]["average"] == -0.167 and result["feeling"]["average_when_popular_posts_count_more"] == -0.667,
          f"{result['feeling']}")
    examples = result["examples"][0]
    check("2d the biggest group brings three example posts, most liked first, each with its link",
          examples["group"] == "Worried" and len(examples["posts"]) == 3
          and examples["posts"][0]["likes"] == 9 and examples["posts"][0]["url"].startswith("https://bsky.app/"),
          f"{examples['group']}, {[row['likes'] for row in examples['posts']]}")
    check("2e the cost is the reader's own tokens", result["cost_usd"] == round(700 * 0.042 / 1e6, 6), f"${result['cost_usd']}")
    card = result["_card"]
    check("2f a chart card goes to the page, with bars and no picture",
          card["kind"] == "chart" and card["title"] == 'How Bluesky feels about "AI" right now' and "png_url" not in card
          and [bar["label"] for bar in card["bars"]] == ["Worried", "Hopeful", "Joking"]
          and abs(sum(bar["share"] for bar in card["bars"]) - 1) < 0.01,
          f"{card['title']}, {len(card['bars'])} bars")
    check("2g the caption says what the numbers are made of",
          card["caption"] == "From 6 posts in the last 15 minutes. Most of them sound worried.", card["caption"])

    jev_tools.scan_recent = fake_scan(LIVE_POSTS, covered=0.6)
    half = jev_tools.score_live(["AI"], minutes=15)
    check("2h a half read window says so before anything else",
          half["notes"][0].startswith("Only about 60 percent"), half["notes"][0])

    jev_tools.scan_recent = fake_scan([], matched=0)
    empty = jev_tools.score_live(["AI"], minutes=15)
    check("2i nothing matched is said plainly and costs nothing",
          empty["posts_read"] == 0 and empty["cost_usd"] == 0.0 and "nothing to read" in empty["notes"][0],
          empty["notes"][0])

    jev_tools.scan_recent = fake_scan(LIVE_POSTS)
    server.expect(500, 500, 500)
    broken = jev_tools.score_live(["AI"], minutes=15)
    check("2j when the reader is down, the failure is counted and nothing is invented",
          broken["posts_read"] == 0 and broken["posts_we_could_not_read"] == 7 and not broken["groups"]
          and any("could not be read" in note for note in broken["notes"]),
          f"{broken['posts_we_could_not_read']} unread")
    server.expect(200)

    bad = [jev_tools.score_live([], minutes=15), jev_tools.score_live(["AI"], minutes=900),
           jev_tools.score_live(["AI"], language="klingon"), jev_tools.score_live(["AI"], max_posts=5_000)]
    check("2k bad arguments are refused in words a person can read",
          all("error" in answer for answer in bad)
          and all(answer["error"]["message"] and answer["error"]["hint"] for answer in bad),
          ", ".join(answer["error"]["code"] for answer in bad))
    return result


# --------------------------------------------------------------- 3. trying a draft's own questions
DRAFT = {
    "name": "How people talk about AI",
    "keywords": ["AI", "chatbot"],
    "language": "en",
    "observation": {"intent": "How people talk about AI", "source": "twitter_firehose",
                    "window": {"from": "2026-09-01", "to": "2026-09-14"}},
    "categories": [{"name": "worried", "description": "Afraid of what AI will do"},
                   {"name": "hopeful", "description": "Excited about what AI can do"},
                   {"name": "jobs", "description": "About AI taking jobs"},
                   {"name": "art", "description": "About AI and drawing or music"}],
    "sentiment_question": "How strong is the feeling in this post about AI?",
}
DRAFT_POSTS = [
    {"text": "AI scares me [g=worried][s=0]", "short": "AI scares me", "when": "2026-09-12", "likes": 4, "url": "https://x.com/i/status/1"},
    {"text": "AI is going to end us [g=worried][s=0]", "short": "AI is going to end us", "when": "2026-09-12", "likes": 0, "url": "https://x.com/i/status/2"},
    {"text": "AI worries me [g=worried][s=1][unsure]", "short": "AI worries me", "when": "2026-09-13", "likes": 1, "url": "https://x.com/i/status/3"},
    {"text": "AI cured my inbox [g=worried][s=3][unsure]", "short": "AI cured my inbox", "when": "2026-09-13", "likes": 2, "url": "https://x.com/i/status/4"},
    {"text": "AI is unreal [g=hopeful][s=4]", "short": "AI is unreal", "when": "2026-09-13", "likes": 7, "url": "https://x.com/i/status/5"},
    {"text": "AI made my day [g=hopeful][s=4]", "short": "AI made my day", "when": "2026-09-13", "likes": 3, "url": "https://x.com/i/status/6"},
]


def with_draft(draft):
    directory = TEMP / "session"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    os.environ["HARNESS_SESSION_DIR"] = str(directory)


def trying(server):
    with_draft(DRAFT)
    jev_tools.sample_posts = lambda *arguments: (list(DRAFT_POSTS), None)
    server.expect(200)
    result = jev_tools.try_questions(20)
    split = {group["group"]: group["posts"] for group in result["groups"]}
    check("3a the draft's own groups are the ones tried",
          sorted(server.bodies[0]["questions"]["group"]["criteria"]) == ["art", "hopeful", "jobs", "worried"]
          and server.bodies[0]["questions"]["feeling"]["instructions"] == DRAFT["sentiment_question"],
          str(sorted(server.bodies[0]["questions"])))
    check("3b the split is counted from the real answers",
          split == {"worried": 4, "hopeful": 2} and result["posts_read"] == 6, str(split))
    check("3c the groups that caught nothing are named",
          result["groups_that_caught_nothing"] == ["jobs", "art"], str(result["groups_that_caught_nothing"]))
    check("3d the verdict says what to do about it",
          result["verdict"] == "Two groups caught almost nothing. Consider merging them.", result["verdict"])
    check("3e the posts the reader was least sure about come back, with their links",
          len(result["hard_to_place"]) == 3 and result["hard_to_place"][0]["how_sure"] == 0.35
          and all(row["url"].startswith("https://") for row in result["hard_to_place"]),
          str([row["how_sure"] for row in result["hard_to_place"]]))
    check("3f the days tried are said the way a person reads them",
          result["days"] == "Sep 11 to Sep 13, 2026" and result["cost_usd"] == round(600 * 0.042 / 1e6, 6),
          f"{result['days']}, ${result['cost_usd']}")

    remix = ("jobs", "jobs", "art", "worried", "hopeful", "hopeful")
    even = [dict(row, text=re.sub(r"\[g=\w+\]", f"[g={remix[index]}]", row["text"])) for index, row in enumerate(DRAFT_POSTS)]
    jev_tools.sample_posts = lambda *arguments: (even, None)
    server.expect(200)
    spread = jev_tools.try_questions(20)
    check("3g a sensible split is called sensible",
          spread["verdict"] == "The groups split these posts sensibly."
          or spread["verdict"].startswith("The reader was unsure"), spread["verdict"])

    one_group = dict(DRAFT, categories=DRAFT["categories"][:1])
    with_draft(one_group)
    thin = jev_tools.try_questions(20)
    with_draft(DRAFT)
    no_draft = TEMP / "empty"
    no_draft.mkdir(exist_ok=True)
    os.environ["HARNESS_SESSION_DIR"] = str(no_draft)
    missing = jev_tools.try_questions(20)
    os.environ["HARNESS_SESSION_DIR"] = str(TEMP / "session")
    check("3h a project with nothing to try says so instead of failing",
          thin["error"]["code"] == "no_groups" and missing["error"]["code"] == "no_draft",
          f"{thin['error']['code']}, {missing['error']['code']}")

    windows = [jev_tools.try_window(DRAFT), jev_tools.try_window({}),
               jev_tools.try_window({"observation": {"window": {"from": "2026-09-12", "to": "2026-09-14"}}})]
    check("3i the window is at most three days and never outside the days we have",
          windows[0][:2] == ("2026-09-11", "2026-09-14") and windows[0][2]
          and windows[1][:2] == ("2026-09-14", "2026-09-17") and windows[1][2]
          and windows[2][:2] == ("2026-09-12", "2026-09-14") and not windows[2][2],
          str([window[:2] for window in windows]))
    return result


# --------------------------------------------------------------- 4. the words the activity list shows
def wording(live, tried):
    title = steps.title("mcp__harness__score_live", {"keywords": ["AI"], "minutes": 15, "max_posts": 150})
    outcome = steps.outcome("mcp__harness__score_live", live)
    facts = steps.facts("mcp__harness__score_live", {"keywords": ["AI"], "minutes": 15, "language": "en"})
    check("4a the step says what is being asked, in plain words",
          title == 'Asking Jev to read 150 Bluesky posts about "AI"', title)
    check("4b the outcome is written from the real result",
          outcome == "Read 6 posts. 50 percent sound worried and 33 percent sound hopeful.", outcome)
    check("4c the details panel shows the words, the time and the language",
          [row["label"] for row in facts] == ["Words searched", "Time covered", "Language"]
          and facts[2]["value"] == "English" and facts[1]["value"] == "the last 15 minutes", str(facts))
    try_title = steps.title("mcp__harness__try_questions", {"sample_size": 20})
    try_outcome = steps.outcome("mcp__harness__try_questions", tried)
    check("4d trying the questions reads the same way",
          try_title == "Trying your groups on 20 real posts"
          and try_outcome == "Read 6 posts. Two groups caught almost nothing. Consider merging them.",
          f"{try_title} / {try_outcome}")
    failed = steps.outcome("mcp__harness__score_live", {"error": {"code": "no_reader", "message": "The post reader is not switched on for this laptop."}}, True)
    check("4e a failed step still says one plain line",
          failed == "That did not work: The post reader is not switched on for this laptop.", failed)


# --------------------------------------------------------------- 5. the tools the model is given
class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def add(function):
            self.tools[function.__name__] = function
            return function
        return add


def tool_shapes():
    server = FakeMCP()
    jev_tools.register(server)
    names = sorted(server.tools)
    first = {name: list(inspect.signature(tool).parameters)[0] for name, tool in server.tools.items()}
    check("5a both tools are registered and take a reason first",
          names == ["score_live", "try_questions"] and set(first.values()) == {"reason"}, f"{names}, {first}")
    refused = server.tools["score_live"]("", ["AI"])
    check("5b a step with no reason does not run", refused["error"]["code"] == "no_reason", refused["error"]["code"])
    jev_tools.scan_recent = fake_scan(LIVE_POSTS)
    ran = server.tools["score_live"]("Checking how Bluesky sounds about AI right now.", ["AI"], 15, "en", 5)
    check("5c a step with a reason runs and reads at most the posts it was given",
          ran.get("posts_read") == 5, f"{ran.get('posts_read')} posts")


# --------------------------------------------------------------- 6. everything a person can read
def plain_words(*results):
    bad = []
    for result in results:
        for path, value in strings(result):
            if any(mark in value for mark in BANNED):
                bad.append(f"{path}: {value[:60]}")
            for word in JARGON:
                if re.search(rf"\b{re.escape(word)}\b", value, re.I):
                    bad.append(f"{path}: {word}")
    check("6a nothing the user reads carries a dash, a bullet, an arrow or a semicolon, or any jargon",
          not bad, "; ".join(bad[:3]) if bad else "clean")
    sources = [(ROOT / "prompts/brief.md").read_text(encoding="utf-8"), (ROOT / "prompts/jev.md").read_text(encoding="utf-8")]
    check("6b both prompt fragments exist and are short plain prose",
          all(0 < len(text.splitlines()) <= 40 for text in sources)
          and not any(mark in text for text in sources for mark in "‒–—―·•‣▪・←→↔⇒⇨➡"),
          f"{[len(text.splitlines()) for text in sources]} lines")
    browser = (ROOT / "web/brief.js").read_text(encoding="utf-8")
    sentences = re.findall(r"text:\s*'([^']+)'|=\s*'([A-Z][^']{6,})'", browser)
    said = [one or two for one, two in sentences]
    check("6c the card's own sentences are plain too",
          said and not any(mark in line for line in said for mark in BANNED), f"{len(said)} sentences")
    # A guard written with the characters it removes hides them in its own source, where a tidy edit
    # deletes one without a trace and nothing fails. These two files say the marks as numbers instead.
    mine = {name: (ROOT / name).read_text(encoding="utf-8")
            for name in ("web/brief.js", "jev_tools.py", "web/brief.css", "prompts/brief.md", "prompts/jev.md")}
    hiding = [f"{name} holds {ord(letter):#06x}" for name, text in mine.items() for letter in text
              if any(low <= ord(letter) <= high for low, high in jev_tools.HIDDEN_RANGES) and letter not in "\t\n\r"]
    check("6e no hidden mark is hiding in the code that takes hidden marks out", not hiding, hiding[0] if hiding else "clean")
    check("6d the card builds on the chat's exports and writes text as text",
          "from '/chat.js'" in browser and "innerHTML" not in browser
          and all(name in browser for name in ("onEvent", "appendCard", "plainText", "el(")),
          "onEvent, appendCard, plainText, el")


# --------------------------------------------------------------- 7. the card in the browser
STUB = """
export function el(tag, props = {}, ...children) {
  const node = { tag, attrs: {}, textContent: '', children: children.filter(Boolean) };
  for (const [key, value] of Object.entries(props)) {
    if (key === 'text') node.textContent = String(value);
    else node.attrs[key] = value;
  }
  node.append = (...items) => { node.children.push(...items); };
  node.replaceChildren = (...items) => { node.children = items; };
  return node;
}
export function plainText(value) {
  return typeof value === 'string' ? value.replace(/[^\\S\\n]*[\\u2012-\\u2015]+[^\\S\\n]*/g, ', ').replace(/;/g, '.') : '';
}
export function onEvent(fn) { globalThis.__listener = fn; return () => {}; }
export function appendCard(node) { (globalThis.__cards = globalThis.__cards || []).push(node); return node; }
export async function send() {}
"""

RUNNER = """
import { readFileSync } from 'node:fs';
import * as brief from './brief.js';

const checks = [];
const check = (name, ok, detail = '') => checks.push([name, Boolean(ok), String(detail)]);
const sample = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url), 'utf-8'));
const text = (node) => [node.textContent, ...(node.children || []).map(text)].filter(Boolean).join(' | ');
const find = (node, tag) => node.tag === tag ? node : (node.children || []).map((child) => find(child, tag)).find(Boolean);

check('7a the server steps read as plain words', [
  brief.statusWords({ status: 'working', step: 'Starting' }),
  brief.statusWords({ status: 'working', step: 'Checking engagement on Bluesky' }),
  brief.statusWords({ status: 'working', step: 'Claude is choosing the stories and writing the script' }),
  brief.statusWords({ status: 'working', step: 'ElevenLabs is recording the audio' }),
  brief.statusWords({ status: 'ready', step: '' }),
].join(' / ') === 'Choosing the stories. / Choosing the stories. / Writing the script. / Recording the voice. / Ready.',
  [brief.statusWords({ status: 'working', step: 'Starting' }), brief.statusWords({ status: 'ready' })].join(' / '));

check('7b a failure the server wrote for itself becomes one plain line',
  brief.failureWords('No posts have been collected for these interests in this window yet.')
    === 'No posts have been collected for these interests in this window yet.'
  && brief.failureWords('Unexpected error: KeyError(\\'audio\\')') === 'That did not work. Ask me to make a new one.',
  brief.failureWords('Unexpected error: KeyError(1)'));

check('7c the story headlines come out of the real brief shape',
  brief.storyLines(sample).length === 5 && brief.storyLines(sample)[0].startsWith('Trump plans an'),
  brief.storyLines(sample)[0]);

check('7d the player points at the recording, and never anywhere else',
  brief.audioSrc(sample) === '/api/briefs/20260919-162012/audio/brief.mp3'
  && brief.audioSrc({ id: 'x', audio: { full: '../../secret.mp3' } }) === null
  && brief.audioSrc({ id: 'x' }) === null,
  String(brief.audioSrc(sample)));

check('7e polling stops when it is ready, when it failed, and when it has asked too often',
  brief.decide({ brief: { status: 'ready' } }, { tries: 1 }).stop === true
  && brief.decide({ brief: { status: 'failed', step: 'No posts yet.' } }, { tries: 1 }).stop === true
  && brief.decide({ brief: { status: 'working', step: 'Starting' } }, { tries: 1 }).stop === false
  && brief.decide({ brief: { status: 'working' } }, { tries: brief.CONFIG.maxPolls }).words === brief.TOO_LONG,
  'ready, failed, working, too long');

check('7f the chat alone says so once and stops asking',
  brief.decide({ missing: true, api: false }, {}).words === brief.ONE_APP
  && brief.decide({ missing: true, api: false }, {}).stop === true
  && brief.decide({ missing: true, api: true }, {}).words === brief.GONE
  && brief.decide({ lost: true }, { lost: 1 }).stop === false
  && brief.decide({ lost: true }, { lost: brief.CONFIG.lostLimit }).stop === true,
  brief.ONE_APP);

brief.CONFIG.pollMs = 1;
const plan = [
  { status: 200, body: { id: '20260919-162012', status: 'working', step: 'Checking engagement on Bluesky' } },
  { status: 200, body: { id: '20260919-162012', status: 'working', step: 'ElevenLabs is recording the audio' } },
  { status: 200, body: sample },
];
let asked = [];
globalThis.fetch = async (url) => {
  asked.push(url);
  const next = plan.shift() || plan[plan.length - 1];
  return { status: next.status, ok: next.status < 400, json: async () => next.body };
};
const view = brief.briefCard({ brief_id: '20260919-162012', title: '', status: 'working' });
await brief.follow(view);
const player = find(view.node, 'audio');
check('7g the card follows the brief and ends with the player and the stories',
  view.words === 'Ready.' && asked.length === 3 && asked[0] === '/api/briefs/20260919-162012'
  && player && player.attrs.src === '/api/briefs/20260919-162012/audio/brief.mp3' && player.attrs.controls === true
  && text(view.node).includes('Trump plans an'),
  `${asked.length} reads, ${view.words}`);
check('7h it never tries to play by itself',
  player && player.attrs.autoplay === undefined && player.attrs.preload === 'none' && !/\\.play\\(/.test(String(brief.follow)),
  'no autoplay, nothing plays itself');

asked = [];
globalThis.fetch = async (url) => {
  asked.push(url);
  return { status: 404, ok: false, json: async () => ({ error: 'No such file.' }) };
};
const alone = brief.briefCard({ brief_id: '20260919-162012' });
await brief.follow(alone);
check('7i on the chat server alone it asks once and says one plain line',
  alone.words === brief.ONE_APP && asked.length === 1, `${asked.length} read, ${alone.words}`);

globalThis.__cards = [];
globalThis.fetch = async () => ({ status: 200, ok: true, json: async () => sample });
brief.show({ brief_id: '20260919-162012', title: 'Your morning brief', status: 'working' });
brief.show({ brief_id: '20260919-162012', title: 'Your morning brief', status: 'ready' });
await new Promise((done) => setTimeout(done, 30));
check('7j the same brief seen twice updates one card instead of adding another',
  globalThis.__cards.length === 1 && text(globalThis.__cards[0]).includes('Ready.')
  && text(globalThis.__cards[0]).includes('Trump plans an'),
  `${globalThis.__cards.length} card`);

check('7k a card from another stream of work is left alone',
  typeof globalThis.__listener === 'function'
  && (globalThis.__listener({ type: 'card', card: { kind: 'chart' } }), globalThis.__cards.length === 1),
  'only brief cards');

// A headline is written from real posts, so a post can push its own text into one.
const monster = { id: 'm', status: 'ready', title: 'T'.repeat(9000), audio: { full: 'brief.mp3' },
  segments: [{ stories: [{ title: 'S'.repeat(9000) }] }] };
const monsterCard = brief.briefCard({ brief_id: 'm' });
monsterCard.update(monster);
check('7m a headline as long as a page becomes one short line',
  brief.storyLines(monster)[0].length === brief.CONFIG.line
  && find(monsterCard.node, 'h3').textContent.length === brief.CONFIG.title,
  `${brief.storyLines(monster)[0].length} and ${find(monsterCard.node, 'h3').textContent.length} characters`);

const flipped = { id: 'f', status: 'ready', title: 'Brief \\u202Eevil', audio: { full: 'brief.mp3' },
  segments: [{ stories: [{ title: 'Story \\u202E gnihtemos\\u0000 \\u200B' }] }] };
const flippedCard = brief.briefCard({ brief_id: 'f', title: 'Card \\u202E flipped' });
flippedCard.update(flipped);
check('7n a mark that would draw the card backwards is taken out before it is shown',
  !/[\\u0000-\\u0008\\u000b-\\u001f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]/.test(text(flippedCard.node))
  && brief.storyLines(flipped)[0] === 'Story gnihtemos'
  && !/\\n/.test(brief.storyLines(flipped)[0]),
  JSON.stringify(brief.storyLines(flipped)[0]));

// One answer it cannot use must not end the following, and nothing here may reach the page as a
// broken promise: the chat only guards the listener it calls, not the polling that listener starts.
const broken = [];
process.on('unhandledRejection', (reason) => broken.push(String(reason)));
globalThis.__cards = [];
let reads = 0;
globalThis.fetch = async () => {
  reads += 1;
  if (reads === 1) throw new Error('the network is on fire');
  if (reads === 2) return { status: 200, ok: true, json: async () => ({ id: 'g', status: 'working', get segments() { throw new Error('nope'); } }) };
  return { status: 200, ok: true, json: async () => sample };
};
brief.show({ brief_id: 'guarded' });
await new Promise((done) => setTimeout(done, 60));
check('7o an answer it cannot use does not stop it following the brief',
  reads >= 3 && globalThis.__cards.length === 1 && text(globalThis.__cards[0]).includes('Ready.'),
  `${reads} reads`);
check('7p nothing here reaches the page as a broken promise', broken.length === 0, broken[0] || 'none');

for (const row of checks) console.log('CHECK ' + JSON.stringify(row));
"""

FIXTURE = {
    "id": "20260919-162012", "status": "ready", "step": "",
    "title": 'Trump\'s "AI Force" and a Truth Social poll to rename AI',
    "hours": 8, "seconds": 60, "estimated_seconds": 63,
    "audio": {"full": "brief.mp3", "voice": None, "characters": 781},
    "notes": [],
    "segments": [{
        "topic": "AI", "headline": "Trump plans an AI Force and asks followers to rename AI",
        "posts_collected": 412, "distinct_authors": 388,
        "stories": [{"title": "Trump plans an AI Force and an AI czar and rejects new limits", "summary": "", "posts": []},
                    {"title": "Trump asks his followers to rename AI", "summary": "", "posts": []},
                    {"title": "Reports that AI played a role in military intelligence failures", "summary": "", "posts": []},
                    {"title": "Anger over AI deciding insurance claims", "summary": "", "posts": []},
                    {"title": "Google says Gemini hacked companies during a test", "summary": "", "posts": []}],
    }],
}


def browser():
    folder = TEMP / "web"
    folder.mkdir(parents=True, exist_ok=True)
    source = (ROOT / "web/brief.js").read_text(encoding="utf-8").replace("'/chat.js'", "'./chat.js'")
    (folder / "brief.js").write_text(source, encoding="utf-8")
    (folder / "chat.js").write_text(STUB, encoding="utf-8")
    (folder / "run.mjs").write_text(RUNNER, encoding="utf-8")
    (folder / "fixture.json").write_text(json.dumps(FIXTURE), encoding="utf-8")
    try:
        done = subprocess.run(["node", str(folder / "run.mjs")], capture_output=True, text=True, timeout=90, cwd=str(folder))
    except (OSError, subprocess.TimeoutExpired) as error:
        check("7 the card runs in a browser engine", False, f"node could not run it: {error!r}")
        return
    for line in done.stdout.splitlines():
        if line.startswith("CHECK "):
            name, ok, detail = json.loads(line[6:])
            check(name, ok, detail)
    if done.returncode or "CHECK " not in done.stdout:
        check("7 the card's own checks all ran", False, (done.stderr or done.stdout)[-400:].replace("\n", " "))
    check("7l the card brings its own stylesheet",
          (ROOT / "web/brief.css").exists() and 'href="/brief.css"' in (ROOT / "web/brief.js").read_text(encoding="utf-8"),
          "brief.css")


# --------------------------------------------------------------- 8. the saved posts really answer
def archive():
    jev_tools.sample_posts = REAL_SAMPLE  # the fake from section 3 would otherwise answer for the archive
    if not list((ROOT.parent / "twitter-firehose").glob("tweets-*.parquet")):
        check("8a the saved posts can be sampled the way the preview counts them", True, "no archive on this machine, skipped")
        return
    started = time.monotonic()
    posts, problem = jev_tools.sample_posts(["AI"], "en", "2026-09-16", "2026-09-17", 5)
    check("8a the saved posts can be sampled the way the preview counts them",
          problem is None and posts and len(posts) == 5 and all(row["text"].strip() for row in posts)
          and all(row["url"].startswith("https://x.com/") for row in posts),
          f"{len(posts or [])} posts in {time.monotonic() - started:.1f} seconds")


# --------------------------------------------------------------- 9. a careless or hostile turn
# Nothing a service, a post or the model itself can send may make a tool raise: a tool that raises
# leaves the model with nothing to say and the step row hanging. Every case below used to.
HIDDEN = "‮"  # a right to left override: every character after it is drawn backwards


def hostile(server):
    jev_tools.scan_recent = fake_scan(LIVE_POSTS[:2])
    answers = {}
    for kind in ("half an answer", "a web page", "an odd bill"):
        server.break_with(kind)
        answers[kind] = jev_tools.score_live(["AI"], 15)
    server.expect(200)
    check("9a an answer we cannot read is counted as unread, and the tool still answers",
          all(isinstance(answer, dict) and "error" not in answer for answer in answers.values())
          and answers["half an answer"]["posts_we_could_not_read"] == 2
          and answers["a web page"]["posts_we_could_not_read"] == 2
          and answers["an odd bill"]["posts_read"] == 2 and answers["an odd bill"]["cost_usd"] == 0.0,
          ", ".join(f"{kind}: {answer['posts_we_could_not_read']} unread" for kind, answer in answers.items()))

    broken = {"the posts are a word": {"matched": 2, "examples": "nope", "covered_fraction": 1.0},
              "the posts are strings": {"matched": 2, "examples": ["a", "b"], "covered_fraction": 1.0},
              "a post is missing": {"matched": 2, "examples": [None], "covered_fraction": 1.0},
              "the count is a word": {"matched": "many", "examples": [], "covered_fraction": "most"}}
    scanned = {}
    for name, found in broken.items():
        async def scan(keywords, *, minutes=15, language=None, budget_s=40, connections=12, _f=found):
            return dict(_f)
        jev_tools.scan_recent = scan
        scanned[name] = jev_tools.score_live(["AI"], 15)
    check("9b a scan that comes back in a shape we did not ask for is handled, not crashed into",
          all(isinstance(answer, dict) and "error" not in answer and answer["posts_read"] == 0
              for answer in scanned.values())
          and scanned["the count is a word"]["posts_found"] == 0
          and scanned["the count is a word"]["covered_fraction"] is None,
          ", ".join(f"{name}: {answer['posts_read']} read" for name, answer in scanned.items()))

    odd = [dict(LIVE_POSTS[0], like_count="9"), dict(LIVE_POSTS[1], like_count=None),
           dict(LIVE_POSTS[3], like_count=float("nan")), dict(LIVE_POSTS[4], like_count=-5)]
    jev_tools.scan_recent = fake_scan(odd)
    counted = jev_tools.score_live(["AI"], 15)
    check("9c a like count that is not a number does not break the averages",
          isinstance(counted.get("feeling", {}).get("average"), float)
          and isinstance(counted["feeling"]["average_when_popular_posts_count_more"], float)
          and all(isinstance(row["likes"], int) and row["likes"] >= 0
                  for group in counted["examples"] for row in group["posts"]),
          f"{counted['feeling']['average']} plain, {counted['feeling']['average_when_popular_posts_count_more']} by likes")

    jev_tools.scan_recent = fake_scan(LIVE_POSTS[:2])
    refused = [jev_tools.score_live([None], 15), jev_tools.score_live([7], 15),
               jev_tools.score_live([{"term": "AI"}], 15), jev_tools.score_live(["AI", None], 15)]
    check("9d a search word that is not text is refused in plain words instead of raising",
          all(answer.get("error", {}).get("code") == "bad_words" for answer in refused),
          ", ".join(answer["error"]["code"] for answer in refused))

    titles = {}
    for name, word in (("a dash", "AI — now"), ("a semicolon", "AI; robots"), ("a bullet", "AI · jobs"),
                       ("a hidden mark", f"AI{HIDDEN} evil"), ("two lines", "AI\nHACK")):
        titles[name] = jev_tools.score_live([word], 15)["_card"]["title"]
    check("9e the card's own title is plain even when the words the user typed are not",
          not any(mark in title for title in titles.values() for mark in BANNED + HIDDEN)
          and titles["a dash"] == 'How Bluesky feels about "AI, now" right now'
          and titles["two lines"] == 'How Bluesky feels about "AI HACK" right now',
          titles["a semicolon"])

    with_draft(dict(DRAFT, categories=[{"name": "worried — scared", "description": "d"},
                                       {"name": f"hopeful; glad{HIDDEN}", "description": "d"},
                                       {"name": "jobs · work", "description": "d"}]))
    jev_tools.sample_posts = lambda *arguments: (list(DRAFT_POSTS[:2]), None)
    server.expect(200)
    named = jev_tools.try_questions(20)
    shown = [group["group"] for group in named["groups"]] + named["groups_that_caught_nothing"]
    check("9f a group name from the project is cleaned before the user reads it back",
          shown and not any(mark in name for name in shown for mark in BANNED + HIDDEN)
          and "worried, scared" in shown and "jobs, work" in shown, str(shown))
    with_draft(DRAFT)

    # Two readings in one turn run on two threads. Whichever finishes last must hand the scan's own
    # limits back as they were, or every later scan of every other tool stays widened for the session.
    caps = (bluesky.MAX_EXAMPLES, bluesky.HYDRATE_LIMIT)

    def reading(pause):
        jev_tools.scan_recent = fake_scan([], matched=0, pause=pause)
        jev_tools.score_live(["AI"], 15, max_posts=120)

    quick = threading.Thread(target=reading, args=(0.05,))
    quick.start()
    time.sleep(0.02)
    slow = threading.Thread(target=reading, args=(0.5,))
    slow.start()
    quick.join()
    slow.join()
    check("9g two readings at once put the scan's own limits back",
          (bluesky.MAX_EXAMPLES, bluesky.HYDRATE_LIMIT) == caps,
          f"{caps} before, {(bluesky.MAX_EXAMPLES, bluesky.HYDRATE_LIMIT)} after")

    huge = {"keywords": ["AI"], "minutes": 10 ** 12, "max_posts": 10 ** 30}
    check("9h a step written from an argument the tool will refuse still reads sanely",
          steps.title("score_live", huge) == 'Asking Jev to read 160 Bluesky posts about "AI"'
          and steps.facts("score_live", huge)[1]["value"] == "the last 60 minutes"
          and steps.title("try_questions", {"sample_size": 10 ** 20}) == "Trying your groups on 40 real posts",
          f"{steps.title('score_live', huge)} / {steps.facts('score_live', huge)[1]['value']}")
    return [answers["an odd bill"], counted, named]


# --------------------------------------------------------------- the one real reading
def live_check():
    os.environ.pop("TYPESAFE_API_BASE", None)
    jev_tools.scan_recent = REAL_SCAN
    started = time.monotonic()
    result = jev_tools.score_live(["AI"], minutes=5, max_posts=40)
    if "error" in result:
        print(f"LIVE failed: {result['error']['code']}: {result['error']['message']}", flush=True)
        return
    print(f"\nLIVE score_live(['AI'], minutes=5, max_posts=40) in {time.monotonic() - started:.1f} seconds", flush=True)
    print(f"  found {result['posts_found']} posts, read {result['posts_read']}, on topic {result['posts_about_the_topic']}, "
          f"could not read {result['posts_we_could_not_read']}, covered {result['covered_fraction']}", flush=True)
    for group in result["groups"]:
        print(f"  {group['group']}: {group['posts']} posts, {group['percent']} percent", flush=True)
    print(f"  feeling {result['feeling']['average']} plain, {result['feeling']['average_when_popular_posts_count_more']} by likes", flush=True)
    print(f"  cost ${result['cost_usd']:.6f}", flush=True)
    print(f"  card: {result['_card']['title']} | {result['_card']['caption']}", flush=True)
    print(f"  step: {steps.title('score_live', {'keywords': ['AI'], 'minutes': 5, 'max_posts': 40})}", flush=True)
    print(f"  said: {steps.outcome('score_live', result)}", flush=True)
    for note in result["notes"]:
        print(f"  note: {note}", flush=True)
    example = (result["examples"][0]["posts"] or [{}])[0] if result["examples"] else {}
    print(f"  example: {str(example.get('text'))[:120]} {example.get('url')}", flush=True)


async def main():
    os.environ["TYPESAFE_API_BASE"] = BASE
    os.environ.setdefault("TYPESAFE_API_KEY", "test-only-not-a-real-key")
    server = FakeJev()
    runner = await start_fake(server)
    try:
        await asking(server)
        # The tools are plain blocking functions and the fake reader lives in this event loop, so a tool
        # called from this thread would wait for an answer the loop cannot write. Each one gets a thread.
        live = await asyncio.to_thread(live_scoring, server)
        tried = await asyncio.to_thread(trying, server)
        wording(live, tried)
        await asyncio.to_thread(tool_shapes)
        rough = await asyncio.to_thread(hostile, server)
        plain_words(live, tried, jev_tools.score_live([], minutes=1), *rough)
        await asyncio.to_thread(browser)
        await asyncio.to_thread(archive)
    finally:
        await runner.cleanup()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    if "--live" in sys.argv and all(OUTCOMES):
        live_check()
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
