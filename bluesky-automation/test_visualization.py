# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5", "jsonschema>=4.23,<5", "httpx>=0.28,<0.29", "aiohttp>=3.12,<4", "websockets>=15,<16", "mcp>=1.28,<2", "pydantic>=2.11,<3"]
# ///
"""Late completions, updates, likes and reconnect delivery through the real CLI store."""
import asyncio
import json
import tempfile
from pathlib import Path
import unittest
import httpx
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from websockets.asyncio.server import serve as websocket_server

from test_service import fixture_config, post, like, LABELS
from store import Store
from runtime import Runtime
from visualization import changes, ingestion
from api import serve
from test_service import until


def complete(rows):
    return [dict(content_version=row['content_version'], status='ready',
        classification={'status': 'accepted', 'companies': ['openai'], 'probabilities': {'openai': .95}},
        sentiment={'openai': {'type': 'choice', 'choice': 'positive', 'confidence': .96,
                             'probabilities': {label: .96 if label == 'positive' else .01 for label in LABELS}}}) for row in rows]


class Projection(unittest.TestCase):
    def test_ingestion_counts_receipts_without_matches_and_expires_after_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'live.sqlite')
            try:
                auto = store.create_automation('no matches', fixture_config(), 0)
                with patch('store._utc_now', return_value='2026-09-20T13:00:00.000Z'):
                    # The old source timestamp must not hide newly received replay data.
                    store.ingest([post(1, 'unrelated cooking', rkey='old', cid='old')])
                payload = [post(2, 'unrelated gardening', rkey='new', cid='new'),
                           post(3, 'edited gardening', rkey='new', cid='edit', operation='update'),
                           like(4, subject='new', cid='new'),
                           post(5, '', rkey='old', operation='delete')]
                with patch('store._utc_now', return_value='2026-09-20T13:04:00.000Z'):
                    store.ingest(payload)
                    store.ingest(payload)  # Reconnect replay doesn't inflate receipts.
                now = datetime(2026, 9, 20, 13, 5, tzinfo=timezone.utc)
                activity = ingestion(store, now)
                self.assertEqual(activity['posts_last_5m'], 1)  # Exact left boundary expires.
                self.assertEqual(activity['events_last_5m'], 4)
                self.assertEqual(activity['last_post_at'], '2026-09-20T13:04:00.000Z')
                self.assertEqual(changes(store, auto['id'])['events'], [])
                self.assertEqual(changes(store, auto['id'])['counts'], {})
                store.close(); store = Store(Path(directory) / 'live.sqlite')
                self.assertEqual(ingestion(store, now), activity)
                later = datetime(2026, 9, 20, 13, 9, tzinfo=timezone.utc)
                self.assertEqual(ingestion(store, later)['posts_last_5m'], 0)
            finally:
                store.close()

    def test_delivery_pages_cover_more_than_latest_results_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'live.sqlite')
            try:
                auto = store.create_automation('pages', fixture_config(), 0)
                store.set_enabled(auto['id'], True)
                store.ingest([post(i, 'OpenAI shared text', rkey=str(i), cid=str(i)) for i in range(1, 1006)])
                while store.get_automation(auto['id'])['routing_cursor'] < 1005:
                    rows, through = store.fetch_posts(auto['id'])
                    for row in rows:
                        row.update(groups=['openai'], discovery=False)
                    store.route_posts(auto['id'], rows, through)
                store.save_results(auto['id'], complete(store.pending(auto['id'])))
                first = changes(store, auto['id'])
                self.assertEqual(len(first['events']), 1000)
                self.assertTrue(first['more'])
                second = changes(store, auto['id'], first['cursor'])
                self.assertEqual(len(second['events']), 5)
                self.assertFalse(second['more'])
                self.assertEqual(len({e['id'] for e in first['events'] + second['events']}), 1005)
            finally:
                store.close()

    def test_completion_cursor_does_not_lose_old_posts_or_double_count_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'live.sqlite')
            try:
                auto = store.create_automation('test', fixture_config(), 0)
                store.set_enabled(auto['id'], True)
                store.ingest([post(1, 'OpenAI original'), like(2), post(3, 'OpenAI edited', cid='cid-edit', operation='update')])
                # Route real mutations without invoking a provider.
                rows, through = store.fetch_posts(auto['id'], limit=100)
                for row in rows:
                    row.update(groups=['openai'], discovery=False)
                store.route_posts(auto['id'], rows, through)
                self.assertEqual(changes(store, auto['id'])['events'], [])
                store.save_results(auto['id'], complete(rows[1:]))  # newer text completes first
                first = changes(store, auto['id'])
                self.assertEqual(len(first['events']), 1)
                self.assertFalse(first['events'][0]['publication'])
                store.save_results(auto['id'], complete(rows[:1]))
                late = changes(store, auto['id'], first['cursor'])
                self.assertEqual(len(late['events']), 2)
                self.assertEqual(sorted(e['kind'] for e in late['events']), ['like', 'post'])
                self.assertEqual(next(e for e in late['events'] if e['kind']=='like')['delta'], 1)
                self.assertGreater(late['cursor'], first['cursor'])
                self.assertEqual(changes(store, auto['id'], late['cursor'])['events'], [])
                reconnect = changes(store, auto['id'])
                self.assertEqual(len(reconnect['events']), 3)
                self.assertEqual(len({e['id'] for e in reconnect['events']}), 3)
                self.assertTrue(all(e['grades'][0]['score'] == 9.75 for e in reconnect['events']))
            finally:
                store.close()


