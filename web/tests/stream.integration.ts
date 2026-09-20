import assert from 'node:assert/strict'
import { WebSocket } from 'ws'
import type { ReplayDataset, ReplayMessage } from '../src/data/replayTypes.ts'
import { loadReplayDataset } from '../server/loadReplayDataset.ts'

let dataset: ReplayDataset | null = await loadReplayDataset(new URL('../data/demo.json.gz', import.meta.url))
const expectedEvents = new Set(dataset.events.map(e => e.id))
const expectedCounts = dataset.counts
dataset = null // Keep only the expected IDs while the server streams this large package.
const ws = new WebSocket(process.argv[2] ?? 'ws://localhost:5173/api/replay')
const seen = new Set<string>()
let posts = 0
let likes = 0
let last = -1
let run = ''
let phase = 'start'
let pausedAt = 0
let done = false
const timeoutMs = Number(process.env.REPLAY_TEST_TIMEOUT_MS ?? 120_000)
assert.ok(Number.isFinite(timeoutMs) && timeoutMs > 0, 'REPLAY_TEST_TIMEOUT_MS must be positive')
const timeout = setTimeout(() => { console.error('Replay integration timed out'); process.exitCode = 1; ws.close() }, timeoutMs)
ws.on('error', (error) => { clearTimeout(timeout); throw error })
ws.on('message', (raw) => {
  if (done) return
  const message = JSON.parse(raw.toString()) as ReplayMessage
  if (message.type === 'error') throw new Error(message.message)
  if (message.type === 'init') {
    if (run) {
      assert.notEqual(message.run, run)
      done = true
      clearTimeout(timeout)
      ws.close()
      console.log({ posts, likes, uniqueEvents: seen.size, pauseResume: true, complete: true, restart: true })
    } else { run = message.run; last = -1 }
    return
  }
  assert.equal(message.run, run)
  assert.equal(message.sequence, ++last)
  for (const event of message.events) {
    assert.ok(event.t <= message.now)
    assert.ok(!seen.has(event.id))
    assert.ok(expectedEvents.has(event.id), `Unexpected event ${event.id}`)
    seen.add(event.id)
    if (event.kind === 'post') posts++
    else likes++
  }
  if (phase === 'start') {
    phase = 'pausing'
    ws.send(JSON.stringify({ type: 'pause' }))
  } else if (phase === 'pausing' && message.status === 'paused') {
    pausedAt = message.now
    phase = 'speed'
    ws.send(JSON.stringify({ type: 'speed', speed: 1_000_000 }))
  } else if (phase === 'speed' && message.speed === 1_000_000) {
    assert.equal(message.now, pausedAt)
    phase = 'running'
    setTimeout(() => ws.send(JSON.stringify({ type: 'resume' })), 200)
  }
  if (message.status === 'complete') {
    assert.equal(posts, expectedCounts.posts)
    assert.equal(likes, expectedCounts.likes)
    assert.equal(seen.size, expectedEvents.size)
    phase = 'restarting'
    ws.send(JSON.stringify({ type: 'restart' }))
  }
})
