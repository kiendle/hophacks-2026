import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { createStreamSource, REPLAY_PUBLISH_INTERVAL_MS } from '../src/data/streamSource.ts'
import type { ReplayEvent, ReplayMessage } from '../src/data/replayTypes.ts'
import type { StreamSnapshot } from '../src/data/source.ts'

class Socket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: Socket[] = []
  readyState = 1
  onmessage?: (event: { data: string }) => void
  onclose?: () => void
  onopen?: () => void
  sent: string[] = []
  constructor() { Socket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.() }
  emit(message: ReplayMessage) { this.onmessage?.({ data: JSON.stringify(message) }) }
}

function setup(t: TestContext) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const originals = ['WebSocket', 'location'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: Socket })
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { protocol: 'http:', host: 'localhost' } })
  const source = createStreamSource()
  const updates: StreamSnapshot[] = []
  const close = source.subscribe(snapshot => updates.push(snapshot))
  const socket = Socket.instances.at(-1)!
  t.after(() => {
    close()
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else Reflect.deleteProperty(globalThis, key)
    }
  })
  const init = (run = 'one') => socket.emit({ type: 'init', run, start: 0, end: 10_000,
    companies: [{ id: 'openai', name: 'OpenAI' }], speed: 14400 })
  const batch = (sequence: number, events: ReplayEvent[], fields = {}) => socket.emit({ type: 'batch',
    run: 'one', sequence, events, now: events.at(-1)?.t ?? 9000, speed: 14400, status: 'playing', ...fields })
  init()
  return { source, socket, updates, close, init, batch }
}

const post = (i: number): ReplayEvent => ({ id: String(i), postId: String(i), kind: 'post', t: i,
  postTime: i, text: String(i), authorId: null, contentVersion: String(i), observedAt: i,
  grades: [{ company: 'openai', choice: 'positive', score: 8, confidence: 1,
    probabilities: { positive: 1, negative: 0, mixed: 0, neutral: 0, insufficient_evidence: 0 } }] })

test('coalesces delivery bursts while retaining every event and exact historical data', t => {
  const { updates, batch, source } = setup(t)
  const events = Array.from({ length: 2500 }, (_, i) => post(i + 1))
  for (let i = 0; i < 5; i++) batch(i, events.slice(i * 500, (i + 1) * 500))
  assert.equal(updates.length, 3, 'Loading, init and the first data frame appear immediately')
  assert.equal(updates.at(-1)!.read, 500, 'The chart can draw the first points without a redraw delay')
  t.mock.timers.tick(REPLAY_PUBLISH_INTERVAL_MS)
  assert.equal(updates.length, 4)
  assert.equal(updates.at(-1)!.read, events.length)
  assert.equal(updates.at(-1)!.now, 2500)
  assert.deepEqual(updates.at(-1)!.series[0].buckets[0].activity!.map(a => a.event.id), events.map(e => e.id))
  assert.equal(source.snapshotAt!(1250)[0].buckets[0].volume, 1250)
})

test('pause, resume, speed and completion publish immediately including pending events', t => {
  const { updates, batch } = setup(t)
  batch(0, [post(1)])
  batch(1, [], { status: 'paused', now: 2 })
  assert.equal(updates.at(-1)!.status, 'paused')
  assert.equal(updates.at(-1)!.read, 1)
  batch(2, [], { speed: 43200, status: 'paused', now: 2 })
  assert.equal(updates.at(-1)!.speed, 43200)
  batch(3, [], { speed: 43200, now: 2 })
  assert.equal(updates.at(-1)!.status, 'playing')
  batch(4, [post(3)], { speed: 43200 })
  batch(5, [], { status: 'complete', speed: 43200, now: 10000 })
  assert.equal(updates.at(-1)!.read, 2)
  assert.equal(updates.at(-1)!.status, 'complete')
  const publications = updates.length
  t.mock.timers.tick(1000)
  assert.equal(updates.length, publications)
})

test('restart and unsubscribe cancel pending redraws and ignore old-run frames', t => {
  const { updates, batch, init, close } = setup(t)
  batch(0, [post(1)])
  batch(1, [post(2)])
  init('two')
  batch(2, [post(3)])
  t.mock.timers.tick(1000)
  assert.equal(updates.at(-1)!.run, 'two')
  assert.equal(updates.at(-1)!.read, 0)
  batch(0, [post(4)], { run: 'two' })
  batch(1, [post(5)], { run: 'two' })
  close()
  const publications = updates.length
  t.mock.timers.tick(1000)
  assert.equal(updates.length, publications)
})

test('a sequence gap reports an error instead of presenting incomplete data as complete', t => {
  const { updates, batch, socket } = setup(t)
  batch(0, [post(1)])
  batch(2, [post(3)], { status: 'complete' })
  assert.equal(updates.at(-1)!.status, 'error')
  assert.equal(updates.at(-1)!.read, 1)
  assert.equal(socket.readyState, 3)
  assert.match(updates.at(-1)!.error!, /interrupted/i)
})