class Cancellation(unittest.IsolatedAsyncioTestCase):
    async def test_total_budget_survives_new_batches_and_runtime_restart(self):
        from budgeting import usage
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'live.sqlite')
            auto = store.create_automation('bounded', fixture_config(), .0061)
            store.set_enabled(auto['id'], True)
            calls = []
            async def provider(request):
                payload = json.loads(request.content); calls.append(payload)
                answers = {target: {'type': 'noul', 'noul': .95} if question['type']=='noul' else
                    {'type': 'choice', 'choice': 'positive', 'confidence': .96,
                     'probabilities': {label: .96 if label=='positive' else .01 for label in LABELS}}
                    for target, question in payload['questions'].items()}
                return httpx.Response(200, json={'model': payload['model'], 'answers': answers,
                    'usage': {'input_tokens': 40000, 'output_tokens': 20}})
            try:
                for seq in (1, 2):
                    store.ingest([post(seq, f'OpenAI different text {seq}', rkey=str(seq), cid=str(seq))])
                    rows, through = store.fetch_posts(auto['id'])
                    for row in rows:
                        row.update(groups=['openai'], discovery=False)
                    store.route_posts(auto['id'], rows, through)
                    runtime = Runtime(store, Path(directory), api_key='test', requests_per_minute=60000,
                                      transport=httpx.MockTransport(provider))
                    await runtime._enrich_one(auto['id'])
                self.assertEqual(len(calls), 3, 'Second native batch must not get a fresh full budget')
                self.assertEqual(store.get_automation(auto['id'])['worker_state'], 'budget_exhausted')
                self.assertLessEqual(sum(usage(Path(directory)/'inference.sqlite', auto['id']).values()), .0061)
            finally:
                store.close()

    async def test_managed_capture_closes_when_last_tracker_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            active = 0
            async def stream(connection):
                nonlocal active
                active += 1
                try:
                    await connection.wait_closed()
                finally:
                    active -= 1
            async with websocket_server(stream, '127.0.0.1', 0, subprotocols=['xrpc.v1.json']) as server:
                store = Store(Path(directory) / 'live.sqlite')
                auto = store.create_automation('capture', fixture_config(), 0)
                runtime = Runtime(store, Path(directory), source_url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream')
                runtime.managed = True
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                try:
                    await asyncio.sleep(.1)
                    self.assertEqual(active, 0)
                    store.set_enabled(auto['id'], True); runtime.wake()
                    await until(lambda: active == 1)
                    store.set_enabled(auto['id'], False); runtime.wake()
                    await runtime.stop_automation(auto['id'])
                    await until(lambda: active == 0)
                    await asyncio.sleep(.2)
                    self.assertEqual(active, 0)
                finally:
                    stop.set(); runtime.wake()
                    await task
                    store.close()

    async def test_service_expires_without_parent_and_windows_token_can_be_reopened(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, SENTIMETER_CONTROL_TTL='.2', SENTIMETER_MANAGED='1'):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            args = SimpleNamespace(state_dir=directory, concurrency=1, requests_per_minute=60,
                batch_size=1, port=port, source_url='ws://127.0.0.1:9/unused', run_seconds=None)
            for _ in range(2):
                started = time.monotonic()
                self.assertEqual(await asyncio.wait_for(serve(args), 5), 0)
                self.assertLess(time.monotonic() - started, 4)

    async def test_stop_cancels_provider_and_preserves_retryable_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'live.sqlite')
            auto = store.create_automation('cancel', fixture_config(), .1)
            store.set_enabled(auto['id'], True)
            store.ingest([post(1, 'OpenAI cancellation check')])
            rows, through = store.fetch_posts(auto['id'], limit=100)
            for row in rows:
                row.update(groups=['openai'], discovery=False)
            store.route_posts(auto['id'], rows, through)
            entered, cancelled = asyncio.Event(), asyncio.Event()
            async def provider(request):
                entered.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
            runtime = Runtime(store, Path(directory), api_key='offline-test', transport=httpx.MockTransport(provider))
            task = runtime._enrichment_task = asyncio.create_task(runtime._enrich_one(auto['id']))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                store.set_enabled(auto['id'], False)
                runtime.wake()
                await asyncio.wait_for(runtime.stop_automation(auto['id']), 2)
                self.assertTrue(task.cancelled())
                self.assertTrue(cancelled.is_set())
                self.assertFalse(store.get_automation(auto['id'])['enabled'])
                self.assertEqual(len(store.pending(auto['id'])), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                store.close()


if __name__ == '__main__':
    unittest.main()
