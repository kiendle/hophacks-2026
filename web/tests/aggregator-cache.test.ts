import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ActivityAccumulator } from '../src/data/activity.ts'
import { Aggregator } from '../src/data/aggregator.ts'
import { BUCKET_MS } from '../src/data/config.ts'
import type { Grade, ReplayEvent } from '../src/data/replayTypes.ts'

const companies = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }, { id: 'c', name: 'C' }]
const grade = (score: number | null, company = 'a'): Grade => ({ company, score,
  choice: score === null ? 'insufficient_evidence' : 'neutral', confidence: 0.8,
  probabilities: { positive: 0.2, negative: 0.2, neutral: 0.2, mixed: 0.2, insufficient_evidence: 0.2 } })
const post = (id: string, t: number, score: number | null = 5): ReplayEvent => ({ id, kind: 'post', t,
  postId: id, postTime: t, text: id, authorId: 'author', contentVersion: id, observedAt: t, grades: [grade(score)] })
const like = (id: string, t: number, postId: string, delta: number, opening = false,
  score: number | null = 5): ReplayEvent => ({ ...post(id, t, score), kind: 'like', postId,
  postTime: null, delta, opening })

// A cold snapshot rebuilds every post contribution. Comparing with it exactly
// catches changes in summation order as well as stale incremental state.
function cold(events: ReplayEvent[], now: number) {
  const aggregator = new Aggregator(companies)
  aggregator.addBatch(events)
  return aggregator.snapshot(now)
}

function fixture() {
  return Array.from({ length: 900 }, (_, i) => {
    const t = Math.floor(i / 150) * BUCKET_MS + Math.floor((i % 150) / 3) + 10
    const score = i % 13 === 0 ? null : i % 11
    const event = i % 4 === 0 ? post(`event-${i}`, t, score) :
      like(`event-${i}`, t, `event-${Math.floor(i / 4) * 4}`, i % 7 === 0 ? -20 : i % 31,
        i % 4 === 1, score)
    event.grades = [grade(score, companies[i % companies.length].id)]
    if (i % 5 === 0) event.grades.push(grade(10 - (score ?? 5), companies[(i + 1) % companies.length].id))
    return event
  })
}

test('incremental snapshots exactly match rebuilding for split batches, ties, duplicates, and signed likes', () => {
  const events = fixture()
  for (const batchSize of [1, 17, 151]) {
    const aggregator = new Aggregator(companies)
    const received: ReplayEvent[] = []
    for (let i = 0; i < events.length; i += batchSize) {
      const batch = events.slice(i, i + batchSize)
      received.push(...batch)
      aggregator.addBatch(batch)
      aggregator.add(batch[0])
      const now = batch.at(-1)!.t
      assert.deepEqual(aggregator.snapshot(now), cold(received, now))
      assert.deepEqual(aggregator.snapshot(now + 0.5), cold(received, now + 0.5))
    }
    assert.equal(aggregator.posts, events.filter(event => event.kind === 'post').length)
    assert.equal(aggregator.likes, events.filter(event => event.kind === 'like').length)
  }
})

test('historical rewinds and subsequent forward snapshots match fresh snapshots exactly', () => {
  const events = fixture()
  const aggregator = new Aggregator(companies)
  aggregator.addBatch(events)
  for (const now of [6 * BUCKET_MS, 10, 25, 25, 15, 59, BUCKET_MS + 30,
    4 * BUCKET_MS + 59, 2 * BUCKET_MS + 20, 6 * BUCKET_MS, 3 * BUCKET_MS + 10]) {
    assert.deepEqual(aggregator.snapshot(now), cold(events, now))
  }
})

test('a timestamp tie invalidates its partial snapshot while prior popup values remain frozen', () => {
  const aggregator = new Aggregator(companies)
  aggregator.add(post('p', 100, 2))
  const initial = aggregator.snapshot(100)[0].buckets[0]
  assert.strictEqual(aggregator.snapshot(100)[0].buckets[0], initial)
  aggregator.add(like('opening', 100, 'p', 50, true, 2))
  const opened = aggregator.snapshot(100)[0].buckets[0]
  assert.notStrictEqual(opened, initial)
  assert.equal(initial.weight, 1)
  assert.equal(initial.topPost.likes, 0)
  assert.equal(initial.topPost.likesKnown, false)
  assert.equal(opened.topPost.likes, 50)
  assert.equal(opened.topPost.periodLikes, 50)
  aggregator.add(like('repeat-opening', 100, 'p', 80, true, 2))
  const repeated = aggregator.snapshot(100)[0].buckets[0]
  assert.equal(repeated.weight, opened.weight)
  assert.equal(repeated.traction, 0)
  assert.equal(repeated.topPost.periodLikes, 50)
  assert.equal(repeated.topPost.likes, 80)
  assert.equal(opened.topPost.likes, 50)
})

