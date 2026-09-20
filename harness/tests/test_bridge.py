# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_bridge.py   (HARNESS_TEST_STEPS=4,5,6,7 skips the model turns)

Drives the real bridge over real HTTP on port 5198, with real local Claude Code turns in steps 1-3.
Three turns are spent on the subscription; the confirmation state machine is tested without the model
so its outcome is deterministic (the endpoint's streaming is checked with a stub runner).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HARNESS_REPO", str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import bridge  # noqa: E402
import claude_runner  # noqa: E402
import demo_mcp_server as tools  # noqa: E402

BASE = "http://127.0.0.1:5198"
STEPS = {step for step in (os.environ.get("HARNESS_TEST_STEPS") or "1,2,3,4,5,6,7").split(",")}
OUTCOMES = []


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the Windows console is cp1252


def kinds(events):
    return [event["type"] for event in events]


def said(events):
    return " ".join(event["text"] for event in events if event["type"] == "message")


async def sse(client, path, payload):
    """Every response on /messages and /confirm is a stream of `data: <json>` frames."""
    started, events = time.monotonic(), []
    async with client.post(BASE + path, json=payload) as response:
        if response.status != 200:
            return [{"type": "http_error", "status": response.status, "text": await response.text()}]
        async for raw in response.content:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]) | {"_at": round(time.monotonic() - started, 1)})
    return events


def timing(events, kind):
    return next((event["_at"] for event in events if event["type"] == kind), None)


async def new_session(client):
    async with client.post(BASE + "/api/sessions", json={}) as response:
        return (await response.json())["session_id"]


async def conversation(client):
    session_id = await new_session(client)
    events = await sse(client, f"/api/sessions/{session_id}/messages", {"text": "What data can I observe here?"})
    first, progress, tail = timing(events, "delta"), [e for e in events if e["type"] == "progress"], kinds(events)[-1]
    check("1 first turn streams", kinds(events).count("delta") >= 1 and progress and "message" in kinds(events) and tail == "done",
          f"{kinds(events).count('delta')} deltas, first at {first}s, {len(progress)} progress, done at {timing(events, 'done')}s, "
          f"tool call at {progress[0]['_at'] if progress else None}s")
    check("1 progress line names the tool", any(e["text"] and "data" in e["text"].lower() for e in progress),
          repr(next((e["text"] for e in progress if e["text"]), None)))
    starts = [event for event in events if event["type"] == "step" and event["phase"] == "start"]
    ends = {event["id"]: event for event in events if event["type"] == "step" and event["phase"] == "end"}
    check("1 the real turn shows real steps, each with the model's own why", bool(starts) and len(ends) == len(starts)
          and all(isinstance(s["why"], str) and len(s["why"].split()) >= 3 and s["id"] in ends and ends[s["id"]]["outcome"] for s in starts)
          and [s["n"] for s in starts] == list(range(1, len(starts) + 1)),
          " | ".join(f"{s['n']}. {s['title']} / why: {s['why']} / {ends.get(s['id'], {}).get('outcome')}"
                     f" ({ends.get(s['id'], {}).get('ms')} ms)" for s in starts)[:600])
    if "2" in STEPS:
        follow = await sse(client, f"/api/sessions/{session_id}/messages", {"text": "Which one is larger?"})
        answer = said(follow).lower()
        check("2 second turn keeps the context", any(word in answer for word in ("firehose", "twitter", " x ", "377")),
              f"{timing(follow, 'done')}s, answered: {said(follow)[:110]!r}")
    return session_id


async def assertion_fires(client):
    """The one control that can never be cut: built-in tools mean the process dies at the init event."""
    session_id = await new_session(client)
    directory = bridge.SESSIONS / session_id
    runner = claude_runner.Runner(session_id, directory, all_tools=True)
    events = [event async for event in runner.turn("List the files in the current directory.")]
    rogue = [name for name in runner.tools_seen if not name.startswith("mcp__harness__")]
    check("3 --tools default is refused", kinds(events) == ["error"] and "not ours" in events[0]["text"],
          f"{kinds(events)} {events[0].get('text', '')[:90]!r}")
    check("3 killed before any tool ran", not any(k in kinds(events) for k in ("progress", "delta", "message", "done")) and not (directory / "draft.json").exists(),
          f"built-ins offered: {len(rogue)} ({', '.join(rogue[:4])}…)")


