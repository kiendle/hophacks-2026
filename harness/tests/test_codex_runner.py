"""Offline provider selection and Codex app-server event contract tests."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
import claude_runner
from codex_runner import Runner, payload_of


def notification(method, **params):
    return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}


class Replay(Runner):
    def __init__(self, directory, events):
        super().__init__("test", directory)
        self.events = events
        self.closed = False

    async def _connect(self):
        self.thread_id = "thread"
        self.tools_seen = ["mcp__harness__request_automation_confirmation"]

    async def _rpc(self, method, params):
        assert method == "turn/start"
        assert params["environments"] == []
        for event in self.events:
            self._events.put_nowait(event)
        return {"turn": {"id": "turn"}}

    async def aclose(self):
        self.closed = True
        await super().aclose()


class CodexTests(unittest.IsolatedAsyncioTestCase):
    async def run_events(self, events, timeout=1):
        with tempfile.TemporaryDirectory() as folder:
            runner = Replay(folder, events)
            runner.timeout = timeout
            try:
                frames = [frame async for frame in runner.turn("hello")]
                return frames, runner.closed
            finally:
                await runner.aclose()

    async def test_stream_tool_confirmation_and_final_message(self):
        payload = {"confirmation_id": "confirm-test", "kind": "automation_proposal",
                   "_card": {"type": "automation_proposal", "status": "draft"}}
        item = {"type": "mcpToolCall", "id": "tool-1", "server": "harness",
                "tool": "request_automation_confirmation", "arguments": {"reason": "Review"}}
        frames, closed = await self.run_events([
            notification("item/started", item=item),
            notification("item/completed", item={**item, "status": "completed", "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}]}}),
            notification("item/agentMessage/delta", itemId="answer", delta="Ready "),
            notification("item/agentMessage/delta", itemId="answer", delta="to review."),
            notification("item/completed", item={"id": "answer", "type": "agentMessage", "text": "Ready to review."}),
            notification("turn/completed", turn={"id": "turn", "status": "completed"}),
        ])
        self.assertFalse(closed, "Successful sessions stay warm")
        self.assertEqual("".join(f["text"] for f in frames if f["type"] == "delta"), "Ready to review.")
        self.assertEqual(sum(f["type"] == "message" for f in frames), 1)
        self.assertEqual(next(f for f in frames if f["type"] == "confirm_request")["confirmation_id"], "confirm-test")
        self.assertEqual(next(f for f in frames if f["type"] == "card")["card"], payload["_card"])
        self.assertTrue(next(f for f in frames if f.get("phase") == "end")["ok"])
        self.assertEqual(frames[-1]["provider"], "codex")

    async def test_tool_failure_is_visible(self):
        item = {"type": "mcpToolCall", "id": "tool-1", "server": "harness",
                "tool": "request_automation_confirmation", "arguments": {}}
        frames, _ = await self.run_events([
            notification("item/started", item=item),
            notification("item/completed", item={**item, "status": "failed", "error": {"message": "Invalid arguments"}}),
            notification("turn/completed", turn={"id": "turn", "status": "completed"}),
        ])
        self.assertFalse(next(f for f in frames if f.get("phase") == "end")["ok"])
        self.assertFalse(any(f["type"] in {"card", "confirm_request"} for f in frames))

    async def test_unregistered_tools_stop_the_turn(self):
        for item in [{"type": "commandExecution"}, {"type": "mcpToolCall", "server": "personal", "tool": "execute"}]:
            frames, closed = await self.run_events([notification("item/started", item=item)])
            self.assertTrue(closed)
            self.assertEqual([f["type"] for f in frames], ["error", "done"])

    async def test_timeout_closes_pending_steps(self):
        frames, closed = await self.run_events([notification("item/started", item={
            "type": "mcpToolCall", "id": "pending", "server": "harness",
            "tool": "request_automation_confirmation", "arguments": {}})], timeout=0.01)
        self.assertTrue(closed)
        self.assertFalse(next(f for f in frames if f.get("phase") == "end")["ok"])
        self.assertEqual(frames[-1]["type"], "done")
        self.assertTrue(any(f["type"] == "error" for f in frames))

    def test_default_and_claude_selection(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(agent_runner.Runner("new", folder), Runner)
            os.environ["HARNESS_PROVIDER"] = "claude"
            self.assertIsInstance(agent_runner.Runner("legacy", folder), claude_runner.Runner)
            os.environ["HARNESS_PROVIDER"] = "typo"
            with self.assertRaises(ValueError):
                agent_runner.Runner("bad", folder)

    def test_payload_shapes(self):
        self.assertEqual(payload_of({"structuredContent": {"ok": True}}), {"ok": True})
        self.assertIsNone(payload_of({"content": [{"type": "text", "text": "tool failed"}]}))


if __name__ == "__main__":
    unittest.main()
