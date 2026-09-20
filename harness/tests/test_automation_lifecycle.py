# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb==1.5.5", "jsonschema>=4.23,<5", "pytz"]
# ///
"""Configuration authorization, retry idempotence and independent browser leases."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation_tools as tools
from automation_api import Manager, historical


class FakeManager(Manager):
    def __init__(self):
        super().__init__()
        self.autos, self.calls = {}, []
        self.stopped = False

    async def ensure(self):
        pass

    async def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/automations':
            if method == 'POST':
                auto = dict(id=str(len(self.autos) + 1), enabled=False, **body)
                self.autos[auto['id']] = auto
                return {key: value for key, value in auto.items() if key != 'config'}
            return {'automations': list(self.autos.values())}
        if path == '/status':
            return {'automations': list(self.autos.values())}
        if path == '/shutdown':
            self.stopped = True
            return {}
        identifier = path.split('/')[2]
        if path.endswith('/start'):
            self.autos[identifier]['enabled'] = True
        elif path.endswith('/pause'):
            self.autos[identifier]['enabled'] = False
        return dict(self.autos[identifier])


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_only_current_confirmed_schema_launches_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, HARNESS_SESSION_DIR=folder, TYPESAFE_API_KEY='offline-test'):
            manager = FakeManager()
            config = tools.get_automation_contract('Read schema', 'public_policy')['example']
            proposal = tools.save_automation_proposal('Save', 'Transit', json.dumps(config), [])['proposal']
            with self.assertRaisesRegex(ValueError, 'Confirm'):
                await manager.launch(Path(folder), proposal['proposal_hash'], .1)
            self.assertEqual(manager.calls, [])
            approval = tools.request_automation_confirmation('Review')
            tools.decide_proposal(Path(folder), approval['confirmation_id'], True)
            first = await manager.launch(Path(folder), proposal['proposal_hash'], .1)
            retry = await manager.launch(Path(folder), proposal['proposal_hash'], 10)
            self.assertEqual(first, retry)
            self.assertEqual(len(manager.autos), 1)
            self.assertEqual(retry['max_usd'], .1)
            self.assertEqual(manager.autos['1']['config'], config)
            with self.assertRaisesRegex(ValueError, 'Confirm'):
                await manager.launch(Path(folder), 'superseded-hash', 0)

    async def test_lost_viewer_stops_only_its_tracker_and_final_close_stops_service(self):
        manager = FakeManager()
        await manager.release('unknown', 'viewer')
        self.assertEqual(manager.leases, {})
        manager.autos = {'a': {'id': 'a', 'enabled': True}, 'b': {'id': 'b', 'enabled': True}}
        manager.leases = {'a': {'tab1': time.monotonic() - 1}, 'b': {'tab2': time.monotonic() + 30}}
        await manager.reap()
        self.assertFalse(manager.autos['a']['enabled'])
        self.assertTrue(manager.autos['b']['enabled'])
        self.assertFalse(manager.stopped)
        await manager.control('b', 'pause')
        self.assertNotIn('launch', manager.leases['b'], 'Closing must not create a fresh 30-second lease')
        manager.autos['b']['enabled'] = True
        manager.leases['b']['tab3'] = time.monotonic() + 30
        await manager.release('b', 'tab2')
        await manager.reap()
        self.assertTrue(manager.autos['b']['enabled'])
        manager.leases['b'] = {'lost-browser': time.monotonic() - 1}
        await manager.reap()
        self.assertFalse(manager.autos['b']['enabled'])
        self.assertTrue(manager.stopped)

    def test_historical_exposes_original_policy_not_new_live_template(self):
        contract = historical()
        self.assertEqual(contract['configuration']['policy']['sentiment']['prompt_version'], 'company-sentiment-choice-v1')
        self.assertEqual(contract['configuration']['configuration_key'], contract['configuration_key'])
        self.assertEqual(len(contract['taxonomy']['categories']), 27)


if __name__ == '__main__':
    unittest.main()
