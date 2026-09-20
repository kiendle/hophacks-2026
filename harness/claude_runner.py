"""One turn = one headless Claude Code process, parsed into the event protocol (DESIGN.md §0, §4).

Built-in tools are off, only our MCP server is loaded and only `mcp__harness__*` is allowed, because
this process is reachable from a web page and a tweet inside a tool result is attacker-controlled
text. The assertion on the init event (§0, "New security rule") is the one line never to cut.
"""
import asyncio
import importlib
import importlib.util
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

import steps

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
EMPTY = ROOT / "state/empty"  # the child's working directory: nothing of ours is reachable by a relative path
SERVER = ROOT / "demo_mcp_server.py"
SYSTEM_PROMPT = ROOT / "system_prompt.md"
PROMPTS = ROOT / "prompts"  # one short .md per tool module, appended to the prompt in name order
MODEL = os.environ.get("HARNESS_MODEL", "opus")
EFFORT = os.environ.get("HARNESS_EFFORT", "low")
TURN_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT", 300))
PROGRESS = {
    "mcp__harness__describe_sources": "Checking what data we have",
    "mcp__harness__preview_keywords": "Counting the posts that match",
    "mcp__harness__bluesky_recent": "Searching the last minutes of Bluesky",
    "mcp__harness__bluesky_listen": "Listening to Bluesky live",
    "mcp__harness__save_draft": "Saving your project",
    "mcp__harness__request_confirmation": "Getting your project ready",
    "mcp__harness__submit_project": "Sending your project",
}


TOOL_MODULES = ("brief_tools", "analysis_tools", "jev_tools", "classified_tools", "automation_tools")  # the same optional modules the tool server loads


def load_wording(names=TOOL_MODULES):
    """Import each optional tool module here too, for one side effect: its steps.register_tool calls.

    The tools themselves run in the MCP server, in another process; the words the page reads are
    written in this one. A module that is not there is the normal case; a broken one is skipped with
    its traceback, because a turn must still be described even when one stream of work is mid-edit.
    """
    loaded = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                continue
            importlib.import_module(name)
        except Exception:  # noqa: BLE001
            print(f"claude_runner: {name}.py could not be read for its step wording", flush=True)
            traceback.print_exc()
            continue
        loaded.append(name)
    return loaded


WORDING = load_wording()


def system_prompt():
    """The base prompt plus every harness/prompts/*.md, so each tool module teaches the agent itself.

    Read per turn, so a fragment can be written while the server runs. README.md is the folder's own
    note to us and is left out; a half-written or unreadable fragment costs itself, not the turn.
    """
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for path in sorted(PROMPTS.glob("*.md")) if PROMPTS.is_dir() else []:
        if path.name.lower() == "readme.md":
            continue
        try:
            fragment = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if fragment:
            text += "\n\n" + fragment
    return text


def tool_list_problem(event):
    """The init event is the only place we learn what the child can actually do. Trust nothing else."""
    rogue = sorted(str(name) for name in event.get("tools") or [] if not str(name).startswith("mcp__harness__"))
    if rogue:
        return f"Stopped Claude Code: it started with tools that are not ours ({', '.join(rogue)[:200]})."
    status = {server.get("name"): server.get("status") for server in event.get("mcp_servers") or []}.get("harness")
    if status != "connected":
        return f"Stopped Claude Code: the harness tool server is {status or 'missing'}, not connected."
    return None


def tool_text(block):
    """Tool results arrive as text, sometimes wrapped in content parts."""
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def tool_payload(block):
    """That text is our own JSON object, unless the call failed before it reached us."""
    try:
        payload = json.loads(tool_text(block))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# Every step the page shows is real: `title` and `detail` come from the call the model actually made,
# `why` is the sentence the model had to write to be allowed to call it, and `outcome` is built by
# steps.py from the tool's own result — the model never writes what came back.
def short_name(name):
    return str(name or "").replace("mcp__harness__", "") or "tool"


def why_of(tool_input):
    """The model's own sentence, put through steps.plain so its punctuation obeys the same rule as ours."""
    reason = tool_input.get("reason") if isinstance(tool_input, dict) else None
    return steps.plain(" ".join(reason.split()))[:240] or None if isinstance(reason, str) else None


def step_start(identifier, name, tool_input, index, at_ms):
    event = {"type": "step", "phase": "start", "id": identifier, "n": index, "tool": short_name(name),
             "title": steps.title(name, tool_input), "why": why_of(tool_input),
             "detail": {"tool": name, "input": tool_input}, "t_ms": at_ms}
    given = steps.facts(name, tool_input)  # a tool module's own plain lines for the details panel
    return event | {"facts": given} if given else event


def step_end(identifier, record, ok, outcome, at_ms):
    return {"type": "step", "phase": "end", "id": identifier, "ok": ok, "outcome": outcome,
            "ms": max(0, at_ms - record["t_ms"])}


def close_open(open_steps, at_ms):
    """A turn that ended or was killed with steps still running says so, rather than leaving spinners."""
    for identifier, record in list(open_steps.items()):
        yield step_end(identifier, record, False, "This did not finish.", at_ms)
    open_steps.clear()