test('partial popup balance changes invalidate cached output even if only another company is graded', () => {
  const aggregator = new Aggregator(companies)
  aggregator.add(post('shared', 100))
  const before = aggregator.snapshot(200)[0].buckets[0]
  const opening = like('other-company', 150, 'shared', 30, true)
  opening.grades = [grade(8, 'b')]
  aggregator.add(opening)
  const after = aggregator.snapshot(200)[0].buckets[0]
  assert.notStrictEqual(after, before)
  assert.equal(after.weight, before.weight)
  assert.equal(after.topPost.likes, 30)
  assert.equal(after.topPost.likesKnown, true)
  assert.equal(before.topPost.likes, 0)
  assert.equal(aggregator.snapshot(120)[0].buckets[0].topPost.likes, 0)
  assert.equal(aggregator.snapshot(200)[0].buckets[0].topPost.likes, 30)
})

test('closed buckets and gaps retain identity until an earlier arrival revises the corresponding bucket', () => {
  const aggregator = new Aggregator(companies)
  const events = [post('first', 100, 2), post('later', 3 * BUCKET_MS + 100, 8)]
  aggregator.addBatch(events)
  const before = aggregator.snapshot(4 * BUCKET_MS)[0].buckets
  const again = aggregator.snapshot(5 * BUCKET_MS)[0].buckets
  for (let i = 0; i < before.length; i++) assert.strictEqual(again[i], before[i])
  const revision = post('revision', 200, null)
  const gapArrival = post('gap-arrival', BUCKET_MS + 100, 4)
  aggregator.addBatch([revision, gapArrival])
  const after = aggregator.snapshot(4 * BUCKET_MS)[0].buckets
  assert.notStrictEqual(after[0], before[0])
  assert.notStrictEqual(after[1], before[1])
  assert.strictEqual(after[2], before[2])
  assert.strictEqual(after[3], before[3])
  assert.deepEqual(aggregator.snapshot(4 * BUCKET_MS), cold([...events, revision, gapArrival], 4 * BUCKET_MS))
  assert.equal(before[0].volume, 1)
  assert.equal(before[1].volume, 0)
})

test('a rewind after an earlier timestamp revision discards contributions beyond its cutoff', () => {
  const aggregator = new Aggregator(companies)
  const events = [post('first', 100, 2), post('second', 300, 8), post('revision', 200, 4)]
  aggregator.addBatch(events.slice(0, 2))
  aggregator.snapshot(400)
  aggregator.add(events[2])
  aggregator.snapshot(400)
  assert.deepEqual(aggregator.snapshot(250), cold(events, 250))
  assert.deepEqual(aggregator.snapshot(400), cold(events, 400))
})

test('forward ticks group each new activity only once and unchanged ticks reuse calculated results', () => {
  const originalAdd = ActivityAccumulator.prototype.add
  const originalResult = ActivityAccumulator.prototype.result
  let grouped = 0, calculated = 0
  ActivityAccumulator.prototype.add = function (activity) { grouped++; return originalAdd.call(this, activity) }
  ActivityAccumulator.prototype.result = function () { calculated++; return originalResult.call(this) }
  try {
    const aggregator = new Aggregator(companies)
    for (let i = 0; i < 100; i++) {
      aggregator.add(post(`post-${i}`, i * 10))
      aggregator.snapshot(i * 10)
      aggregator.snapshot(i * 10)
      aggregator.snapshot(i * 10 + 1)
    }
    assert.equal(grouped, 100)
    assert.equal(calculated, 100)
    aggregator.snapshot(BUCKET_MS)
    aggregator.snapshot(2 * BUCKET_MS)
    assert.equal(grouped, 100)
    assert.equal(calculated, 100)
  } finally {
    ActivityAccumulator.prototype.add = originalAdd
    ActivityAccumulator.prototype.result = originalResult
  }
})