DRAFT = {
    "name": "PlayStation cancellation reaction", "keywords": ["playstation", "ps6"], "language": "en",
    "observation": {"intent": "How did people react to the PlayStation cancellation?", "source": "twitter_firehose",
                    "window": {"from": "2026-09-09", "to": "2026-09-12"}},
    "categories": [{"name": "angry", "description": "Blames Sony"}, {"name": "unclear", "description": "Cannot tell"}],
    "sentiment_question": "How negative is the author about Sony?",
}


class StubRunner:
    """/confirm must stream exactly like /messages; what the model then says is already covered by steps 1-2."""

    def __init__(self):
        self.said = []

    async def turn(self, text):
        self.said.append(text)
        yield {"type": "message", "text": "ok"}
        yield {"type": "done", "duration_ms": 0}


async def confirmation_gate(client):
    session_id = await new_session(client)
    directory = bridge.SESSIONS / session_id
    os.environ["HARNESS_SESSION_DIR"] = str(directory)
    stub = StubRunner()
    bridge.app["state"]["sessions"][session_id]["runner"] = stub

    saved = tools.save_draft("to write down what we agreed so far", json.dumps(DRAFT))
    stable = tools.spec_hash(json.loads(json.dumps(DRAFT, sort_keys=True))) == saved["spec_hash"]
    asked = tools.request_confirmation("so you can check it before anything runs")
    record = json.loads((directory / "confirmations" / f"{asked['confirmation_id']}.json").read_text())
    shape = set(record) == {"confirmation_id", "spec_hash", "created_ms", "expires_ms", "decision", "decided_ms", "consumed"}
    check("4a save_draft then request_confirmation", stable and shape and record["decision"] is None and not record["consumed"]
          and record["expires_ms"] - record["created_ms"] == 300_000 and len(record["confirmation_id"]) == 16,
          f"hash {saved['spec_hash'][:12]}…, id {asked['confirmation_id']}, summary {len(asked['summary'])} chars")
    wanted = "\n".join(["Name: PlayStation cancellation reaction",
                        "What we are watching: How did people react to the PlayStation cancellation?",
                        "Where: the X/Twitter archive",
                        "When: Sep 9 to Sep 11, 2026",
                        "Language: English",
                        'Words we search for: "playstation", "ps6"',
                        "Groups we sort posts into:", "angry: Blames Sony", "unclear: Cannot tell",
                        "Feeling question: How negative is the author about Sony?"])
    check("4a the confirm card reads as plain labelled lines, with no jargon and no symbols",
          asked["summary"] == wanted, repr(asked["summary"])[:300])
    check("4a nothing saved reads as one plain sentence", tools.render_summary({}) == "There is nothing saved to confirm yet."
          and tools.render_summary(None) == "There is nothing saved to confirm yet.", repr(tools.render_summary({})))
    check("4b submit before the button is refused", tools.submit_project("to start it now that you approved it").get("error", {}).get("code") == "not_confirmed",
          tools.submit_project("to start it now that you approved it").get("error", {}).get("message", "")[:80])

    events = await sse(client, f"/api/sessions/{session_id}/confirm", {"confirmation_id": asked["confirmation_id"], "approved": True})
    decided = json.loads((directory / "confirmations" / f"{asked['confirmation_id']}.json").read_text())
    check("4c the button writes the decision and starts a turn", decided["decision"] == "approved" and decided["decided_ms"]
          and kinds(events)[-1] == "done" and len(stub.said) == 1 and asked["confirmation_id"] in stub.said[0],
          f"harness wrote: {stub.said[0][:80]!r}")

    submitted = tools.submit_project("to start it now that you approved it")
    check("4d submit_project succeeds", submitted.get("status") == "submitted" and submitted.get("project_id")
          and (directory / "submitted.json").exists(), f"{submitted.get('project_id')}, note: {submitted.get('note', '')[:45]}…")
    check("4e submitting twice is refused", "already used" in tools.submit_project("to start it now that you approved it").get("error", {}).get("message", ""),
          tools.submit_project("to start it now that you approved it").get("error", {}).get("message", "")[:80])

    again = tools.request_confirmation("so you can check it before anything runs")
    tools.save_draft("to change the name you asked me to change", json.dumps(DRAFT | {"name": "changed my mind"}))
    gone = not (directory / "confirmations" / f"{again['confirmation_id']}.json").exists()
    async with client.post(BASE + f"/api/sessions/{session_id}/confirm", json={"confirmation_id": again["confirmation_id"], "approved": True}) as response:
        stale = response.status
    check("4f a new draft drops the pending confirmation", gone and stale == 404 and tools.submit_project("to start it now that you approved it").get("error", {}).get("code") == "not_confirmed",
          f"old button now HTTP {stale}")

    third = tools.request_confirmation("so you can check it before anything runs")
    path = directory / "confirmations" / f"{third['confirmation_id']}.json"
    expired = json.loads(path.read_text()) | {"expires_ms": int(time.time() * 1000) - 1}
    path.write_text(json.dumps(expired))
    async with client.post(BASE + f"/api/sessions/{session_id}/confirm", json={"confirmation_id": third["confirmation_id"], "approved": True}) as response:
        check("4g an expired confirmation is 410", response.status == 410, (await response.json())["error"])

    def frames(name, payload):  # what the page will receive, from a real tool result wrapped as Claude Code wraps it
        block = {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": json.dumps(payload)}]}
        return list(claude_runner.tool_events(name, claude_runner.tool_payload(block)))
    preview = tools.preview_keywords("to check that word catches the right conversation", ["ps6"], "2026-09-09", "2026-09-10", "en")
    spec_frame, preview_frame, confirm_frame = (frames(f"mcp__harness__{tool}", payload) for tool, payload in
                                                (("save_draft", saved), ("preview_keywords", preview), ("request_confirmation", third)))
    check("4h tool results become spec / preview / confirm_request frames",
          spec_frame[0]["type"] == "spec" and spec_frame[0]["spec_hash"] == saved["spec_hash"]
          and preview_frame[0]["type"] == "preview" and preview_frame[0]["per_day"] == preview["per_day"]
          and confirm_frame[0] == {"type": "confirm_request", "confirmation_id": third["confirmation_id"],
                                   "summary": third["summary"], "expires_ms": third["expires_ms"],
                                   "spec": third["spec"], "spec_hash": third["spec_hash"], "draft_path": third["draft_path"]}
          and not frames("mcp__harness__preview_keywords", {"error": {"code": "x"}}),
          f"spec_hash {spec_frame[0]['spec_hash'][:8]}…, {len(preview_frame[0]['per_day'])} day(s), errors emit nothing")
    check("4i the developer view can still see the file and the version", spec_frame[0]["draft_path"] == saved["draft_path"]
          and confirm_frame[0]["draft_path"].endswith("/draft.json") and confirm_frame[0]["spec"]["name"] == "changed my mind"
          and confirm_frame[0]["spec_hash"] == tools.spec_hash(confirm_frame[0]["spec"]),
          f"draft_path {confirm_frame[0]['draft_path']}")
    check("4j a tool call without a reason does nothing", tools.describe_sources().get("error", {}).get("code") == "no_reason"
          and tools.preview_keywords("   ", ["ps6"], "2026-09-09", "2026-09-10").get("error", {}).get("code") == "no_reason"
          and tools.submit_project(None).get("error", {}).get("code") == "no_reason"
          and len(tools.describe_sources("x" * 500).get("sources", [])) == 3,
          f"hint: {tools.describe_sources().get('error', {}).get('hint', '')[:70]}…, an over-long reason is truncated, not refused")


def preview_numbers():
    import duckdb
    started = time.monotonic()
    out = tools.preview_keywords("to count how much was posted that day", ["anthropic"], "2026-09-09", "2026-09-10")
    connection = duckdb.connect()
    for setting in ("SET TimeZone='UTC'", "SET threads=4", "SET memory_limit='4GB'"):
        connection.execute(setting)
    files = tools.files_for_window(connection, "2026-09-09T00:00:00+00", "2026-09-10T00:00:00+00")
    every = connection.execute(  # the documented figure: all post types, so the timezone handling is provable
        "SELECT count(DISTINCT id) FROM read_parquet($files) WHERE created_at >= $lo::TIMESTAMPTZ AND created_at < $hi::TIMESTAMPTZ"
        " AND regexp_matches(body, '(?i)anthropic')",
        {"files": files, "lo": "2026-09-09T00:00:00+00", "hi": "2026-09-10T00:00:00+00"}).fetchone()[0]
    connection.close()
    check("5 preview_keywords counts exactly, over the whole day", every == 5396 and out.get("exact") and out.get("total") == 484
          and out["per_day"] == [{"day": "2026-09-09", "count": 484}] and len(out["examples"]) == 6,
          f"all post types {every} (4821 would mean a timezone bug), posts people wrote themselves {out.get('total')}, "
          f"{out.get('files_scanned')} of 396 files, {out.get('seconds')}s scan, {time.monotonic() - started:.1f}s total")
    check("5 the window guard holds", tools.preview_keywords("to count how much was posted that week", ["anthropic"], "2026-09-01", "2026-09-06").get("error", {}).get("code") == "window_too_large"
          and tools.preview_keywords("to try one short word", ["a"], "2026-09-09", "2026-09-10").get("error", {}).get("code") == "bad_keywords",
          "5 days refused, 1-character keyword refused")

    # A person can read an example in full and open it where it lives: the short body the card shows
    # first, the full text (at most 2,000 characters) and an address built from the post's own id.
    import re
    posts = out.get("examples") or []
    sizes = duckdb.connect()  # DuckDB cut the texts, so DuckDB measures them: left() counts what a person sees as one character
    lengths = [sizes.execute("SELECT length_grapheme(?), length_grapheme(?)", [post.get("body") or "", post.get("full_text") or ""]).fetchone() for post in posts]
    cuts = sizes.execute(f"SELECT length(left(repeat('x', 5000), {tools.BODY_LIMIT})), length(left(repeat('x', 5000), {tools.FULL_TEXT_LIMIT}))").fetchone()
    sizes.close()
    check("5a every example carries its short text, its full text and its address",
          len(posts) == 6 and cuts == (240, 2000)
          and all(isinstance(post.get("body"), str) and isinstance(post.get("full_text"), str) and post["body"] and post["full_text"].startswith(post["body"]) for post in posts)
          and all(short <= 240 and short <= whole <= 2000 for short, whole in lengths)
          and all(re.fullmatch(r"https://x\.com/i/web/status/[0-9]{1,25}", post.get("url") or "") and post["url"].rsplit("/", 1)[1] == str(post["id"]) for post in posts),
          f"short and full lengths {lengths}, {sum(whole > short for short, whole in lengths)} of 6 have more to show, first address {posts[0].get('url') if posts else None}")
    numbers = ("1965432100000000001", 1965432100000000001, "7", "0" * 25)
    refused = ("", "12a", "a12", "12/../x", "12/", "1?x=1", "1#x", " 12", "12 ", "12\n", "-1", "+1", "1.5", "1e5", "１２３", "١٢٣", "1" * 26,
               "javascript:alert(1)", "https://evil.example/1", None, True, False, 1.0, ["1"], {"id": 1}, b"12")
    check("5b only an id that is plain digits becomes an address",
          [tools.post_url(value) for value in numbers] == [f"https://x.com/i/web/status/{value}" for value in numbers]
          and not [value for value in refused if tools.post_url(value) is not None]
          and tools.example_post("12/../x", "2026-09-09", 3, "en", "short", "short and the rest")
          == {"id": "12/../x", "day": "2026-09-09", "like_count": 3, "lang": "en", "body": "short", "full_text": "short and the rest", "url": None},
          f"{len(numbers)} numbers accepted, {len(refused)} other shapes refused, among them full-width and Arabic digits, a path, a query and a trailing newline")
    live = {"mode": "recent", "window": {"minutes": 15}, "matched": 1, "scanned": 10, "covered_fraction": 1.0, "per_bucket": [], "examples": [
        {"uri": "at://did:plc:abc/app.bsky.feed.post/3k", "url": "https://bsky.app/profile/did:plc:abc/post/3k", "time_label": "19:05",
         "like_count": 2, "langs": ["en"], "text": "cut off he", "full_text": "cut off here no more — and; kept -> as written"}, "not a post"]}
    archive_frame = list(claude_runner.tool_events("mcp__harness__preview_keywords", out))[0]
    live_frame = list(claude_runner.tool_events("mcp__harness__bluesky_recent", live))[0]
    old_frame = list(claude_runner.tool_events("mcp__harness__preview_keywords", {"total": 1, "per_day": [], "examples": [{"id": "9", "day": "2026-09-09", "like_count": 1, "lang": "en", "body": "from an older tool", "url": 7}]}))[0]
    check("5c the preview event passes the full text and the address on to the page, untouched",
          [(post["body"], post["full_text"], post["url"]) for post in archive_frame["examples"]] == [(post["body"], post["full_text"], post["url"]) for post in posts]
          and archive_frame["total"] == out["total"] and archive_frame["title"] == "What we found on X/Twitter"
          and live_frame["examples"] == [{"id": "at://did:plc:abc/app.bsky.feed.post/3k", "day": "19:05", "like_count": 2, "lang": "en", "body": "cut off he",
                                         "full_text": "cut off here no more — and; kept -> as written", "url": "https://bsky.app/profile/did:plc:abc/post/3k"}]
          and old_frame["examples"] == [{"id": "9", "day": "2026-09-09", "like_count": 1, "lang": "en", "body": "from an older tool", "full_text": None, "url": None}],
          "archive and live examples alike, a post is never reworded, and a tool that sent no full text says so with None")

    # A real test found "PS6" matching 87 posts about nothing, because "ps6" sits inside t.co links
    # such as https://t.co/MCqPS6NfCl. Links are taken out before matching, and a short or capitalised
    # word has to be a whole word.
    posts = duckdb.connect()
    posts.execute("CREATE TABLE posts(body VARCHAR)")
    posts.executemany("INSERT INTO posts VALUES (?)", [("look at this https://t.co/MCqPS6NfCl now",), ("the PS6 was cancelled",),
                                                       ("he said nothing at all",), ("AI is everywhere",),
                                                       ("PlayStations everywhere",), ("read https://anthropic.com/news today",)])

    def hits(word):
        return [row[0] for row in posts.execute(f"SELECT body FROM posts WHERE regexp_matches({tools.LINKLESS}, $k)",
                                                {"k": tools.search_pattern(word)}).fetchall()]
    inside_link, whole_words = hits("PS6"), hits("AI")
    check("5 a word hiding inside a link is never a match", inside_link == ["the PS6 was cancelled"] and hits("anthropic") == [],
          f"PS6 matched {inside_link}, and the link-only anthropic post was skipped")
    check("5 a short or capitalised word matches only whole words", whole_words == ["AI is everywhere"]
          and hits("playstation") == ["PlayStations everywhere"],
          f"AI matched {whole_words} and not \"said\", while a long word still matches inside another")
    posts.close()


class FakePipe:
    """Recorded stream-json lines, fed through the real turn loop: no model, no subprocess, no cost."""

    def __init__(self, lines=(), data=b""):
        self.lines, self.data = list(lines), data

    def write(self, _payload):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def read(self):
        return self.data

    async def __aiter__(self):
        for line in self.lines:
            yield line


class FakeProcess:
    def __init__(self, lines):
        self.stdin, self.stdout, self.stderr = FakePipe(), FakePipe(lines), FakePipe()
        self.returncode = None

    def kill(self):
        self.returncode = self.returncode if self.returncode is not None else -9

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0


async def replay(lines):
    directory = bridge.SESSIONS / "replay"
    (directory / "confirmations").mkdir(parents=True, exist_ok=True)
    runner = claude_runner.Runner("00000000-0000-4000-8000-00000000beef", directory)
    original = claude_runner.asyncio.create_subprocess_exec

    async def fake(*_arguments, **_keywords):
        return FakeProcess(json.dumps(line).encode("utf-8") + b"\n" for line in lines)

    claude_runner.asyncio.create_subprocess_exec = fake
    try:
        return [event async for event in runner.turn("replayed")]
    finally:
        claude_runner.asyncio.create_subprocess_exec = original


INIT = {"type": "system", "subtype": "init", "tools": ["mcp__harness__preview_keywords", "mcp__harness__bluesky_recent"],
        "mcp_servers": [{"name": "harness", "status": "connected"}]}
CALLS = [{"type": "tool_use", "id": "toolu_A", "name": "mcp__harness__preview_keywords",
          "input": {"reason": "to check those two words catch the conversation you mean", "keywords": ["playstation", "ps6"],
                    "date_from": "2026-09-09", "date_to": "2026-09-12", "language": "en"}},
         {"type": "tool_use", "id": "toolu_B", "name": "mcp__harness__bluesky_recent",
          "input": {"reason": "to see whether people are still posting about it right now", "keywords": ["ps6"], "minutes": 15}}]
ARCHIVE = {"total": 2144, "exact": True, "seconds": 8.4, "per_day": [{"day": "2026-09-09", "count": 788},
           {"day": "2026-09-10", "count": 701}, {"day": "2026-09-11", "count": 655}], "examples": []}
LIVE = {"mode": "recent", "window": {"minutes": 15}, "matched": 41, "scanned": 131_000, "covered_fraction": 1.0,
        "seconds": 12.7, "per_bucket": [], "examples": []}


def returned(identifier, payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": identifier,
                                                     "is_error": is_error, "content": [{"type": "text", "text": text}]}]}}


