"""Offline confirmation checks. Both brief and Telegram HTTP endpoints are local fixtures."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import brief_confirmation as confirmation
import brief_delivery as delivery
import bridge
import claude_runner
import telegram_bot

BRIEF = '20260919-230000'


class BriefConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.status, self.audio, self.reported_id = 'ready', b'existing audio fixture', None
        self.sent = []

        async def brief(request):
            return web.json_response({'id': self.reported_id or request.match_info['id'], 'status': self.status,
                                      'title': 'Saved episode', 'audio': {'full': 'brief.mp3'}})

        async def audio(request):
            return web.Response(body=self.audio, content_type='audio/mpeg')

        async def telegram(request):
            form = await request.post()
            self.sent.append((form['chat_id'], form['audio'].file.read()))
            return web.json_response({'ok': True, 'result': {'message_id': 42}})

        upstream = web.Application()
        upstream.add_routes([web.get('/api/briefs/{id}', brief), web.get('/api/briefs/{id}/audio/brief.mp3', audio),
                             web.post('/botfixture/sendAudio', telegram)])
        self.server = TestServer(upstream)
        await self.server.start_server()
        self.base = str(self.server.make_url('')).rstrip('/')
        self.patches = [patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fixture', 'TELEGRAM_ALLOWED_CHAT_IDS': '123',
                                               'TELEGRAM_DEFAULT_CHAT_ID': '', 'SIGNAL_BASE_URL': self.base,
                                               'HARNESS_SESSION_DIR': str(self.directory)}),
                        patch.object(delivery, 'RECEIPTS', self.directory / 'receipts.sqlite3'),
                        patch.object(delivery.telegram, 'API_BASE', self.base)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.server.close()
        self.temporary.cleanup()

    async def request(self, brief_id=BRIEF, directory=None):
        result = await confirmation.request_confirmation(brief_id, directory=directory, brief_base=self.base)
        self.assertIn('confirmation_id', result, result)
        return result

    def path(self, result):
        return self.directory / 'confirmations' / f"{result['confirmation_id']}.json"

    async def decide(self, result, approved=True):
        return await confirmation.decide_confirmation(self.directory, result['confirmation_id'], approved)

    async def test_request_snapshots_audio_without_sending_and_reuses_pending_request(self):
        first, second = await self.request(), await self.request()
        self.assertEqual(first['confirmation_id'], second['confirmation_id'])
        self.assertEqual(first['kind'], 'brief_telegram')
        record = json.loads(self.path(first).read_text())
        self.assertEqual(record['delivery']['chat_id'], 123)
        self.assertEqual(record['delivery']['audio_bytes'], len(self.audio))
        self.assertEqual(len(record['delivery']['audio_sha256']), 64)
        self.assertIsNone(record['decision'])
        self.assertFalse(self.sent)

    async def test_generation_failure_working_empty_and_wrong_brief_cannot_request(self):
        for status in ('working', 'failed'):
            self.status = status
            self.assertIn('error', await confirmation.request_confirmation(BRIEF))
        self.status, self.audio = 'ready', b''
        self.assertIn('empty', (await confirmation.request_confirmation(BRIEF))['error'])
        self.audio, self.reported_id = b'fixture', '20260919-230001'
        self.assertIn('different brief', (await confirmation.request_confirmation(BRIEF))['error'])
        self.assertIn('error', await confirmation.request_confirmation('../secret'))
        self.assertFalse(self.sent)
        self.assertFalse(list(self.directory.glob('confirmations/*.json')))

    async def test_decline_is_immutable_and_never_sends(self):
        pending = await self.request()
        self.assertTrue((await self.decide(pending, False))['declined'])
        saved = self.path(pending).read_bytes()
        self.assertIn('already decided', (await self.decide(pending, True))['error'])
        self.assertEqual(saved, self.path(pending).read_bytes())
        self.assertFalse(self.sent)

    async def test_expired_confirmation_cannot_send_or_change_decision(self):
        pending = await self.request()
        record = json.loads(self.path(pending).read_text())
        record['expires_ms'] = 0
        self.path(pending).write_text(json.dumps(record))
        saved = self.path(pending).read_bytes()
        self.assertIn('expired', (await self.decide(pending))['error'])
        self.assertEqual(saved, self.path(pending).read_bytes())
        self.assertFalse(self.sent)

    async def test_concurrent_confirm_and_new_confirmation_send_existing_audio_once(self):
        pending = await self.request()
        results = await asyncio.gather(self.decide(pending), self.decide(pending))
        self.assertEqual(sum(bool(result.get('sent')) for result in results), 1)
        self.assertEqual(self.sent, [('123', b'existing audio fixture')])
        record = json.loads(self.path(pending).read_text())
        self.assertEqual(record['decision'], 'approved')
        self.assertTrue(record['consumed'])
        again = await self.request()
        self.assertNotEqual(pending['confirmation_id'], again['confirmation_id'])
        self.assertTrue((await self.decide(again))['already_sent'])
        self.assertEqual(len(self.sent), 1)

    async def test_changed_recording_or_failed_generation_after_request_never_sends(self):
        pending = await self.request()
        self.audio = b'replaced recording'
        self.assertIn('recording changed', (await self.decide(pending))['error'])
        pending = await self.request()
        self.status = 'failed'
        self.assertIn('ready audio', (await self.decide(pending))['error'])
        self.assertFalse(self.sent)

    async def test_changed_destination_and_foreign_telegram_callback_never_send(self):
        pending = await self.request()
        result = await confirmation.decide_confirmation(self.directory, pending['confirmation_id'], True, expected_chat_id=456)
        self.assertIn('different Telegram chat', result['error'])
        self.assertIsNone(json.loads(self.path(pending).read_text())['decision'])
        with patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '123,456', 'TELEGRAM_DEFAULT_CHAT_ID': '456'}):
            self.assertIn('destination changed', (await self.decide(pending))['error'])
        self.assertFalse(self.sent)

    async def test_saved_confirmation_cannot_point_to_a_different_brief(self):
        pending = await self.request()
        record = json.loads(self.path(pending).read_text())
        record['brief_id'] = '20260919-230001'
        self.path(pending).write_text(json.dumps(record))
        self.assertIn('does not match', (await self.decide(pending))['error'])
        self.assertFalse(self.sent)

    async def test_web_button_delivers_directly_without_model_turn(self):
        pending = await self.request()
        runner = type('Runner', (), {'turn': AsyncMock(side_effect=AssertionError('Must not call the model'))})()
        app = web.Application(middlewares=[bridge.local_only])
        app['state'] = {'sessions': {'test': {'dir': self.directory, 'busy': False, 'runner': runner}}}
        app.router.add_post('/api/sessions/{id}/confirm', bridge.post_confirm)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/sessions/test/confirm', json={'confirmation_id': pending['confirmation_id'], 'approved': True})
            self.assertEqual(response.status, 200)
            self.assertIn('Your brief was sent to Telegram.', await response.text())
            repeat = await client.post('/api/sessions/test/confirm', json={'confirmation_id': pending['confirmation_id'], 'approved': False})
            self.assertEqual(repeat.status, 409)
        runner.turn.assert_not_called()
        self.assertEqual(len(self.sent), 1)

    async def test_telegram_button_delivers_directly_and_decline_stays_declined(self):
        bot = telegram_bot.Bot('fixture', {123}, None, runner_factory=lambda *_: object(),
                               sessions=self.directory / 'sessions', chats_path=self.directory / 'chats.json', brief_base=self.base)
        bot.api.call, bot.edit, bot.run_turn = AsyncMock(), AsyncMock(), AsyncMock()
        directory = bot.chat(123)['dir']
        for approved in (False, True):
            pending = await self.request(directory=directory)
            query = {'id': 'callback', 'data': f"c:{pending['confirmation_id']}:{int(approved)}",
                     'message': {'chat': {'id': 123}, 'message_id': 10, 'text': 'Send this recording?'}}
            await bot.on_callback(query)
            count = len(self.sent)
            await bot.on_callback(query | {'data': f"c:{pending['confirmation_id']}:{int(not approved)}"})
            self.assertEqual(len(self.sent), count)
        bot.run_turn.assert_not_called()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(bot.chat(123)['busy'])

    async def test_model_brief_card_only_announces_ready_audio(self):
        bot = telegram_bot.Bot('fixture', {123}, None, sessions=self.directory / 'sessions',
                               chats_path=self.directory / 'chats.json', brief_base=self.base)
        bot.ask = AsyncMock(return_value=(200, {'status': 'ready', 'audio': {'full': 'brief.mp3'}}))
        bot.send, bot.send_audio = AsyncMock(), AsyncMock()
        await bot.follow_brief(123, {'kind': 'brief', 'brief_id': BRIEF, 'title': 'Saved episode'})
        bot.send_audio.assert_not_called()
        self.assertIn('needs your confirmation', bot.send.call_args.args[1])
        self.assertEqual(bot.ask.call_count, 1)

    async def test_request_tool_result_emits_confirmation_card(self):
        pending = await self.request()
        events = list(claude_runner.tool_events('mcp__harness__request_brief_delivery_confirmation', pending))
        self.assertEqual(events[0]['type'], 'confirm_request')
        self.assertEqual(events[0]['kind'], 'brief_telegram')

    async def test_brief_command_preserves_instructions_to_wait_before_sending(self):
        bot = telegram_bot.Bot('fixture', {123}, None, runner_factory=lambda *_: object(),
                               sessions=self.directory / 'sessions', chats_path=self.directory / 'chats.json', brief_base=self.base)
        bot.on_brief, bot.run_turn = AsyncMock(), AsyncMock()
        instruction = '/brief make audio now but do not send until I confirm'
        await bot.on_message({'chat': {'id': 123}, 'text': instruction})
        bot.on_brief.assert_not_called()
        self.assertEqual(bot.run_turn.call_args.args[1], instruction)
        bot.chat(123)['busy'] = False
        await bot.on_message({'chat': {'id': 123}, 'text': '/brief'})
        bot.on_brief.assert_awaited_once_with(123, [])


if __name__ == '__main__':
    unittest.main()
