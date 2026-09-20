"""Opt-in real harness conversation. Uses the selected provider's existing login.

Run with --run. The proposal-only MCP server has no collection/inference/submission tools.
"""
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runner import Runner
from automation_tools import _validate


async def main():
    async with AsyncExitStack() as cleanup:
        folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="automation-conversation-"))
        runner = Runner(str(uuid.uuid4()), folder)
        runner.proposal_mode = True
        close = getattr(runner, "aclose", None)
        if close:
            cleanup.push_async_callback(close)
        allowed = {"get_automation_contract", "get_automation_proposal", "save_automation_proposal", "request_automation_confirmation"}
        for index, question in enumerate([
            "I want to build an automation to analyze sentiment about AI companies. Help me choose what to track before finalizing anything.",
            "Track only OpenAI and Anthropic, including their AI products. Exclude unrelated business news and incidental mentions. Use the standard sentiment categories and a 0.75 uncalibrated relevance cutoff. Those choices are settled. Save the complete configuration and show me the confirmation button. Do not run anything.",
        ], 1):
            print(f"Starting conversation turn {index}", flush=True)
            async for event in runner.turn(question):
                if event["type"] == "error":
                    raise AssertionError(event.get("text"))
                if event["type"] == "step" and event.get("phase") == "start":
                    tool = event.get("detail", {}).get("tool", "").split("__")[-1]
                    assert tool in allowed, tool
                    print(f"  Tool: {tool}", flush=True)
                if event["type"] == "confirm_request":
                    print("  Confirmation card received", flush=True)
            if index == 1:
                assert not list((Path(folder) / "confirmations").glob("*.json")), "Vague goal must not trigger a final confirmation"
        proposal = json.loads((Path(folder) / "automation-proposal.json").read_text(encoding="utf-8"))
        assert {t["id"] for t in proposal["configuration"]["targets"]} == {"openai", "anthropic"}
        assert proposal["open_questions"] == []
        assert _validate(proposal["configuration"])["ok"]
        assert list((Path(folder) / "confirmations").glob("*.json"))
        assert not (Path(folder) / "automation-final.json").exists(), "Only the user button may finalize"
        print("PASS: real two-turn conversation produced a valid two-company proposal and awaited confirmation.")


if __name__ == "__main__":
    if "--run" not in sys.argv:
        raise SystemExit("Opt-in only: pass --run to use the configured provider login (HARNESS_PROVIDER=codex|claude).")
    asyncio.run(main())
