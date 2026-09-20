import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MAX_BATCH_EVENTS, splitReplayBatch } from '../server/replayBatches.ts'
import { ReplayClock } from '../server/replayClock.ts'
import type { ReplayDataset, ReplayEvent, ReplayMessage } from '../src/data/replayTypes.ts'

type Batch = Extract<ReplayMessage, { type: 'batch' }>
const events: ReplayEvent[] = Array.from({ length: 2_501 }, (_, i) => ({
  id: String(i), postId: String(i), kind: 'post', t: i + 1, postTime: i + 1,
  text: 'Saved post', authorId: 'a', contentVersion: 'v', observedAt: i + 1, grades: [],
}))
const fixture: ReplayDataset = { version: 1, start: 0, end: 4_000, companies: [],
  counts: { posts: events.length, likes: 0 }, events }
const message: Batch = { type: 'batch', run: 'run-a', sequence: 7, now: fixture.end,
  events, status: 'complete', speed: 1_000_000 }

test('large ticks deliver every event once with bounded frames and completion only on the final frame', () => {
  const batches = [...splitReplayBatch(message)]
  assert.deepEqual(batches.map(batch => batch.events.length), [1_000, 1_000, 501])
  assert.deepEqual(batches.map(batch => batch.sequence), [7, 8, 9])
  assert.deepEqual(batches.map(batch => batch.now), [1_000, 2_000, 4_000])
  assert.deepEqual(batches.map(batch => batch.status), ['playing', 'playing', 'complete'])
  const delivered = batches.flatMap(batch => batch.events)
  assert.deepEqual(delivered, events)
  assert.equal(new Set(delivered.map(event => event.id)).size, events.length)
  for (const batch of batches) {
    assert.ok(batch.events.length <= MAX_BATCH_EVENTS)
    assert.ok(batch.events.every(event => event.t <= batch.now))
    assert.equal(batch.run, message.run)
    assert.equal(batch.speed, message.speed)
  }
})

test('empty flushes preserve time/status and exact multiples have no extra frame', () => {
  for (const status of ['playing', 'paused', 'complete'] as const) {
    const empty = { ...message, events: [], status }
    assert.deepEqual([...splitReplayBatch(empty)], [empty])
  }
  const batches = [...splitReplayBatch({ ...message, events: events.slice(0, 2_000) })]
  assert.equal(batches.length, 2)
  assert.equal(batches[1].status, 'complete')
  assert.equal(batches[1].now, fixture.end)
})

test('timestamp ties stay ordered while visible replay time remains monotonic', () => {
  const tied = events.map(event => ({ ...event, t: Math.floor(event.t / 1_100) }))
  const batches = [...splitReplayBatch({ ...message, events: tied, now: 3 })]
  assert.deepEqual(batches.flatMap(batch => batch.events.map(event => event.id)), events.map(event => event.id))
  assert.deepEqual(batches.map(batch => batch.now), [0, 1, 3])
})

test('bounded flushes preserve pause/resume and a restarted run resets sequence', () => {
  let clock = new ReplayClock(fixture, 1, 0)
  let sequence = 0
  let run = 'first'
  const flush = (wall: number) => {
    const reached = clock.tick(wall)
    const batches = [...splitReplayBatch({ type: 'batch', run, sequence, now: clock.now,
      events: reached, status: clock.status, speed: clock.speed })]
    sequence += batches.length
    return batches
  }
  const first = flush(1_200)
  clock.pause()
  const paused = flush(5_000)
  assert.deepEqual(paused.map(batch => [batch.sequence, batch.now, batch.status, batch.events.length]),
    [[2, 1_200, 'paused', 0]])
  clock.resume(5_000)
  const remaining = flush(7_800)
  const all = [...first, ...paused, ...remaining]
  assert.deepEqual(all.map(batch => batch.sequence), [0, 1, 2, 3, 4])
  assert.deepEqual(all.flatMap(batch => batch.events), events)
  assert.equal(all.filter(batch => batch.status === 'complete').length, 1)
  assert.equal(all.at(-1)!.status, 'complete')
  for (let i = 1; i < all.length; i++) assert.ok(all[i].now >= all[i - 1].now)

  clock = new ReplayClock(fixture, 1, 0)
  sequence = 0
  run = 'restarted'
  const restarted = flush(4_000)
  assert.equal(restarted[0].sequence, 0)
  assert.ok(restarted.every(batch => batch.run === run))
  assert.deepEqual(restarted.flatMap(batch => batch.events), events)
})
