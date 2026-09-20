# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb==1.5.5", "jsonschema>=4.23,<5", "pytz"]
# ///
"""A quiet tool must keep the HTTP stream alive without making fake progress."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge


class QuietRunner:
    async def turn(self, text):
        await asyncio.sleep(.15)
        yield {'type': 'message', 'text': 'Complete'}
        yield {'type': 'done'}


async def main():
    session = {'id': 'heartbeat-check', 'runner': QuietRunner(), 'busy': True}
    app = web.Application()
    app['state'] = {'gate': asyncio.Semaphore(1)}
    async def handle(request):
        return await bridge.stream(request, session, 'test')
    app.router.add_post('/test', handle)
    with patch.object(bridge, 'SSE_HEARTBEAT_SECONDS', .02):
        async with TestClient(TestServer(app)) as client:
            async with client.post('/test') as response:
                first = (await response.content.readline()).decode()
                assert first.startswith(': keepalive'), first
                rest = (await response.read()).decode()
                frames = [json.loads(line[5:]) for line in rest.splitlines() if line.startswith('data:')]
                assert frames == [{'type': 'message', 'text': 'Complete'}, {'type': 'done'}], frames
    assert session['busy'] is False
    print('PASS: quiet tools emit SSE keepalives, preserve result frames and release the session')


if __name__ == '__main__':
    asyncio.run(main())