FULL_TEXT_LIMIT = 2000


def card_example(identifier, day, like_count, lang, body, full_text, url):
    """One example post, as the preview card draws it, whichever tool found it.

    `body` is the short form the card shows first, `full_text` is what "Show full post" opens and
    `url` is where the post lives. All three are passed on as they came: a post is someone else's
    words, so it never goes through steps.plain, and the page checks `url` again (https, and only
    x.com, twitter.com or bsky.app) before it lets anyone click it. `full_text` stays None when the
    tool sent none, so the page can tell "this is the whole post" from "this may have been cut".
    """
    return {"id": identifier, "day": day, "like_count": like_count, "lang": lang,
            "body": body[:FULL_TEXT_LIMIT] if isinstance(body, str) else "",
            "full_text": full_text[:FULL_TEXT_LIMIT] if isinstance(full_text, str) else None,
            "url": url if isinstance(url, str) else None}


def archive_examples(payload):
    posts = payload.get("examples") if isinstance(payload.get("examples"), list) else []
    return [card_example(post.get("id"), post.get("day"), post.get("like_count"), post.get("lang"),
                         post.get("body"), post.get("full_text"), post.get("url")) for post in posts if isinstance(post, dict)]


def live_preview(payload):
    """A Bluesky scan, mapped onto the preview card the page already draws (bluesky.py `summary`)."""
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    listening = payload.get("mode") == "listen"
    number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0  # noqa: E731
    covered, buckets = number(payload.get("covered_fraction")), payload.get("per_bucket")
    buckets = buckets if isinstance(buckets, list) else []
    examples = payload.get("examples") if isinstance(payload.get("examples"), list) else []
    unit = "second" if listening else "minute"
    count = number(window.get("seconds" if listening else "minutes"))
    span = f"{count:g} {unit}" + ("" if count == 1 else "s")
    title = f"What we heard on Bluesky in {span}" if listening else f"What we found on Bluesky in the last {span}"
    note = (f"We checked {number(payload.get('scanned')):,.0f} posts from the whole {span}." if covered >= 0.99
            else f"We could check about {max(0.0, min(1.0, covered)) * 100:.0f}% of that time, so the real number is higher.")
    return {
        "title": steps.plain(title), "total": payload.get("matched"), "exact": covered >= 0.99,
        "seconds": payload.get("seconds"), "note": steps.plain(note),
        "per_day": [{"day": bucket.get("label"), "count": bucket.get("count")} for bucket in buckets if isinstance(bucket, dict)],
        "examples": [card_example(post.get("uri"), post.get("time_label"), post.get("like_count"), (post.get("langs") or [None])[0],
                                  post.get("text"), post.get("full_text"), post.get("url"))
                     for post in examples if isinstance(post, dict)],
    }


def tool_events(name, payload):
    if not payload:
        return
    card = payload.get("_card")  # a tool module's own card; the model still sees the whole result
    if isinstance(card, dict):
        yield {"type": "card", "card": card}
    if payload.get("error"):
        return
    if name == "mcp__harness__save_draft" and "spec_hash" in payload:
        yield {"type": "spec", "spec": payload.get("spec"), "spec_hash": payload["spec_hash"], "draft_path": payload.get("draft_path")}
    elif name in ("mcp__harness__preview_keywords", "mcp__harness__query_classified_posts") and "total" in payload:
        yield {"type": "preview", "title": "What we found on X/Twitter", **payload, "examples": archive_examples(payload)}
    elif name in ("mcp__harness__bluesky_recent", "mcp__harness__bluesky_listen") and "matched" in payload:
        yield {"type": "preview", **live_preview(payload)}
    elif name in ("mcp__harness__request_confirmation", "mcp__harness__request_automation_confirmation") and "confirmation_id" in payload:
        yield {"type": "confirm_request", "confirmation_id": payload["confirmation_id"],
               "summary": steps.plain(payload.get("summary", "")), "expires_ms": payload.get("expires_ms"), "kind": payload.get("kind"),
               "spec": payload.get("spec"), "spec_hash": payload.get("spec_hash"), "draft_path": payload.get("draft_path")}