async def step_stream():
    """The page's whole story of a turn, proved without the model: one row per real call, and its real result."""
    events = await replay([
        INIT,
        {"type": "assistant", "message": {"content": CALLS}},
        returned("toolu_B", LIVE),  # the live scan finishes first: results must match their call by id
        returned("toolu_A", ARCHIVE),
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_C", "name": "mcp__harness__save_draft",
                                                       "input": {"reason": "to write down what we agreed", "spec_json": "{"}}]}},
        returned("toolu_C", {"error": {"code": "bad_json", "message": "Your project could not be written down.",
                                       "hint": "The draft was not valid JSON: Expecting property name."}}),
        {"type": "result", "is_error": False, "duration_ms": 21_300, "num_turns": 3},
    ])
    starts = [event for event in events if event.get("type") == "step" and event["phase"] == "start"]
    ends = [event for event in events if event.get("type") == "step" and event["phase"] == "end"]
    first, second, third = (starts + [{}, {}, {}])[:3]
    check("7a a tool call becomes a step with what, why and the raw call",
          [s.get("n") for s in starts] == [1, 2, 3] and [s.get("id") for s in starts] == ["toolu_A", "toolu_B", "toolu_C"]
          and first.get("tool") == "preview_keywords" and first.get("why") == CALLS[0]["input"]["reason"]
          and first.get("title") == 'Searching X/Twitter for "playstation", "ps6", from Sep 9 to Sep 11, in English'
          and second.get("title") == 'Searching the last 15 minutes of Bluesky for "ps6"'
          and third.get("title") == "Saving your project" and first["detail"] == {"tool": CALLS[0]["name"], "input": CALLS[0]["input"]}
          and all(isinstance(s.get("t_ms"), int) and s["t_ms"] >= 0 for s in starts),
          f"1. {first.get('title')} / why: {first.get('why')}")
    check("7b results are matched to their call by id, not by order",
          [e["id"] for e in ends] == ["toolu_B", "toolu_A", "toolu_C"] and all(isinstance(e["ms"], int) and e["ms"] >= 0 for e in ends)
          and [e["ok"] for e in ends] == [True, True, False],
          f"ends arrived as {[e['id'] for e in ends]}")
    check("7c the outcome is written from the tool's own result",
          ends[0]["outcome"] == "Found 41 posts out of 131,000 checked."
          and ends[1]["outcome"] == "Found 2,144 posts. Most were on Sep 9 (788)."
          and ends[2]["outcome"] == "That did not work: Your project could not be written down.",
          " | ".join(e["outcome"] for e in ends))
    check("7d the old events are untouched", kinds(events)[-1] == "done" and kinds(events).count("progress") == 6
          and [e["type"] for e in events if e["type"] == "preview"] == ["preview", "preview"],
          f"{kinds(events).count('progress')} progress lines, 2 preview cards, done last")

    stopped = await replay([INIT, {"type": "assistant", "message": {"content": [CALLS[1]]}}])  # the process dies mid-call
    last = [event for event in stopped if event.get("type") == "step" and event["phase"] == "end"]
    check("7e a step still running when the turn dies is closed, not left spinning",
          len(last) == 1 and last[0]["id"] == "toolu_B" and last[0]["ok"] is False and last[0]["outcome"] == "This did not finish."
          and kinds(stopped)[-1] == "error",
          f"{last[0]['outcome'] if last else None}, then {kinds(stopped)[-1]}")

    listed = await tools.mcp.list_tools()
    required = {tool.name: tool.input_schema.get("required", []) for tool in listed}
    check("7f every tool requires a reason", len(required) == 7 and all(names and names[0] == "reason" for names in required.values()),
          ", ".join(f"{name}({len(names)})" for name, names in required.items()))


