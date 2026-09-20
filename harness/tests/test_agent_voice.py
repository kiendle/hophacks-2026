"""Offline token boundary tests. No paid voice session is opened."""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_voice as agent


async def check():
    calls = []
    failed = False
    async def token(request):
        calls.append(request.headers.get('xi-api-key'))
        assert request.query['agent_id'] == 'test-agent'
        return web.json_response({'error': 'secret-provider-message'} if failed else {'token': 'short-lived-token'}, status=401 if failed else 200)
    upstream = web.Application()
    upstream.router.add_get('/v1/convai/conversation/token', token)
    async with TestServer(upstream) as provider:
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': 'private-key', 'ELEVENLABS_AGENT_ID': 'test-agent', 'ELEVENLABS_API_BASE': str(provider.make_url('')).rstrip('/')}):
            app = web.Application()
            agent.setup(app)
            async with TestClient(TestServer(app)) as client:
                status = await client.get('/api/live/agent/status')
                assert (await status.json())['available'] and not calls
                denied = await client.post('/api/live/agent/session', headers={'Origin': 'https://untrusted.example'})
                assert denied.status == 403 and not calls
                response = await client.post('/api/live/agent/session')
                assert await response.json() == {'token': 'short-lived-token'}
                assert response.headers['Cache-Control'] == 'no-store' and calls == ['private-key']
                failed = True
                response = await client.post('/api/live/agent/session')
                text = await response.text()
                assert response.status == 502 and 'private-key' not in text and 'secret-provider-message' not in text
                with patch.dict(os.environ, {'ELEVENLABS_API_KEY': ''}):
                    assert (await client.post('/api/live/agent/session')).status == 503
    config = agent.agent_config()
    assert config['platform_settings']['auth']['enable_auth']
    assert config['conversation_config']['agent']['first_message'] == ''
    tool = config['conversation_config']['agent']['prompt']['tools'][0]
    assert tool['expects_response'] and tool['name'] == 'ask_workspace'
    print('PASS: private agent, user speaks first, origin guard, ephemeral tokens, provider errors and missing configuration.')


asyncio.run(check())