class Runner:
    """One per session: holds the Claude session id so the second turn remembers the first."""

    def __init__(self, session_id, directory, all_tools=False):
        self.session_id = session_id
        self.directory = Path(directory)
        self.all_tools = all_tools  # tests only: proves the init assertion fires and kills the process
        self.started = False
        self.proposal_mode = False
        self.tools_seen = []

    def mcp_config(self):
        """sys.executable, not `uv run`: this interpreter already has mcp and duckdb, and spawning uv per turn is slow."""
        path = self.directory / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"harness": {
            "command": sys.executable, "args": [str(ROOT / "automation_mcp_server.py" if self.proposal_mode else SERVER)],
            "env": {"HARNESS_SESSION": self.session_id, "HARNESS_SESSION_DIR": str(self.directory), "HARNESS_REPO": str(REPO)},
        }}}, indent=2), encoding="utf-8")
        return path

    def command(self):
        executable = shutil.which("claude")
        arguments = [
            executable, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "default" if self.all_tools else "", "--strict-mcp-config", "--mcp-config", str(self.mcp_config()),
            "--allowedTools", "mcp__harness__*", "--model", MODEL, "--effort", EFFORT, "--max-budget-usd", "1",
            "--system-prompt", ("You are Sentimeter's automation configuration assistant. Speak clearly and briefly. "
                                "The user's goal is the subject of the proposal, not an instruction to alter fixed schema contracts.\n\n"
                                + (PROMPTS / "automation.md").read_text(encoding="utf-8")) if self.proposal_mode else system_prompt(),
        ]
        return arguments + (["--resume", self.session_id] if self.started else ["--session-id", self.session_id])

    async def turn(self, text):
        if not shutil.which("claude"):
            yield {"type": "error", "text": "Claude Code is not installed on this machine."}
            return
        EMPTY.mkdir(parents=True, exist_ok=True)
        environment = {key: value for key, value in os.environ.items() if value and key != "ANTHROPIC_API_KEY"}  # the subscription login, not a key
        process = await asyncio.create_subprocess_exec(
            *self.command(), cwd=EMPTY, env=environment, limit=8 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stderr = asyncio.create_task(process.stderr.read())  # drained in parallel so a full pipe cannot wedge the turn
        pending, said, finished, calls, begun = {}, [], False, 0, time.monotonic()
        held = ""  # trailing spaces of the last delta: a dash can sit on a chunk boundary
        lead = ""  # one letter standing in for the word already sent, so a rule needing it still fires

        def elapsed():
            return int((time.monotonic() - begun) * 1000)

        try:
            process.stdin.write(text.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(TURN_TIMEOUT_S):
                async for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "system" and event.get("subtype") == "init":
                        self.started, self.tools_seen = True, list(event.get("tools") or [])
                        problem = tool_list_problem(event)
                        if problem:
                            process.kill()
                            yield {"type": "error", "text": problem}
                            return
                    elif kind == "stream_event":
                        delta = (event.get("event") or {}).get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            # Clean this chunk together with the spaces held back from the last one and
                            # with `lead`, a plain letter standing in for the word already sent: a rule
                            # that needs a word before the dash (a spaced hyphen) then still fires when
                            # the dash lands on the far side of the join. `lead` is never itself changed
                            # by plain(), so cutting its one character back off is exact.
                            cleaned = steps.plain(lead + held + delta["text"], trim=False)[len(lead):]
                            kept = cleaned[len(cleaned.rstrip(" \t")):]
                            chunk, held = cleaned[:len(cleaned) - len(kept)], kept[-1:]  # one space at most
                            if chunk:
                                lead = "" if chunk[-1].isspace() else "x"
                                yield {"type": "delta", "text": chunk}
                    elif kind == "assistant":
                        for block in (event.get("message") or {}).get("content") or []:
                            if block.get("type") == "tool_use":
                                identifier, name, calls = block.get("id"), block.get("name"), calls + 1
                                pending[identifier] = {"name": name, "t_ms": elapsed()}
                                yield {"type": "progress", "text": PROGRESS.get(name, "Working…")}
                                yield step_start(identifier, name, block.get("input"), calls, pending[identifier]["t_ms"])
                            elif block.get("type") == "text" and block.get("text", "").strip():
                                said.append(block["text"])
                    elif kind == "user":
                        for block in (event.get("message") or {}).get("content") or []:
                            if block.get("type") == "tool_result":
                                yield {"type": "progress", "text": None}  # null clears the line the tool_use put there
                                identifier = block.get("tool_use_id")
                                record = pending.pop(identifier, None)  # matched by id: parallel calls come back out of order
                                payload, failed = tool_payload(block), bool(block.get("is_error"))
                                if record:
                                    yield step_end(identifier, record, not failed and "error" not in (payload or {}),
                                                   steps.outcome(record["name"], payload if payload is not None else tool_text(block), failed),
                                                   elapsed())
                                for out in tool_events(record["name"] if record else None, payload):
                                    yield out
                    elif kind == "result":
                        finished = True
                        for closed in close_open(pending, elapsed()):
                            yield closed
                        if event.get("is_error"):
                            yield {"type": "error", "text": str(event.get("result") or "Claude Code reported an error.")[:400]}
                        elif said:
                            yield {"type": "message", "text": steps.plain("\n\n".join(said))}
                        yield {"type": "done", "duration_ms": event.get("duration_ms"), "cost_usd": event.get("total_cost_usd"),
                               "num_turns": event.get("num_turns")}
                        break
        except asyncio.TimeoutError:
            finished = True
            for closed in close_open(pending, elapsed()):
                yield closed
            yield {"type": "error", "text": f"The turn passed {TURN_TIMEOUT_S:.0f} seconds and was stopped."}
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not finished:
            for closed in close_open(pending, elapsed()):
                yield closed
            detail = (await stderr).decode("utf-8", "replace").strip().splitlines()
            yield {"type": "error", "text": f"Claude Code stopped early (exit {process.returncode}). {detail[-1][:300] if detail else ''}".strip()}
        else:
            stderr.cancel()