HOSTILE = "Found 1,522 posts — most on Sep 10. See A -> B; done."


async def plain_words():
    """Every word the page shows goes through steps.plain: the model's why, its answer, and the summary."""
    events = await replay([
        INIT,
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_D", "name": "mcp__harness__bluesky_recent",
             "input": {"reason": "Narrowing the words — one of them was pulling in film posts; trying again.", "keywords": ["ps6"]}}]}},
        returned("toolu_D", LIVE),
        # a dash split across two deltas: the space before it arrives in the first chunk, the dash in the second
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Found 1,522 posts "}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "— most on Sep 10"}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": ". See A -> B; done."}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": HOSTILE}]}},
        {"type": "result", "is_error": False, "duration_ms": 900, "num_turns": 2},
    ])
    clean = "Found 1,522 posts, most on Sep 10. See A to B. Done."
    why = next((event["why"] for event in events if event.get("type") == "step" and event["phase"] == "start"), "")
    streamed = "".join(event["text"] for event in events if event.get("type") == "delta")
    final = next((event["text"] for event in events if event.get("type") == "message"), "")
    check("7g the model's own why is cleaned before the page sees it",
          why == "Narrowing the words, one of them was pulling in film posts. Trying again.", repr(why))
    check("7h a dash split across two deltas leaves no stray comma", streamed == clean, repr(streamed))
    check("7i the final answer is cleaned as a whole", final == clean, repr(final))
    banned = [piece for piece in (why, streamed, final, tools.render_summary(DRAFT))
              if any(character in "–—―·•→;" for character in piece) or "->" in piece]
    check("7j nothing the page shows carries a banned character", not banned, repr(banned[:1])[:200] or "why, answer and summary are clean")

    # A plain hyphen is what a model reaches for once it is told not to type an em dash, and it can
    # land on the far side of a chunk boundary with whitespace-only chunks in between. The rule needs
    # a word before the dash, which by then has already been sent, so the runner stands one in.
    split = await replay([
        INIT,
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Found 41 posts "}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "   "}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "  "}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "- most on Sep 10."}}},
        {"type": "result", "is_error": False, "duration_ms": 300, "num_turns": 1},
    ])
    pieces = [event["text"] for event in split if event.get("type") == "delta"]
    check("7k a hyphen used as a dash across a chunk boundary is cleaned, and a blank chunk is never sent",
          "".join(pieces) == "Found 41 posts, most on Sep 10." and all(piece.strip() for piece in pieces),
          f"{len(pieces)} deltas: {pieces}")


