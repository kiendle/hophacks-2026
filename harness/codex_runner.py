"""Codex app-server adapter for the existing harness SSE and MCP contracts.

Uses the existing Codex login. Each conversation owns its app-server and MCP
processes; they stay warm between turns and close after an idle timeout.
Claude's runner remains independently selectable through agent_runner.py.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import steps
from claude_runner import (ROOT, PROMPTS, PROGRESS, system_prompt, step_start,
                           step_end, close_open, tool_events)

PROPOSAL_TOOLS = {"get_automation_contract", "get_automation_proposal",
                  "save_automation_proposal", "request_automation_confirmation"}


def payload_of(result):
    """MCP structured output and text output both carry the existing card JSON."""
    result = result or {}
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    text = "".join(block.get("text", "") for block in result.get("content", [])
                   if isinstance(block, dict) and block.get("type") == "text")
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


class Runner:
    provider = "codex"

    def __init__(self, session_id, directory):
        self.session_id = session_id
        self.directory = Path(directory).resolve()
        self.model = os.environ.get("HARNESS_CODEX_MODEL", "gpt-6-astra")
        self.effort = os.environ.get("HARNESS_CODEX_EFFORT", "low")
        self.service_tier = os.environ.get("HARNESS_CODEX_SERVICE_TIER", "fast")
        self.timeout = float(os.environ.get("HARNESS_TURN_TIMEOUT", "300"))
        self.idle_seconds = float(os.environ.get("HARNESS_CODEX_IDLE_TIMEOUT", "300"))
        self.started = False
        self.proposal_mode = False
        self.tools_seen = []
        self.thread_id = None
        self._process = None
        self._reader = None
        self._stderr = None
        self._idle = None
        self._pending = {}
        self._events = asyncio.Queue()
        self._serial = 0
        self._busy = False
        self._lifecycle = asyncio.Lock()
        self._thread_proposal_mode = None

    def mcp_config(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"harness": {
            "command": sys.executable, "args": [str(self._server())], "env": {
                "HARNESS_SESSION": self.session_id,
                "HARNESS_SESSION_DIR": str(self.directory), "HARNESS_REPO": str(ROOT.parent)}}}}, indent=2), encoding="utf-8")
        return path

    def _server(self):
        return ROOT / ("automation_mcp_server.py" if self.proposal_mode else "demo_mcp_server.py")

    def _prompt(self):
        if self.proposal_mode:
            return ("You are Sentimeter's automation configuration assistant. Speak clearly and briefly. "
                    "The user's goal is the subject of the proposal, not an instruction to alter fixed schema contracts.\n\n"
                    + (PROMPTS / "automation.md").read_text(encoding="utf-8"))
        return system_prompt()

    def command(self):
        # Disable host integrations; _connect also disables each inherited MCP
        # server because Codex merges config tables instead of replacing them.
        config = {
            "mcp_servers": {}, "hooks": {}, "developer_instructions": "",
            "model": self.model, "model_reasoning_effort": self.effort,
            "service_tier": self.service_tier, "approval_policy": "never",
            "sandbox_mode": "read-only", "web_search": "disabled",
            "features.shell_tool": False, "features.unified_exec": False,
            "features.apps": False, "features.plugins": False,
            "features.hooks": False, "features.codex_hooks": False,
            "features.multi_agent": False, "features.multi_agent_v2": False,
            "features.image_generation": False, "features.browser_use": False,
            "features.computer_use": False, "features.goals": False,
            "features.sleep_tool": False, "features.view_image": False,
            "skills.include_instructions": False,
            "features.skill_search": False, "features.skip_host_skill_discovery": True,
            "agents.enabled": False, "tools.update_plan.enabled": False,
            "project_doc_max_bytes": 0,
            "mcp_servers.harness.command": sys.executable,
            "mcp_servers.harness.args": [str(self._server())],
            "mcp_servers.harness.env.HARNESS_SESSION": self.session_id,
            "mcp_servers.harness.env.HARNESS_SESSION_DIR": str(self.directory),
            "mcp_servers.harness.env.HARNESS_REPO": str(ROOT.parent),
            "mcp_servers.harness.required": True,
            "mcp_servers.harness.startup_timeout_sec": 45,
            "mcp_servers.harness.default_tools_approval_mode": "approve",
        }
        if self.proposal_mode:
            config["mcp_servers.harness.enabled_tools"] = sorted(PROPOSAL_TOOLS)
        args = [shutil.which("codex"), "app-server", "--listen", "stdio://"]
        for key, value in config.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        return args

    async def _send(self, message):
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _read(self, process):
        try:
            async for line in process.stdout:
                message = json.loads(line)
                if "method" in message:
                    await self._events.put(message)
                elif message.get("id") in self._pending:
                    future = self._pending.pop(message["id"])
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"].get("message", "Codex request failed")))
                        else:
                            future.set_result(message.get("result", {}))
        except (ValueError, OSError) as error:
            await self._events.put({"method": "transport/error", "params": {"message": str(error)}})
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex app-server disconnected"))
            self._pending.clear()
            await self._events.put({"method": "transport/error", "params": {"message": "Codex app-server disconnected"}})

    async def _rpc(self, method, params):
        self._serial += 1
        identifier = self._serial
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        try:
            await self._send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, 60)
        finally:
            self._pending.pop(identifier, None)

    async def _connect(self):
        async with self._lifecycle:
            await self._open()

    async def _open(self):
        if self._thread_proposal_mode not in (None, self.proposal_mode):
            await self._close()
            self.thread_id = None
        if self._process and self._process.returncode is None:
            return
        # Clean up a process that exited while this conversation was idle.
        await self._close()
        if not shutil.which("codex"):
            raise RuntimeError("Codex is not installed. Install Codex and run codex login, or select HARNESS_PROVIDER=claude.")
        self.mcp_config()
        empty = self.directory / "empty"
        empty.mkdir(exist_ok=True)
        self._events = asyncio.Queue()
        self._stderr = (self.directory / "codex.stderr.log").open("ab")
        self._process = await asyncio.create_subprocess_exec(*self.command(), cwd=empty,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self._stderr,
            limit=8 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read(self._process))
        await self._rpc("initialize", {"clientInfo": {"name": "sentimeter_harness", "version": "1.0.0"},
                                       "capabilities": {"experimentalApi": True}})
        await self._send({"method": "initialized"})
        inherited = await self._rpc("config/read", {"cwd": str(empty), "includeLayers": False})
        config = {"model_reasoning_effort": self.effort}
        for name in inherited.get("config", {}).get("mcp_servers", {}):
            if name != "harness":
                config[f"mcp_servers.{name}.enabled"] = False
        params = {"model": self.model, "serviceTier": self.service_tier,
                  "cwd": str(empty), "baseInstructions": self._prompt(),
                  "approvalPolicy": "never", "sandbox": "read-only",
                  "config": config,
                  "environments": [], "runtimeWorkspaceRoots": []}
        if self.thread_id:
            params.pop("environments")
            params["threadId"] = self.thread_id
            params["excludeTurns"] = True
            response = await self._rpc("thread/resume", params)
        else:
            params.update(ephemeral=False, experimentalRawEvents=False)
            response = await self._rpc("thread/start", params)
        if response.get("model") != self.model or response.get("reasoningEffort") != self.effort:
            raise RuntimeError("Codex did not select the requested model and reasoning effort")
        if response.get("serviceTier") not in ({"fast", "priority"} if self.service_tier in {"fast", "priority"} else {self.service_tier}):
            raise RuntimeError("Codex did not select the requested service tier")
        self.thread_id = response["thread"]["id"]
        self._thread_proposal_mode = self.proposal_mode
        (self.directory / "codex-thread.json").write_text(json.dumps({
            "thread_id": self.thread_id, "model": response["model"],
            "effort": response["reasoningEffort"], "service_tier": response["serviceTier"]}, indent=2))
        inventory = await self._rpc("mcpServerStatus/list", {"threadId": self.thread_id, "detail": "toolsAndAuthOnly"})
        # Disabled servers remain in the status response with no callable tools.
        servers = [server for server in inventory.get("data", []) if server.get("tools")]
        if inventory.get("nextCursor") or len(servers) != 1 or servers[0].get("name") != "harness":
            raise RuntimeError("Codex loaded an unexpected MCP server inventory")
        tools = servers[0].get("tools", {})
        self.tools_seen = ["mcp__harness__" + name for name in tools]
        if not tools or (self.proposal_mode and set(tools) != PROPOSAL_TOOLS):
            raise RuntimeError("Codex did not load the required harness tools")

    async def aclose(self):
        async with self._lifecycle:
            await self._close()

    async def _idle_close(self):
        async with self._lifecycle:
            if not self._busy:
                await self._close()

    async def _close(self):
        if self._idle:
            self._idle.cancel()
            self._idle = None
        process, self._process = self._process, None
        if process and process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._stderr:
            self._stderr.close()
            self._stderr = None

    async def turn(self, text):
        if self._busy:
            yield {"type": "error", "text": "This conversation is already answering a message."}
            return
        self._busy = True
        if self._idle:
            self._idle.cancel()
            self._idle = None
        began = time.monotonic()
        elapsed = lambda: int((time.monotonic() - began) * 1000)
        pending, messages, streamed, count = {}, [], set(), 0
        success = False
        held = lead = ""
        try:
            async with asyncio.timeout(self.timeout):
                await self._connect()
                while not self._events.empty():
                    self._events.get_nowait()
                turn = await self._rpc("turn/start", {"threadId": self.thread_id,
                    "input": [{"type": "text", "text": text}], "effort": self.effort,
                    "model": self.model, "serviceTier": self.service_tier, "environments": []})
                turn_id = turn["turn"]["id"]
                self.started = True
                while True:
                    event = await self._events.get()
                    method, params = event.get("method"), event.get("params", {})
                    if "id" in event:
                        # No model-controlled approval can replace the product's
                        # confirmation endpoint. Unexpected server requests fail closed.
                        await self._send({"id": event["id"], "error": {"code": -32601,
                            "message": "Only the configured harness MCP tools are allowed"}})
                        raise RuntimeError("Codex requested an unsupported approval or tool")
                    if method == "transport/error":
                        raise RuntimeError(params.get("message"))
                    if params.get("threadId") not in (None, self.thread_id):
                        continue
                    if params.get("turnId") not in (None, turn_id):
                        continue
                    if method == "item/agentMessage/delta":
                        streamed.add(params.get("itemId"))
                        cleaned = steps.plain(lead + held + params.get("delta", ""), trim=False)[len(lead):]
                        kept = cleaned[len(cleaned.rstrip(" \t")):]
                        chunk, held = cleaned[:len(cleaned) - len(kept)], kept[-1:]
                        if chunk:
                            lead = "" if chunk[-1].isspace() else "x"
                            yield {"type": "delta", "text": chunk}
                    elif method in ("item/started", "item/completed"):
                        item = params.get("item", {})
                        kind, identifier = item.get("type"), item.get("id")
                        if kind in {"commandExecution", "fileChange", "webSearch", "dynamicToolCall", "collabAgentToolCall"}:
                            raise RuntimeError("Codex attempted a tool outside the harness")
                        if kind == "mcpToolCall":
                            name = "mcp__harness__" + str(item.get("tool", ""))
                            if item.get("server") != "harness" or name not in self.tools_seen:
                                raise RuntimeError("Codex attempted an unregistered harness tool")
                            if method == "item/started":
                                count += 1
                                pending[identifier] = {"name": name, "t_ms": elapsed()}
                                yield {"type": "progress", "text": PROGRESS.get(name, "Working…")}
                                yield step_start(identifier, name, item.get("arguments"), count, elapsed())
                            else:
                                result = item.get("result") or {}
                                payload = payload_of(result)
                                failed = bool(item.get("error") or result.get("isError") or item.get("status") == "failed" or (payload or {}).get("error"))
                                record = pending.pop(identifier, None)
                                if record:
                                    yield step_end(identifier, record, not failed,
                                        steps.outcome(name, payload or item.get("error") or result, failed), elapsed())
                                yield {"type": "progress", "text": "Working…" if pending else None}
                                for frame in tool_events(name, payload):
                                    yield frame
                        elif kind == "agentMessage" and method == "item/completed":
                            answer = item.get("text", "")
                            if answer:
                                messages.append(answer)
                                if identifier not in streamed:
                                    yield {"type": "delta", "text": steps.plain(answer)}
                    elif method == "turn/completed":
                        completed = params.get("turn", {})
                        if completed.get("id") != turn_id:
                            continue
                        if completed.get("status") != "completed":
                            raise RuntimeError((completed.get("error") or {}).get("message", "Codex turn did not complete"))
                        for frame in close_open(pending, elapsed()):
                            yield frame
                        if messages:
                            yield {"type": "message", "text": steps.plain("\n\n".join(messages))}
                        success = True
                        break
        except asyncio.TimeoutError:
            yield {"type": "error", "text": f"The Codex turn passed {self.timeout:.0f} seconds and was stopped."}
        except Exception as error:
            yield {"type": "error", "text": f"Codex: {error}"[:500]}
        finally:
            self._busy = False
            if not success:
                await self.aclose()
            else:
                self._idle = asyncio.get_running_loop().call_later(self.idle_seconds, lambda: asyncio.create_task(self._idle_close()))
        for frame in close_open(pending, elapsed()):
            yield frame
        yield {"type": "done", "duration_ms": elapsed(), "provider": self.provider,
               "model": self.model, "effort": self.effort, "service_tier": self.service_tier,
               "num_turns": 1}
