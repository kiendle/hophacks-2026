# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_bridge.py   (HARNESS_TEST_STEPS=4,5,6 skips the model turns)

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

import bridge  # noqa: E402
import claude_runner  # noqa: E402
import demo_mcp_server as tools  # noqa: E402

BASE = "http://127.0.0.1:5198"
STEPS = {step for step in (os.environ.get("HARNESS_TEST_STEPS") or "1,2,3,4,5,6").split(",")}
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

    saved = tools.save_draft(json.dumps(DRAFT))
    stable = tools.spec_hash(json.loads(json.dumps(DRAFT, sort_keys=True))) == saved["spec_hash"]
    asked = tools.request_confirmation()
    record = json.loads((directory / "confirmations" / f"{asked['confirmation_id']}.json").read_text())
    shape = set(record) == {"confirmation_id", "spec_hash", "created_ms", "expires_ms", "decision", "decided_ms", "consumed"}
    check("4a save_draft then request_confirmation", stable and shape and record["decision"] is None and not record["consumed"]
          and record["expires_ms"] - record["created_ms"] == 300_000 and len(record["confirmation_id"]) == 16,
          f"hash {saved['spec_hash'][:12]}…, id {asked['confirmation_id']}, summary {len(asked['summary'])} chars")
    check("4b submit before the button is refused", tools.submit_project().get("error", {}).get("code") == "not_confirmed",
          tools.submit_project().get("error", {}).get("message", "")[:80])

    events = await sse(client, f"/api/sessions/{session_id}/confirm", {"confirmation_id": asked["confirmation_id"], "approved": True})
    decided = json.loads((directory / "confirmations" / f"{asked['confirmation_id']}.json").read_text())
    check("4c the button writes the decision and starts a turn", decided["decision"] == "approved" and decided["decided_ms"]
          and kinds(events)[-1] == "done" and len(stub.said) == 1 and asked["confirmation_id"] in stub.said[0],
          f"harness wrote: {stub.said[0][:80]!r}")

    submitted = tools.submit_project()
    check("4d submit_project succeeds", submitted.get("status") == "submitted" and submitted.get("project_id")
          and (directory / "submitted.json").exists(), f"{submitted.get('project_id')}, note: {submitted.get('note', '')[:45]}…")
    check("4e submitting twice is refused", "already used" in tools.submit_project().get("error", {}).get("message", ""),
          tools.submit_project().get("error", {}).get("message", "")[:80])

    again = tools.request_confirmation()
    tools.save_draft(json.dumps(DRAFT | {"name": "changed my mind"}))
    gone = not (directory / "confirmations" / f"{again['confirmation_id']}.json").exists()
    async with client.post(BASE + f"/api/sessions/{session_id}/confirm", json={"confirmation_id": again["confirmation_id"], "approved": True}) as response:
        stale = response.status
    check("4f a new draft drops the pending confirmation", gone and stale == 404 and tools.submit_project().get("error", {}).get("code") == "not_confirmed",
          f"old button now HTTP {stale}")

    third = tools.request_confirmation()
    path = directory / "confirmations" / f"{third['confirmation_id']}.json"
    expired = json.loads(path.read_text()) | {"expires_ms": int(time.time() * 1000) - 1}
    path.write_text(json.dumps(expired))
    async with client.post(BASE + f"/api/sessions/{session_id}/confirm", json={"confirmation_id": third["confirmation_id"], "approved": True}) as response:
        check("4g an expired confirmation is 410", response.status == 410, (await response.json())["error"])

    def frames(name, payload):  # what the page will receive, from a real tool result wrapped as Claude Code wraps it
        block = {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": json.dumps(payload)}]}
        return list(claude_runner.tool_events(name, claude_runner.tool_payload(block)))
    preview = tools.preview_keywords(["ps6"], "2026-09-09", "2026-09-10", "en")
    spec_frame, preview_frame, confirm_frame = (frames(f"mcp__harness__{tool}", payload) for tool, payload in
                                                (("save_draft", saved), ("preview_keywords", preview), ("request_confirmation", third)))
    check("4h tool results become spec / preview / confirm_request frames",
          spec_frame[0]["type"] == "spec" and spec_frame[0]["spec_hash"] == saved["spec_hash"]
          and preview_frame[0]["type"] == "preview" and preview_frame[0]["per_day"] == preview["per_day"]
          and confirm_frame[0] == {"type": "confirm_request", "confirmation_id": third["confirmation_id"],
                                   "summary": third["summary"], "expires_ms": third["expires_ms"]}
          and not frames("mcp__harness__preview_keywords", {"error": {"code": "x"}}),
          f"spec_hash {spec_frame[0]['spec_hash'][:8]}…, {len(preview_frame[0]['per_day'])} day(s), errors emit nothing")


def preview_numbers():
    import duckdb
    started = time.monotonic()
    out = tools.preview_keywords(["anthropic"], "2026-09-09", "2026-09-10")
    connection = duckdb.connect()
    for setting in ("SET TimeZone='UTC'", "SET threads=4", "SET memory_limit='4GB'"):
        connection.execute(setting)
    files = tools.files_for_window(connection, "2026-09-09T00:00:00+00", "2026-09-10T00:00:00+00")
    every = connection.execute(  # the documented figure: all post types, so the timezone handling is provable
        "SELECT count(DISTINCT id) FROM read_parquet($files) WHERE created_at >= $lo::TIMESTAMPTZ AND created_at < $hi::TIMESTAMPTZ"
        " AND regexp_matches(body, '(?i)anthropic')",
        {"files": files, "lo": "2026-09-09T00:00:00+00", "hi": "2026-09-10T00:00:00+00"}).fetchone()[0]
    connection.close()
    check("5 preview_keywords is exact and in UTC", every == 5396 and out.get("exact") and out.get("total") == 484
          and out["per_day"] == [{"day": "2026-09-09", "count": 484}] and len(out["examples"]) == 6,
          f"all types {every} (4821 would mean a timezone bug), originals only {out.get('total')}, "
          f"{out.get('files_scanned')} of 396 files, {out.get('seconds')}s scan, {time.monotonic() - started:.1f}s total")
    check("5 the window guard holds", tools.preview_keywords(["anthropic"], "2026-09-01", "2026-09-06").get("error", {}).get("code") == "window_too_large"
          and tools.preview_keywords(["a"], "2026-09-09", "2026-09-10").get("error", {}).get("code") == "bad_keywords",
          "5 days refused, 1-character keyword refused")


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
        finally:
            await runner.cleanup()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
