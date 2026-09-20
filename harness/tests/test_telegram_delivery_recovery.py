"""Pure offline transport fixtures: recover known-unsent text without replaying model or audio work."""
import asyncio
import contextlib
import json
import ssl
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aiohttp
import telegram_bot as tg

ANSWER = 'The completed answer, including the original timestamps.'
CONNECTION = types.SimpleNamespace(ssl=None, host='offline-fixture', port=443)


def disconnected():
    return aiohttp.ClientConnectorError(CONNECTION, OSError('offline test fixture'))


class Response:
    status = 200

    def __init__(self, result):
        self.result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return {'ok': True, 'result': self.result}


class Http:
    def __init__(self):
        self.calls, self.delivered, self.plans = [], [], {}
        self.offline = False

    def post(self, url, **options):
        method, payload = url.rsplit('/', 1)[-1], options.get('json', {})
        self.calls.append((method, payload))
        plan = self.plans.get((method, payload.get('text'))) or self.plans.get(method)
        if plan:
            error = plan.pop(0)
            if error:
                raise error
        elif self.offline and method == 'sendMessage':
            raise disconnected()
        if method == 'sendMessage':
            self.delivered.append(payload)
        return Response({'message_id': len(self.calls) + 100})


class Runner:
    def __init__(self, events=None):
        self.calls = []
        self.events = events or [{'type': 'message', 'text': ANSWER}, {'type': 'done', 'duration_ms': 100}]

    async def turn(self, text):
        self.calls.append(text)
        for event in self.events:
            yield event


class DeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root, self.http, self.at, self.sleeps = Path(self.temporary.name), Http(), 1000.0, []
        self.bot = self.make_bot()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def make_bot(self):
        return tg.Bot('fixture-token', {123}, self.http, base='https://offline-fixture', sleep=self.sleep,
                      sessions=self.root / 'sessions', chats_path=self.root / 'chats.json',
                      outbox_path=self.root / 'recovery.json', outbox_now=lambda: self.at)

    async def turn(self, runner=None):
        runner = runner or Runner()
        state = {'id': 123, 'session': 'test-session', 'busy': True, 'runner': runner, 'edited': 0}
        await self.bot.run_turn(state, 'fixture request')
        self.assertFalse(state['busy'])
        return runner

    async def test_connection_failure_retries_before_queueing(self):
        self.http.plans['sendMessage'] = [disconnected(), disconnected(), None]
        result = await self.bot.send(123, ANSWER, durable=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(self.sleeps, [1, 2])
        self.assertFalse(self.bot.pending_replies)
        self.assertEqual(self.bot.delivery_review[-1]['state'], 'sent')
        self.assertEqual(len(self.http.delivered), 1)

    async def test_completed_reply_survives_restart_and_recovers_without_model_or_audio(self):
        self.http.plans[('sendMessage', ANSWER)] = [disconnected(), disconnected(), disconnected()]
        runner = await self.turn()
        saved = json.loads(self.bot.outbox_path.read_text())
        self.assertEqual(len(saved['pending']), 1)
        self.assertEqual(saved['pending'][0]['body'], ANSWER)
        self.assertNotIn('Thinking', json.dumps(saved))
        self.assertEqual(runner.calls, ['fixture request'])
        self.assertTrue(any(method == 'editMessageText' and 'could not be confirmed' in data.get('text', '')
                            for method, data in self.http.calls))
        self.bot = self.make_bot()
        self.at += 5
        task = asyncio.create_task(self.bot.recover_replies())
        try:
            for _ in range(20):
                if not self.bot.pending_replies:
                    break
                await asyncio.sleep(0)
            self.assertFalse(self.bot.pending_replies)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.bot.retry_pending_replies()
        self.assertEqual(sum(row.get('text') == ANSWER for row in self.http.delivered), 1)
        self.assertEqual(runner.calls, ['fixture request'])
        self.assertFalse(any(method == 'sendAudio' for method, _ in self.http.calls))

    async def test_confirmation_card_keeps_the_original_confirmation_id(self):
        self.http.offline = True
        confirmation_id = 'a' * 16
        runner = await self.turn(Runner([{'type': 'confirm_request', 'kind': 'brief_telegram',
                                         'confirmation_id': confirmation_id, 'summary': 'Send this ready recording?'},
                                        {'type': 'done'}]))
        self.assertEqual(len(self.bot.pending_replies), 1)
        markup = self.bot.pending_replies[0]['markup']
        self.http.offline, self.at = False, self.at + 5
        await self.bot.retry_pending_replies()
        self.assertEqual(self.http.delivered[0]['reply_markup'], markup)
        buttons = markup['inline_keyboard'][0]
        self.assertEqual([button['callback_data'] for button in buttons], [f'c:{confirmation_id}:1', f'c:{confirmation_id}:0'])
        self.assertEqual(len(runner.calls), 1)

    async def test_timeout_and_read_reset_are_saved_for_review_without_retry(self):
        for problem in (TimeoutError('unknown outcome'), aiohttp.ServerDisconnectedError('response lost')):
            before = len(self.http.calls)
            self.http.plans['sendMessage'] = [problem]
            self.assertIsNone(await self.bot.send(123, ANSWER, durable=True))
            self.assertEqual(len(self.http.calls), before + 1)
            self.assertFalse(self.bot.pending_replies)
            self.assertEqual(self.bot.delivery_review[-1]['state'], 'uncertain')
            self.assertEqual(self.bot.delivery_review[-1]['body'], ANSWER)
            self.at += 100
            await self.bot.retry_pending_replies()
            self.assertEqual(len(self.http.calls), before + 1)

    async def test_attempts_one_upload_and_certificate_errors_are_not_retried(self):
        self.http.plans['sendAudio'] = [disconnected()]
        with self.assertRaises(aiohttp.ClientConnectorError):
            await self.bot.api.call('sendAudio', attempts=1,
                                    upload=('audio', ('fixture.mp3', b'fixture', 'audio/mpeg')), chat_id=123)
        self.assertEqual(sum(method == 'sendAudio' for method, _ in self.http.calls), 1)
        for problem in (aiohttp.ClientConnectorCertificateError(CONNECTION, ssl.CertificateError('fixture')),
                        aiohttp.ClientConnectorSSLError(CONNECTION, ssl.SSLError('fixture'))):
            before = len(self.http.calls)
            self.http.plans['sendMessage'] = [problem]
            await self.bot.send(123, ANSWER, durable=True)
            self.assertEqual(len(self.http.calls), before + 1)
            self.assertFalse(self.bot.pending_replies)
            self.assertEqual(self.bot.delivery_review[-1]['state'], 'failed')

    async def test_removed_chat_cannot_receive_a_queued_reply(self):
        self.http.offline = True
        await self.bot.send(123, ANSWER, durable=True)
        before = len(self.http.calls)
        self.bot.allowed.clear()
        self.at += 100
        await self.bot.retry_pending_replies()
        self.assertFalse(self.bot.pending_replies)
        self.assertEqual(len(self.http.calls), before)
        self.assertEqual(self.bot.delivery_review[-1]['state'], 'failed')

    async def test_retry_queue_and_backoff_are_bounded(self):
        self.http.offline = True
        with patch.object(tg, 'OUTBOX_MAX_PENDING', 2), patch.object(tg, 'OUTBOX_MAX_REVIEW', 2):
            for index in range(4):
                self.at += 1
                await self.bot.send(123, f'Answer {index}', durable=True)
            self.assertEqual(len(self.bot.pending_replies), 2)
            self.assertLessEqual(len(self.bot.delivery_review), 2)
            self.assertEqual(self.bot.delivery_review[-1]['state'], 'unsent')
            oldest = self.bot.pending_replies[0]['id']
            for _ in range(7):
                self.at += 100
                await self.bot.retry_pending_replies()
                record = next(row for row in self.bot.pending_replies if row['id'] == oldest)
                self.assertLessEqual(record['next_attempt_at'] - self.at, 60)
            self.assertEqual(self.bot.pending_replies[0]['id'], oldest)

    async def test_interrupted_send_is_uncertain_on_restart_not_replayed(self):
        self.bot.remember_delivery({'id': 'fixture', 'chat_id': 123, 'body': ANSWER,
                                    'created_at': self.at, 'retry_count': 0}, 'sending')
        restarted = self.make_bot()
        self.assertEqual(restarted.delivery_review[-1]['state'], 'uncertain')
        self.assertFalse(restarted.pending_replies)
        await restarted.retry_pending_replies()
        self.assertFalse(self.http.calls)

    async def test_success_without_message_id_remains_uncertain(self):
        with patch.object(self.bot.api, 'call', AsyncMock(return_value=True)):
            self.assertIsNone(await self.bot.send(123, ANSWER, durable=True))
        self.assertEqual(self.bot.delivery_review[-1]['state'], 'uncertain')
        self.assertFalse(self.bot.pending_replies)


if __name__ == '__main__':
    unittest.main()