async def separate_sessions(client):
    first, second = await new_session(client), await new_session(client)
    directories = [bridge.SESSIONS / first, bridge.SESSIONS / second]
    configs = [json.loads((directory / "mcp.json").read_text())["mcpServers"]["harness"]["env"] for directory in directories]
    check("6 two sessions are isolated", first != second and directories[0] != directories[1] and all(d.is_dir() for d in directories)
          and configs[0]["HARNESS_SESSION_DIR"] != configs[1]["HARNESS_SESSION_DIR"] and configs[0]["HARNESS_SESSION"] == first,
          f"…{first[-8:]} and …{second[-8:]}")


async def main():
    runner = web.AppRunner(bridge.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5198)
    await site.start()
    async with aiohttp.ClientSession(headers={"Origin": BASE}, timeout=aiohttp.ClientTimeout(total=360)) as client:
        try:
            if "1" in STEPS:
                await conversation(client)
            if "3" in STEPS:
                await assertion_fires(client)
            if "4" in STEPS:
                await confirmation_gate(client)
            if "5" in STEPS:
                await asyncio.to_thread(preview_numbers)
            if "6" in STEPS:
                await separate_sessions(client)
            if "7" in STEPS:
                await step_stream()
                await plain_words()
        finally:
            await runner.cleanup()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
