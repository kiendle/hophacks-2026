"""Offline checks: exact brief delivery, destination safety, retries and HTTP wiring."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import brief_delivery as delivery
import brief_tools
import bridge

BRIEF = '20260919-230000'


async def check():
    sent = []
    ready = True
    refuse = False
    async def brief(request):
        return web.json_response({'status': 'ready' if ready else 'working', 'title': 'Saved episode', 'audio': {'full': 'brief.mp3'}})
    async def audio(request):
        return web.Response(body=b'saved audio fixture', content_type='audio/mpeg')
    async def telegram(request):
        form = await request.post()
        sent.append((form['chat_id'], form['audio'].file.read()))
        if refuse:
            return web.json_response({'ok': False, 'description': 'Forbidden'}, status=403)
        return web.json_response({'ok': True, 'result': {'message_id': 42}})
    upstream = web.Application()
    upstream.add_routes([web.get('/api/briefs/{id}', brief), web.get('/api/briefs/{id}/audio/brief.mp3', audio), web.post('/botfixture/sendAudio', telegram)])
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
        'TELEGRAM_BOT_TOKEN': 'fixture', 'TELEGRAM_ALLOWED_CHAT_IDS': '123', 'TELEGRAM_DEFAULT_CHAT_ID': '',
    }), patch.object(delivery, 'RECEIPTS', Path(temporary) / 'receipts.sqlite3'):
        async with TestServer(upstream) as server:
            base = str(server.make_url('')).rstrip('/')
            with patch.object(delivery.telegram, 'API_BASE', base), patch.dict(os.environ, {'SIGNAL_BASE_URL': base}):
                app = web.Application(middlewares=[bridge.local_only])
                delivery.setup(app)
                async with TestClient(TestServer(app)) as client:
                    assert 'error' in await delivery.deliver('../secret')
                    ready = False
                    assert 'error' in await delivery.deliver(BRIEF)
                    assert not sent
                    ready = True
                    response = await client.post(f'/api/briefs/{BRIEF}/telegram', headers={'Origin': 'https://untrusted.example'})
                    assert response.status == 403 and not sent
                    results = await asyncio.gather(*[client.post(f'/api/briefs/{BRIEF}/telegram') for _ in range(3)])
                    assert any(response.status == 200 for response in results)
                    assert sent == [('123', b'saved audio fixture')], sent
                    again = await delivery.deliver(BRIEF)
                    assert again['sent'] and again['already_sent'] and len(sent) == 1
                    refuse = True
                    result = await delivery.deliver('20260919-230001')
                    assert 'error' in result
                    refuse = False
                    assert (await delivery.deliver('20260919-230001'))['sent']
                    with patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '123,456'}):
                        assert 'Multiple' in (await delivery.deliver(BRIEF))['error']
                    with patch.dict(os.environ, {'TELEGRAM_DEFAULT_CHAT_ID': '999'}):
                        assert 'allowed' in (await delivery.deliver(BRIEF))['error']
                    with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
                        assert 'Connect Telegram' in (await delivery.deliver(BRIEF))['error']
    with patch.object(brief_tools, 'call', side_effect=[{'min_seconds': 45, 'max_seconds': 300}, {'id': BRIEF}]) as call:
        result = brief_tools.make_brief(seconds=180, long_length_requested=True)
        assert result['seconds'] == 90 and call.call_args.args[2]['seconds'] == 90
    assert brief_tools.send_brief_to_telegram in brief_tools.TOOLS
    assert delivery.telegram.parse_plan('7:00')[1] == 1.5
    assert delivery.telegram.parse_plan('7:00 3')[1] == 1.5
    assert delivery.telegram.parse_plan('7:00 1.5')[1] == 1.5
    print('PASS: ready audio delivery, no duplicate sends, origin guard, failures, destination validation, 90-second requests.')


if __name__ == '__main__':
    asyncio.run(check())
