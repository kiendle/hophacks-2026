import assert from 'node:assert/strict'
import { WebSocket } from 'ws'
import type { ReplayMessage } from '../src/data/replayTypes.ts'

const ws = new WebSocket(process.argv[2] ?? 'ws://localhost:5173/api/replay')
const seen = new Set<string>()
let posts = 0
let likes = 0
let last = -1
let run = ''
let phase = 'start'
let pausedAt = 0
let done = false
const timeout = setTimeout(() => { console.error('Replay integration timed out'); process.exitCode = 1; ws.close() }, 15000)
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
    assert.equal(posts, 50377)
    assert.equal(likes, 16510)
    assert.equal(seen.size, 66887)
    phase = 'restarting'
    ws.send(JSON.stringify({ type: 'restart' }))
  }
})
