import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { gunzipSync } from 'node:zlib'
import { test } from 'node:test'
import { Aggregator } from '../src/data/aggregator.ts'
import { windowStat } from '../src/data/window.ts'
import { ReplayClock } from '../server/replayClock.ts'
import { BUCKET_MS } from '../src/data/config.ts'
import { engagementWeight } from '../src/data/sentiment.ts'
import type { Grade, ReplayDataset, ReplayEvent } from '../src/data/replayTypes.ts'

const grade = (score: number | null, company = 'a'): Grade => ({ company, score,
  choice: score === null ? 'insufficient_evidence' : score > 5 ? 'positive' : 'negative', confidence: 0.8,
  probabilities: { positive: 0.4, negative: 0.3, neutral: 0.2, mixed: 0.1, insufficient_evidence: 0 } })
const post = (id: string, t: number, score: number | null): ReplayEvent => ({ id, kind: 'post', t,
  postId: id, postTime: t, text: id, authorId: '123', contentVersion: id, observedAt: t + 100, grades: [grade(score)] })
const like = (id: string, t: number, postId: string, delta: number, opening = false): ReplayEvent => ({
  ...post(id, t, 0), kind: 'like', postId, delta, opening })
const companies = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }]
const near = (actual: number, expected: number) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`)

test('log weights are bounded below by one and retain the existing engagement curve', () => {
  for (const value of [undefined, NaN, Infinity, -5, 0]) assert.equal(engagementWeight(value), 1)
  near(engagementWeight(999), 1 + Math.log(1000))
})

test('likes reweight the original publication bucket and spread without changing post counts', () => {
  const agg = new Aggregator(companies)
  const opening = like('opening', BUCKET_MS + 100, 'negative', 99, true)
  // The like event's grade is deliberately different: use the original post's score.
  opening.grades = [grade(10)]
  agg.addBatch([post('negative', 100, 0), post('positive', 200, 10), post('uncertain', 300, null),
    opening, opening, like('uncertain-likes', BUCKET_MS + 101, 'uncertain', 1e6, true),
    like('decrease', BUCKET_MS + 200, 'negative', -90),
    like('orphan', BUCKET_MS + 300, 'missing', 1e7, true)])
  const before = agg.snapshot(BUCKET_MS)[0].buckets[0]
  assert.equal(before.sentiment, 5)
  const atOpening = agg.snapshot(BUCKET_MS + 100)[0].buckets[0]
  const negativeWeight = 1 + Math.log(100)
  const mean = 10 / (negativeWeight + 1)
  near(atOpening.sentiment, mean)
  near(atOpening.weight, negativeWeight + 1)
  assert.equal(atOpening.volume, 3)
  assert.equal(atOpening.scored, 2)
  assert.equal(atOpening.topPost.id, 'negative')
  assert.equal(atOpening.topPost.sentiment, 0)
  // A snapshot cannot see later engagement even when asked for a wider window.
  const stat = windowStat([atOpening], 0, BUCKET_MS * 2)
  near(stat.sentiment, mean)
  near(stat.spread, Math.sqrt(100 / (negativeWeight + 1) - mean ** 2))
  assert.equal(stat.volume, 3)
  assert.equal(stat.scored, 2)
  const after = agg.snapshot(BUCKET_MS + 400)[0].buckets
  near(after[0].sentiment, 10 / (2 + Math.log(10)))
  // Updates to an older post must not contribute when the publication is outside the window.
  const partial = windowStat(after, 200, BUCKET_MS * 2)
  assert.equal(partial.sentiment, 10)
  assert.equal(partial.volume, 2)
  assert.equal(partial.scored, 1)
  assert.equal(partial.spread, 0)
  assert.equal(agg.posts, 3)
  assert.equal(agg.likes, 4)
})

test('historical trails and exact boundaries exclude future engagement and future posts', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('negative', 100, 0), post('positive', 200, 10),
    like('opening', 300, 'negative', 99, true), post('later', 400, 10)])
  const latest = agg.snapshot(500)[0].buckets
  assert.equal(windowStat(latest, 0, 300).sentiment, 5)
  near(windowStat(latest, 0, 301).sentiment, 10 / (2 + Math.log(100)))
  const old = agg.snapshot(200)[0].buckets
  assert.equal(windowStat(old, 0, 1000).sentiment, 5)
  assert.equal(windowStat(old, 0, 1000).volume, 2)
  assert.equal(windowStat(latest, 201, 400).volume, 0)
})

test('unknown balances stay baseline until opening; negative balances remain finite', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('negative', 100, 0), post('positive', 200, 10),
    like('unbased', 300, 'negative', 10000), like('opening', 400, 'negative', 9, true),
    like('over-decrease', 500, 'negative', -20)])
  assert.equal(agg.snapshot(350)[0].buckets[0].sentiment, 5)
  assert.equal(agg.snapshot(350)[0].buckets[0].topPost.likesKnown, false)
  near(agg.snapshot(450)[0].buckets[0].sentiment, 10 / (2 + Math.log(10)))
  assert.equal(agg.snapshot(550)[0].buckets[0].sentiment, 5)
})

test('partial buckets and spread use exact arrived posts; uncertain posts still count', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('first', 100, 0), post('unknown', 200, null), post('last', 300, 10)])
  const buckets = agg.snapshot(400)[0].buckets
  assert.deepEqual(windowStat(buckets, 0, 250), { volume: 2, traction: 0, sentiment: 0, spread: 0, scored: 1 })
  assert.equal(windowStat(buckets, 0, 400).spread, 5)
  assert.equal(windowStat(buckets, 100.5, 300).volume, 1)
  assert.equal(windowStat(buckets, 0, 300).volume, 2)
})

test('multi-company delivery counts one source post and duplicate batches are idempotent', () => {
  const agg = new Aggregator(companies)
  const event = post('both', 100, 0)
  event.grades.push(grade(10, 'b'))
  agg.addBatch([event, event])
  assert.equal(agg.posts, 1)
  assert.deepEqual(agg.snapshot(200).map((s) => s.buckets[0].sentiment), [0, 10])
})

test('a single opinion keeps its score after like updates; engagement counters exclude openings', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('p', 100, 8), like('opening', 200, 'p', 100, true), like('up', 300, 'p', 10), like('down', 400, 'p', -3), like('orphan', 500, 'missing', 5)])
  assert.equal(agg.snapshot(150)[0].buckets[0].topPost.likesKnown, false)
  assert.equal(agg.snapshot(250)[0].buckets[0].topPost.likes, 100)
  assert.equal(agg.snapshot(450)[0].buckets[0].topPost.likes, 107)
  const stat = windowStat(agg.snapshot(600)[0].buckets, 0, 600)
  assert.equal(stat.sentiment, 8)
  assert.equal(stat.spread, 0)
  assert.equal(stat.traction, 12)
  assert.equal(agg.posts, 1)
  assert.equal(agg.likes, 4)
})

test('clock pauses, resumes without catching up, preserves ties and completes', () => {
  const data: ReplayDataset = { version: 1, start: 0, end: 1000, companies, counts: { posts: 3, likes: 0 },
    events: [post('a', 100, 0), post('b', 100, 10), post('c', 900, 5)] }
  const clock = new ReplayClock(data, 10, 0)
  assert.deepEqual(clock.tick(10).map((e) => e.id), ['a', 'b'])
  clock.pause()
  assert.equal(clock.tick(10000).length, 0)
  assert.equal(clock.now, 100)
  clock.resume(10000)
  assert.equal(clock.tick(10010).length, 0)
  assert.equal(clock.now, 200)
  assert.deepEqual(clock.tick(10100).map((e) => e.id), ['c'])
  assert.equal(clock.now, 1000)
  assert.equal(clock.status, 'complete')
  assert.equal(clock.tick(99999).length, 0)
})

const dataset: ReplayDataset = JSON.parse(gunzipSync(readFileSync(new URL('../data/demo.json.gz', import.meta.url))).toString())

test('real package preserves independent counts and matches weighted August 21 reference', () => {
  const agg = new Aggregator(dataset.companies)
  agg.addBatch(dataset.events)
  assert.equal(agg.posts, 50377)
  assert.equal(agg.likes, 16510)
  const end = Date.parse('2026-08-21T00:00:00Z')
  const google = agg.snapshot(end - 1).find((s) => s.id === 'google')!
  const stat = windowStat(google.buckets, end - 86400000, end)
  assert.equal(stat.volume, 1897)
  assert.equal(stat.scored, 1885)
  near(stat.sentiment, 4.007476565422168)
  near(stat.spread, 3.8030760226393814)
})

test('real conflicting buckets match independently calculated log-weighted means', () => {
  const agg = new Aggregator(dataset.companies)
  agg.addBatch(dataset.events)
  const end = Date.parse('2026-08-21T00:00:00Z')
  const series = agg.snapshot(end - 1)
  for (const [company, start, expected] of [
    ['google', '2026-08-18T20:00:00Z', 6.268686471709232],
    ['anthropic', '2026-08-18T16:00:00Z', 4.964445077859396],
    ['anthropic', '2026-08-18T12:00:00Z', 5.619003546918587],
  ] as const) {
    const bucket = series.find(s => s.id === company)!.buckets.find(b => b.start === Date.parse(start))!
    near(bucket.sentiment, expected)
  }
})

// Deliberately independent of Aggregator, atOrBefore, windowStat, and the weighting helper.
function referenceWindow(company: string, start: number, end: number) {
  const balances = new Map<string, number>()
  const posts = new Map<string, { score: number | null }>()
  for (const event of dataset.events) {
    if (event.t >= end) break
    if (event.kind === 'post' && event.t >= start) {
      const g = event.grades.find(g => g.company === company)
      if (g) posts.set(event.postId, { score: g.score })
    } else if (event.kind === 'like') {
      if (event.opening) balances.set(event.postId, event.delta!)
      else if (balances.has(event.postId)) balances.set(event.postId, balances.get(event.postId)! + event.delta!)
    }
  }
  let W = 0, S = 0, Q = 0, scored = 0
  for (const [id, post] of posts) {
    if (post.score === null) continue
    scored++
    const w = 1 + Math.log(1 + Math.max(0, balances.get(id) ?? 0))
    W += w
    S += w * post.score
    Q += w * post.score ** 2
  }
  return { volume: posts.size, scored, sentiment: S / W, spread: Math.sqrt(Math.max(0, Q / W - (S / W) ** 2)) }
}

test('real partial windows and bubble trails match independent event-time calculation', () => {
  const agg = new Aggregator(dataset.companies)
  agg.addBatch(dataset.events)
  const now = Date.parse('2026-09-12T10:37:00Z')
  const series = agg.snapshot(now)
  for (const company of ['google', 'anthropic', 'openai']) {
    for (const lag of [0, 1, 6]) {
      const end = now - lag * BUCKET_MS
      const start = end - 86400000
      const actual = windowStat(series.find(s => s.id === company)!.buckets, start, end)
      const expected = referenceWindow(company, start, end)
      assert.equal(actual.volume, expected.volume)
      assert.equal(actual.scored, expected.scored)
      near(actual.sentiment, expected.sentiment)
      near(actual.spread, expected.spread)
    }
  }
})

test('batch sizes do not alter real-data results; past inspection matches a stopped replay', () => {
  const full = new Aggregator(dataset.companies)
  full.addBatch(dataset.events)
  const partial = new Aggregator(dataset.companies)
  const cutoff = Date.parse('2026-09-02T10:37:00Z')
  const received = dataset.events.filter((e) => e.t <= cutoff)
  for (let i = 0; i < received.length; i += 123) partial.addBatch(received.slice(i, i + 123))
  const summarize = (agg: Aggregator) => agg.snapshot(cutoff).map((s) => ({ id: s.id,
    stat: windowStat(s.buckets, cutoff - 86400000, cutoff),
    posts: s.buckets.filter((b) => b.volume).map((b) => ({ start: b.start, volume: b.volume, sentiment: b.sentiment, top: b.topPost })) }))
  assert.deepEqual(summarize(full), summarize(partial))
})
