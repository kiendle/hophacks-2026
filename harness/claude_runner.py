"""One turn = one headless Claude Code process, parsed into the event protocol (DESIGN.md §0, §4).

Built-in tools are off, only our MCP server is loaded and only `mcp__harness__*` is allowed, because
this process is reachable from a web page and a tweet inside a tool result is attacker-controlled
text. The assertion on the init event (§0, "New security rule") is the one line never to cut.
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
EMPTY = ROOT / "state/empty"  # the child's working directory: nothing of ours is reachable by a relative path
SERVER = ROOT / "demo_mcp_server.py"
SYSTEM_PROMPT = ROOT / "system_prompt.md"
MODEL = os.environ.get("HARNESS_MODEL", "opus")
EFFORT = os.environ.get("HARNESS_EFFORT", "low")
TURN_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT", 300))
PROGRESS = {
    "mcp__harness__describe_sources": "Checking which data is available…",
    "mcp__harness__preview_keywords": "Counting matching posts…",
    "mcp__harness__bluesky_recent": "Scanning the last minutes of Bluesky…",
    "mcp__harness__bluesky_listen": "Listening to Bluesky live…",
    "mcp__harness__save_draft": "Writing the draft project…",
    "mcp__harness__request_confirmation": "Preparing the confirmation…",
    "mcp__harness__submit_project": "Submitting the project…",
}


def tool_list_problem(event):
    """The init event is the only place we learn what the child can actually do. Trust nothing else."""
    rogue = sorted(str(name) for name in event.get("tools") or [] if not str(name).startswith("mcp__harness__"))
    if rogue:
        return f"Stopped Claude Code: it started with tools that are not ours ({', '.join(rogue)[:200]})."
    status = {server.get("name"): server.get("status") for server in event.get("mcp_servers") or []}.get("harness")
    if status != "connected":
        return f"Stopped Claude Code: the harness tool server is {status or 'missing'}, not connected."
    return None


def tool_payload(block):
    """Tool results arrive as JSON text, sometimes wrapped in content parts. Anything else is ignored."""
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        payload = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def live_preview(payload):
    """A Bluesky scan, mapped onto the preview card the page already draws (bluesky.py `summary`)."""
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    listening = payload.get("mode") == "listen"
    number = lambda value: value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0  # noqa: E731
    covered, buckets = number(payload.get("covered_fraction")), payload.get("per_bucket")
    buckets = buckets if isinstance(buckets, list) else []
    examples = payload.get("examples") if isinstance(payload.get("examples"), list) else []
    span = f"listening {number(window.get('seconds')):g} s" if listening else f"last {number(window.get('minutes')):g} min"
    return {
        "title": f"Live Bluesky — {span}", "total": payload.get("matched"), "exact": covered >= 0.99,
        "seconds": payload.get("seconds"), "note": f"scanned {number(payload.get('scanned')):,.0f} posts, covered {covered * 100:.0f}% of the window",
        "per_day": [{"day": bucket.get("label"), "count": bucket.get("count")} for bucket in buckets if isinstance(bucket, dict)],
        "examples": [{"id": post.get("uri"), "day": post.get("time_label"), "like_count": post.get("like_count"),
                      "lang": (post.get("langs") or [None])[0], "body": post.get("text"), "url": post.get("url")}
                     for post in examples if isinstance(post, dict)],
    }


def tool_events(name, payload):
    if not payload or payload.get("error"):
        return
    if name == "mcp__harness__save_draft" and "spec_hash" in payload:
        yield {"type": "spec", "spec": payload.get("spec"), "spec_hash": payload["spec_hash"]}
    elif name == "mcp__harness__preview_keywords" and "total" in payload:
        yield {"type": "preview", "title": "Preview — X/Twitter archive", **payload}
    elif name in ("mcp__harness__bluesky_recent", "mcp__harness__bluesky_listen") and "matched" in payload:
        yield {"type": "preview", **live_preview(payload)}
    elif name == "mcp__harness__request_confirmation" and "confirmation_id" in payload:
        yield {"type": "confirm_request", "confirmation_id": payload["confirmation_id"],
               "summary": payload.get("summary", ""), "expires_ms": payload.get("expires_ms")}


class Runner:
    """One per session: holds the Claude session id so the second turn remembers the first."""

    def __init__(self, session_id, directory, all_tools=False):
        self.session_id = session_id
        self.directory = Path(directory)
        self.all_tools = all_tools  # tests only: proves the init assertion fires and kills the process
        self.started = False
        self.tools_seen = []

    def mcp_config(self):
        """sys.executable, not `uv run`: this interpreter already has mcp and duckdb, and spawning uv per turn is slow."""
        path = self.directory / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"harness": {
            "command": sys.executable, "args": [str(SERVER)],
            "env": {"HARNESS_SESSION": self.session_id, "HARNESS_SESSION_DIR": str(self.directory), "HARNESS_REPO": str(REPO)},
        }}}, indent=2), encoding="utf-8")
        return path

    def command(self):
        executable = shutil.which("claude")
        arguments = [
            executable, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "default" if self.all_tools else "", "--strict-mcp-config", "--mcp-config", str(self.mcp_config()),
            "--allowedTools", "mcp__harness__*", "--model", MODEL, "--effort", EFFORT, "--max-budget-usd", "1",
            "--system-prompt", SYSTEM_PROMPT.read_text(encoding="utf-8"),
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
        pending, said, finished = {}, [], False
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
                            yield {"type": "delta", "text": delta["text"]}
                    elif kind == "assistant":
                        for block in (event.get("message") or {}).get("content") or []:
                            if block.get("type") == "tool_use":
                                pending[block.get("id")] = block.get("name")
                                yield {"type": "progress", "text": PROGRESS.get(block.get("name"), "Working…")}
                            elif block.get("type") == "text" and block.get("text", "").strip():
                                said.append(block["text"])
                    elif kind == "user":
                        for block in (event.get("message") or {}).get("content") or []:
                            if block.get("type") == "tool_result":
                                yield {"type": "progress", "text": None}  # null clears the line the tool_use put there
                                for out in tool_events(pending.pop(block.get("tool_use_id"), None), tool_payload(block)):
                                    yield out
                    elif kind == "result":
                        finished = True
                        if event.get("is_error"):
                            yield {"type": "error", "text": str(event.get("result") or "Claude Code reported an error.")[:400]}
                        elif said:
                            yield {"type": "message", "text": "\n\n".join(said)}
                        yield {"type": "done", "duration_ms": event.get("duration_ms"), "cost_usd": event.get("total_cost_usd"),
                               "num_turns": event.get("num_turns")}
                        break
        except asyncio.TimeoutError:
            finished = True
            yield {"type": "error", "text": f"The turn passed {TURN_TIMEOUT_S:.0f} seconds and was stopped."}
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not finished:
            detail = (await stderr).decode("utf-8", "replace").strip().splitlines()
            yield {"type": "error", "text": f"Claude Code stopped early (exit {process.returncode}). {detail[-1][:300] if detail else ''}".strip()}
        else:
            stderr.cancel()
