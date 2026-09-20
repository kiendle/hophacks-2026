"""UI-owned CLI automations: reviewed configs, bounded budgets, viewer leases.

The CLI remains the only writer/collector. Tokens never reach the browser. The
child also expires its control lease if this server crashes, independently of UI cleanup.
"""
import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import aiohttp
from aiohttp import web

import bridge
from automation_tools import _read, _write, _validate

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'harness/state/live-automations'
PORT = int(os.environ.get('SENTIMETER_CLI_PORT', '8767'))
SAFE_ID = re.compile(r'[a-zA-Z0-9_-]{1,80}')
LEASE_SECONDS = 30


def identifier(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError('Invalid automation or session ID.')
    return value


def budget(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('Set a total Jev spending limit from $0 to $100. $0 captures without scoring.')
    return float(value)


class Manager:
    def __init__(self, state=STATE, port=PORT):
        self.state, self.port = Path(state), port
        self.lock = asyncio.Lock()
        self.process = None
        self.leases = {}  # automation -> {viewer: monotonic deadline}

    async def call(self, method, path, body=None):
        token = (self.state / 'service.token').read_text(encoding='utf-8').strip()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
            async with client.request(method, f'http://127.0.0.1:{self.port}/v1{path}', json=body,
                                      headers={'Authorization': 'Bearer ' + token}) as response:
                result = await response.json()
                if response.status >= 400:
                    raise ValueError(result.get('error', 'The CLI request failed.'))
                return result

    async def ensure(self):
        try:
            await self.call('GET', '/status')
            return
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
            pass
        self.state.mkdir(parents=True, exist_ok=True)
        executable = shutil.which('uv')
        if not executable:
            raise ValueError('Install uv to run the automation CLI.')
        args = [executable, 'run', '--no-project', str(ROOT / 'bluesky-automation/automation.py'),
                '--state-dir', str(self.state), 'serve', '--port', str(self.port)]
        env = dict(os.environ, PYTHONUTF8='1', SENTIMETER_CONTROL_TTL='45', SENTIMETER_MANAGED='1')
        # .env is already loaded by the harness; credentials stay in the child environment.
        with (self.state / 'service.log').open('ab') as log:
            self.process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(150):
            await asyncio.sleep(.2)
            if self.process.poll() is not None:
                raise ValueError('The automation CLI could not start. See harness/state/live-automations/service.log.')
            try:
                status = await self.call('GET', '/status')
                # Nothing from an earlier browser/server lifetime resumes unattended.
                for auto in status['automations']:
                    if auto['enabled']:
                        await self.call('POST', f"/automations/{auto['id']}/pause", {})
                return
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
                continue
        raise ValueError('The automation CLI did not become ready within 30 seconds.')

    async def launch(self, directory, proposal_hash, max_usd):
        async with self.lock:
            proposal = _read(directory / 'automation-proposal.json')
            if not proposal or proposal['status'] != 'final' or proposal['proposal_hash'] != proposal_hash:
                raise ValueError('Confirm the current configuration before starting tracking.')
            _validate(proposal['configuration'])
            max_usd = budget(max_usd)
            if max_usd > 0 and not os.environ.get('TYPESAFE_API_KEY'):
                raise ValueError('Jev needs TYPESAFE_API_KEY in the server environment. Set $0 for capture only.')
            await self.ensure()
            previous = _read(directory / 'live-tracker.json')
            if previous and previous['proposal_hash'] == proposal_hash:
                auto = await self.call('GET', '/automations/' + previous['id'])
                # An idempotent retry cannot silently raise an existing budget.
            else:
                # Find a create whose HTTP response was lost before the mapping was saved.
                name = proposal['title'][:70] + ' [' + directory.name + ':' + proposal_hash[:12] + ']'
                existing = await self.call('GET', '/automations')
                auto = next((a for a in existing['automations'] if a['name'] == name), None)
                if auto:
                    auto = await self.call('GET', '/automations/' + auto['id'])
                else:
                    auto = await self.call('POST', '/automations', dict(name=name, config=proposal['configuration'], max_usd=max_usd))
                _write(directory / 'live-tracker.json', dict(id=auto['id'], proposal_hash=proposal_hash))
                auto = await self.call('GET', '/automations/' + auto['id'])
            await self.call('POST', f"/automations/{auto['id']}/start", {})
            self.leases.setdefault(auto['id'], {})['launch'] = time.monotonic() + LEASE_SECONDS
            return dict(id=auto['id'], title=proposal['title'], config=auto['config'], max_usd=auto['max_usd'])

    async def watch(self, auto, viewer, after):
        async with self.lock:
            await self.ensure()
            result = await self.call('GET', f'/automations/{auto}/events?after={after}')
            leases = self.leases.setdefault(auto, {})
            leases.pop('launch', None)
            leases[viewer] = time.monotonic() + LEASE_SECONDS
            # Viewing a closed tracker reads its results. Resume is always an explicit action.
            return result

    async def release(self, auto, viewer):
        async with self.lock:
            if auto not in self.leases:
                return
            self.leases[auto].pop(viewer, None)
            # Brief grace handles React remounts and navigation between views of the same tracker.
            self.leases.setdefault(auto, {})['closing'] = time.monotonic() + 2

    async def control(self, auto, action, value=None):
        async with self.lock:
            await self.ensure()
            path = f'/automations/{auto}/{action}'
            body = {'max_usd': budget(value)} if action == 'budget' else {}
            if action not in ('pause', 'start', 'budget', 'resume-live'):
                raise ValueError('Unknown tracker control.')
            if action == 'resume-live':
                path, body = '/source/resume-live', {'acknowledge_gap': True}
            result = await self.call('POST', path, body)
            if action == 'start':
                self.leases.setdefault(auto, {})['launch'] = time.monotonic() + LEASE_SECONDS
            return result

    async def reap(self):
        async with self.lock:
            now = time.monotonic()
            for auto, leases in list(self.leases.items()):
                self.leases[auto] = {key: expiry for key, expiry in leases.items() if expiry > now}
                if not self.leases[auto]:
                    try:
                        await self.call('POST', f'/automations/{auto}/pause', {})
                        del self.leases[auto]
                    except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
                        pass  # Other trackers must still be reaped; retry on the next tick.
            if not any(self.leases.values()):
                await self.shutdown()
                self.leases.clear()

    async def shutdown(self):
        with contextlib.suppress(ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            status = await self.call('GET', '/status')
            for auto in status['automations']:
                if auto['enabled']:
                    await self.call('POST', f"/automations/{auto['id']}/pause", {})
        # A failed individual pause must never prevent the last-owner shutdown.
        with contextlib.suppress(ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            await self.call('POST', '/shutdown', {})
        if self.process:
            process, self.process = self.process, None
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 5)
            except asyncio.TimeoutError:
                process.terminate()
                await asyncio.to_thread(process.wait)


def historical():
    import classified_data
    saved = classified_data.manifest()
    # The archive used company-sentiment-choice-v1; the ZIP's live template uses
    # target-sentiment-v1. Do not relabel old results as outputs of the new template.
    config = {key: saved[key] for key in ('schema_version', 'configuration_key', 'policy', 'taxonomy', 'schemas', 'semantics')}
    return dict(source='twitter_archive', supported_topic='AI company sentiment', configuration=config,
                configuration_key=saved['configuration_key'],
                taxonomy=saved['taxonomy'], window=saved['window'], counts=saved['counts'],
                note='Saved classifications only. Other topics require Live data.')


def setup(app):
    manager = Manager()
    app['automation_manager'] = manager

    async def lifecycle(app):
        async def monitor():
            while True:
                await asyncio.sleep(2)
                try:
                    await manager.reap()
                except (OSError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    # The child's independent idle watchdog is the second shutdown path.
                    pass
        task = asyncio.create_task(monitor())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await manager.shutdown()

    async def route(request):
        try:
            if request.path.endswith('/historical'):
                return web.json_response(historical())
            if request.path.endswith('/start') and 'id' not in request.match_info:
                payload = await bridge.body_of(request)
                directory = bridge.SESSIONS / identifier(payload.get('session_id'))
                return web.json_response(await manager.launch(directory, payload.get('proposal_hash'), payload.get('max_usd')))
            auto = identifier(request.match_info['id'])
            if request.method == 'GET':
                return web.json_response(await manager.watch(auto, identifier(request.query.get('viewer')), int(request.query.get('after', '0'))))
            payload = await bridge.body_of(request)
            action = request.match_info['action']
            if action == 'release':
                await manager.release(auto, identifier(payload.get('viewer')))
                return web.json_response({'released': True})
            return web.json_response(await manager.control(auto, action, payload.get('max_usd')))
        except (ValueError, TypeError, KeyError) as error:
            return bridge.fail(400, str(error))
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
            return bridge.fail(503, 'The live automation service is unavailable. Tracking will stop if its connection is lost.')

    app.cleanup_ctx.append(lifecycle)
    app.add_routes([web.get('/api/automations/historical', route), web.post('/api/automations/start', route),
                    web.get('/api/automations/{id}/events', route), web.post('/api/automations/{id}/{action}', route)])
