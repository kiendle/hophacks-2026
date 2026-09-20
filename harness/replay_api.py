"""Serve the UI branch's replay protocol from the existing Python app."""
import asyncio
import json
import math
import time
import uuid

from aiohttp import web, WSMsgType
import classified_data

MAX_BATCH_EVENTS = 1000


def event_batches(events, now, status):
    """Keep replay frames small even when a fast clock reaches many events."""
    for offset in range(0, max(1, len(events)), MAX_BATCH_EVENTS):
        batch = events[offset:offset + MAX_BATCH_EVENTS]
        final = offset + MAX_BATCH_EVENTS >= len(events)
        yield {
            'events': batch,
            'now': now if final else batch[-1]['t'],
            'status': 'playing' if status == 'complete' and not final else status,
        }


class ReplayClock:
    """Same virtual clock contract as web/server/replayClock.ts; wall time is ms."""
    def __init__(self, data, speed, wall):
        self.data, self.speed, self.last_wall = data, speed, wall
        self.now, self.cursor, self.status = data['start'], 0, 'playing'

    def tick(self, wall):
        if self.status == 'playing':
            self.now = min(self.data['end'], self.now + max(0, wall - self.last_wall) * self.speed)
        self.last_wall = wall
        start = self.cursor
        events = self.data['events']
        while self.cursor < len(events) and events[self.cursor]['t'] <= self.now:
            self.cursor += 1
        if self.now >= self.data['end']:
            self.status = 'complete'
        return events[start:self.cursor]

    def pause(self):
        if self.status == 'playing':
            self.status = 'paused'

    def resume(self, wall):
        if self.status != 'complete':
            self.status = 'playing'
        self.last_wall = wall


async def replay(request):
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8192)
    await ws.prepare(request)
    try:
        words = json.loads(request.query.get('terms', '["AI"]'))
        if not isinstance(words, list) or not 1 <= len(words) <= 20 or any(not isinstance(w, str) or not 1 <= len(w) <= 80 for w in words):
            raise ValueError('Invalid keywords')
        result = await asyncio.to_thread(classified_data.scan, words, [])
        data = result['dataset']
    except (OSError, ValueError, KeyError):
        await ws.send_json({'type': 'error', 'message': 'Saved data could not be loaded. Check the classified export and keywords.'})
        await ws.close()
        return ws
    wall = lambda: time.monotonic() * 1000
    clock = None
    run, sequence = '', 0

    async def restart(speed=14400):
        nonlocal clock, run, sequence
        run, sequence = str(uuid.uuid4()), 0
        clock = ReplayClock(data, speed, wall())
        await ws.send_json({'type': 'init', 'run': run, 'start': data['start'], 'end': data['end'],
                            'companies': data['companies'], 'speed': speed})

    async def flush():
        nonlocal sequence
        events = clock.tick(wall())
        for batch in event_batches(events, clock.now, clock.status):
            await ws.send_json({'type': 'batch', 'run': run, 'sequence': sequence,
                                **batch, 'speed': clock.speed})
            sequence += 1

    try:
        await restart()
        await flush()
        while not ws.closed:
            try:
                message = await ws.receive(timeout=.1)
            except asyncio.TimeoutError:
                if clock.status == 'playing':
                    await flush()
                continue
            if message.type != WSMsgType.TEXT:
                break
            try:
                command = json.loads(message.data)
                kind = command.get('type')
                if kind == 'restart':
                    await restart(clock.speed)
                elif kind == 'pause':
                    await flush()
                    clock.pause()
                elif kind == 'resume':
                    clock.resume(wall())
                elif kind == 'speed':
                    speed = command.get('speed')
                    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 1 <= speed <= 1_000_000:
                        raise ValueError('Invalid speed')
                    await flush()
                    clock.speed = speed
                else:
                    raise ValueError('Unknown command')
                await flush()
            except (ValueError, AttributeError):
                await ws.send_json({'type': 'error', 'message': 'Invalid replay command.'})
    except (ConnectionError, RuntimeError):
        pass  # Browser closed while a batch was being sent.
    finally:
        await ws.close()
    return ws


async def warm(app):
    """Decode the saved export while the server starts. It takes most of half a minute, and
    without this the first person to open the page waits for it in front of an empty chart."""
    if classified_data.available():
        task = app['replay_warm'] = asyncio.create_task(asyncio.to_thread(classified_data.scan, ['AI'], []))
        task.add_done_callback(lambda done: done.cancelled() or done.exception())  # the page reports a bad export itself


def setup(app):
    app.router.add_get('/api/replay', replay)
