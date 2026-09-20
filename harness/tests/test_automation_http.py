"""Real HTTP confirmation path with offline proposals; never invokes an agent."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation_tools as tools
import bridge
from claude_runner import Runner, tool_events
from mcp.server.mcpserver import MCPServer


async def main():
    server = MCPServer("automation-test")
    registered = tools.register(server)
    assert len(registered) == 4
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HARNESS_SESSION_DIR": folder}):
        directory = Path(folder)
        runner = Runner("isolation-test", directory)
        runner.proposal_mode = True
        transport = json.loads(runner.mcp_config().read_text())
        assert transport['mcpServers']['harness']['args'][0].endswith('automation_mcp_server.py')
        config = tools.get_automation_contract("Get schema", "public_policy")["example"]
        tools.save_automation_proposal("Save proposal", "Policy", json.dumps(config), [])
        pending = tools.request_automation_confirmation("Review proposal")
        events = list(tool_events("mcp__harness__request_automation_confirmation", pending))
        assert events[0]["type"] == "confirm_request" and events[0]["kind"] == "automation_proposal"
        app = web.Application()
        class FakeRunner:
            started = False
            proposal_mode = False
            async def turn(self, text):
                self.started = True
                yield {"type": "message", "text": "Ready to discuss a proposal."}
                yield {"type": "done"}
        fake = FakeRunner()
        app["state"] = {"sessions": {"test": {"dir": directory, "busy": False, "runner": fake}}, "gate": asyncio.Semaphore(1)}
        app.router.add_post('/api/sessions/{id}/confirm', bridge.post_confirm)
        app.router.add_post('/api/sessions/{id}/messages', bridge.post_message)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/sessions/test/messages', json={"text": "Configure an automation", "purpose": "automation_proposal"})
            assert response.status == 200
            await response.text()
            assert fake.proposal_mode
            response = await client.post('/api/sessions/test/messages', json={"text": "Switch to normal tools"})
            assert response.status == 409
            path = '/api/sessions/test/confirm'
            body = {"confirmation_id": pending["confirmation_id"], "approved": True}
            response = await client.post(path, json=body)
            assert response.status == 200, await response.text()
            raw = await response.text()
            received = [json.loads(frame.removeprefix('data: ')) for frame in raw.strip().split('\n\n')]
            assert [event['type'] for event in received] == ['card', 'message', 'done']
            assert received[0]['card']['status'] == 'final'
            assert json.loads((directory / 'automation-final.json').read_text()) == config
            assert (await client.post(path, json=body)).status == 409
            assert (await client.post('/api/sessions/other/confirm', json=body)).status == 404
            assert (await client.post(path, json={**body, 'approved': 'true'})).status == 400
    print('PASS: MCP registration, confirmation event, HTTP approval, exact JSON export, duplicate/cross-session/invalid decisions.')


asyncio.run(main())
