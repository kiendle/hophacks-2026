import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { test } from 'node:test'
import { Aggregator } from '../src/data/aggregator.ts'
import { windowStat } from '../src/data/window.ts'
import { ReplayClock } from '../server/replayClock.ts'
import { loadReplayDataset } from '../server/loadReplayDataset.ts'
import { BUCKET_MS } from '../src/data/config.ts'
import { engagementWeight } from '../src/data/sentiment.ts'
import type { Grade, ReplayDataset, ReplayEvent } from '../src/data/replayTypes.ts'

const grade = (score: number | null, company = 'a'): Grade => ({ company, score,
  choice: score === null ? 'insufficient_evidence' : score > 5 ? 'positive' : 'negative', confidence: 0.8,
  probabilities: { positive: 0.4, negative: 0.3, neutral: 0.2, mixed: 0.1, insufficient_evidence: 0 } })
const post = (id: string, t: number, score: number | null): ReplayEvent => ({ id, kind: 'post', t,
  postId: id, postTime: t, text: id, authorId: '123', contentVersion: id, observedAt: t + 100, grades: [grade(score)] })
const like = (id: string, t: number, postId: string, delta: number, opening = false, score: number | null = 0): ReplayEvent => ({
  ...post(id, t, score), kind: 'like', postId, postTime: 100, delta, opening })
const companies = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }]
const near = (actual: number, expected: number) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`)

test('log weights are bounded below by one', () => {
  for (const value of [undefined, NaN, Infinity, -5, 0]) assert.equal(engagementWeight(value), 1)
  near(engagementWeight(999), 1 + Math.log(1000))
})

test('initial likes weight arrival, later likes affect only their own bucket, and popup uses period likes', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('old', 100, 2), like('old-opening', 200, 'old', 50000, true, 2)])
  const frozen = structuredClone(agg.snapshot(BUCKET_MS)[0].buckets[0])
  const newer = post('new', BUCKET_MS + 100, 8)
  agg.addBatch([newer, like('new-opening', BUCKET_MS + 200, 'new', 500, true, 8),
    like('new-gain', BUCKET_MS + 300, 'new', 20, false, 8),
    like('old-gain', BUCKET_MS + 400, 'old', 700, false, 2)])
  const buckets = agg.snapshot(2 * BUCKET_MS)[0].buckets
  assert.deepEqual(buckets[0], frozen)
  const current = buckets[1]
  const oldWeight = Math.log(701), newWeight = 1 + Math.log(521)
  const mean = (oldWeight * 2 + newWeight * 8) / (oldWeight + newWeight)
  near(current.sentiment, mean)
  assert.equal(current.volume, 1)
  assert.equal(current.activePosts, 2)
  assert.equal(current.scored, 2)
  assert.equal(current.topPost.id, 'old')
  assert.equal(current.topPost.periodLikes, 700)
  assert.equal(current.topPost.likes, 50700)
  assert.equal(current.topPost.time, 100)
  assert.equal(current.traction, 720)
  const stat = windowStat(buckets, BUCKET_MS, 2 * BUCKET_MS)
  near(stat.sentiment, mean)
  near(stat.spread, Math.sqrt((oldWeight * 4 + newWeight * 64) / (oldWeight + newWeight) - mean ** 2))
  assert.equal(stat.activePosts, 2)
  // Lifetime popularity alone does not influence the next period.
  agg.add(like('old-small-gain', 2 * BUCKET_MS + 100, 'old', 10, false, 2))
  agg.add(like('new-larger-gain', 2 * BUCKET_MS + 200, 'new', 20, false, 8))
  assert.equal(agg.snapshot(3 * BUCKET_MS)[0].buckets[2].topPost.id, 'new')
})

test('new posts with initial likes can win the popup, and initial balances count only once', () => {
  const agg = new Aggregator(companies)
  const opening = like('opening', 200, 'p', 500, true, 8)
  agg.addBatch([post('p', 100, 8), opening, opening,
    like('older-gain', 300, 'older', 100, false, 2)])
  const first = agg.snapshot(BUCKET_MS)[0].buckets[0]
  assert.equal(first.topPost.id, 'p')
  assert.equal(first.topPost.periodLikes, 500)
  near(first.weight, 1 + Math.log(501) + Math.log(101))
  assert.equal(agg.likes, 2)
  agg.add(like('repeated-opening', BUCKET_MS + 100, 'p', 500, true, 8))
  const next = agg.snapshot(2 * BUCKET_MS)[0].buckets[1]
  assert.equal(next.activePosts, 0)
  assert.equal(next.weight, 0)
  assert.ok(Number.isNaN(next.sentiment))
})

test('late opening balances never backfill a closed publication bucket', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('negative', 100, 0), post('positive', 200, 10)])
  const original = structuredClone(agg.snapshot(BUCKET_MS)[0].buckets[0])
  agg.add(like('late-opening', BUCKET_MS + 100, 'negative', 1000, true, 0))
  const buckets = agg.snapshot(2 * BUCKET_MS)[0].buckets
  assert.deepEqual(buckets[0], original)
  assert.equal(buckets[0].sentiment, 5)
  assert.equal(buckets[1].sentiment, 0)
  near(buckets[1].weight, Math.log(1001))
  assert.equal(buckets[1].volume, 0)
  assert.equal(buckets[1].activePosts, 1)
})

test('signed changes reduce net influence without negative weights or inverted sentiment', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([like('gain', 100, 'old', 100, false, 2), like('loss', 200, 'old', -40, false, 2),
    post('new', 250, 8), like('unknown-score', 300, 'uncertain', 1000, true, null)])
  const bucket = agg.snapshot(350)[0].buckets[0]
  near(bucket.sentiment, (Math.log(61) * 2 + 8) / (Math.log(61) + 1))
  assert.equal(bucket.topPost.id, 'uncertain')
  assert.ok(Number.isNaN(bucket.topPost.sentiment))
  assert.equal(bucket.activePosts, 3)
  assert.equal(bucket.scored, 2)
  assert.equal(bucket.traction, 60)
  agg.add(like('more-loss', 400, 'old', -70, false, 2))
  const after = agg.snapshot(500)[0].buckets[0]
  assert.equal(after.sentiment, 8)
  assert.equal(after.activePosts, 2)
  assert.equal(after.scored, 1)
  assert.equal(after.traction, -10)
})

test('split updates have the same effect; rolling windows regroup across bucket boundaries', () => {
  const make = (split: boolean) => {
    const agg = new Aggregator(companies)
    agg.add(post('p', 100, 0))
    if (split) agg.addBatch([like('one', 200, 'p', 10), like('two', 300, 'p', 20)])
    else agg.add(like('combined', 300, 'p', 30))
    agg.add(post('q', 400, 10))
    return agg
  }
  const a = make(true), b = make(false)
  const summarize = (agg: Aggregator) => windowStat(agg.snapshot(BUCKET_MS)[0].buckets, 0, BUCKET_MS)
  assert.deepEqual(summarize(a), summarize(b))
  a.add(like('next', BUCKET_MS + 100, 'p', 70))
  const stat = windowStat(a.snapshot(2 * BUCKET_MS)[0].buckets, 0, 2 * BUCKET_MS)
  near(stat.sentiment, 10 / (2 + Math.log(101)))
  assert.equal(stat.volume, 2)
  assert.equal(stat.activePosts, 2)
})

test('exact partial windows and historical snapshots exclude future arrivals', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('first', 100, 0), post('unknown', 200, null), post('last', 300, 10)])
  const stopped = agg.snapshot(300)[0].buckets
  agg.add(like('future', 400, 'first', 1000, true, 0))
  const latest = agg.snapshot(500)[0].buckets
  assert.deepEqual(windowStat(latest, 0, 250), { volume: 2, activePosts: 2, traction: 0, sentiment: 0, spread: 0, scored: 1 })
  assert.equal(windowStat(latest, 0, 400).sentiment, 5)
  assert.equal(windowStat(latest, 0, 400).spread, 5)
  assert.equal(windowStat(latest, 100.5, 300).volume, 1)
  assert.equal(windowStat(stopped, 0, 1000).sentiment, 5)
  near(windowStat(latest, 0, 401).sentiment, 10 / (2 + Math.log(1001)))
})

test('multi-company events count once globally; self-contained likes need no parent post', () => {
  const agg = new Aggregator(companies)
  const event = like('orphan', 100, 'missing', 50, false, 0)
  event.postTime = null
  event.grades.push(grade(10, 'b'))
  agg.addBatch([event, event])
  const series = agg.snapshot(200)
  assert.equal(agg.posts, 0)
  assert.equal(agg.likes, 1)
  assert.deepEqual(series.map(s => s.buckets[0].sentiment), [0, 10])
  assert.equal(series[0].buckets[0].topPost.likesKnown, false)
  assert.equal(series[0].buckets[0].topPost.periodLikes, 50)
  assert.equal(series[0].buckets[0].topPost.timeKnown, false)
  assert.equal(windowStat(series[0].buckets, 0, 200).activePosts, 1)
})

test('clock pauses, resumes without catching up, preserves ties and completes', () => {
  const data: ReplayDataset = { version: 1, start: 0, end: 1000, companies, counts: { posts: 3, likes: 0 },
    events: [post('a', 100, 0), post('b', 100, 10), post('c', 900, 5)] }
  const clock = new ReplayClock(data, 10, 0)
  assert.deepEqual(clock.tick(10).map(e => e.id), ['a', 'b'])
  clock.pause()
  assert.equal(clock.tick(10000).length, 0)
  assert.equal(clock.now, 100)
  clock.resume(10000)
  assert.equal(clock.tick(10010).length, 0)
  assert.equal(clock.now, 200)
  assert.deepEqual(clock.tick(10100).map(e => e.id), ['c'])
  assert.equal(clock.now, 1000)
  assert.equal(clock.status, 'complete')
})

const packagePath = new URL('../data/demo.json.gz', import.meta.url)
const hasPackage = existsSync(packagePath)
const dataset: ReplayDataset = hasPackage ? await loadReplayDataset(packagePath) : null!
const packageTest = (name: string, fn: () => void) => test(name, { skip: hasPackage ? false : 'Optional classified replay package is not installed' }, fn)

// Independent raw-event reference: no production aggregation or weighting helpers.
function referenceWindow(company: string, start: number, end: number) {
  const initialized = new Set<string>()
  const posts = new Map<string, { published: boolean; likes: number; score: number | null }>()
  let traction = 0
  for (const event of dataset.events) {
    if (event.t >= end) break
    let likes = 0
    if (event.kind === 'like') {
      likes = event.opening && initialized.has(event.postId) ? 0 : event.delta!
      if (event.opening) initialized.add(event.postId)
    }
    if (event.t < start) continue
    const g = event.grades.find(g => g.company === company)
    if (!g) continue
    if (event.kind === 'like' && !event.opening) traction += likes
    const previous = posts.get(event.postId)
    posts.set(event.postId, { published: !!previous?.published || event.kind === 'post',
      likes: (previous?.likes ?? 0) + likes, score: g.score })
  }
  let W = 0, S = 0, Q = 0, scored = 0, volume = 0, activePosts = 0
  for (const p of posts.values()) {
    if (p.published) volume++
    const w = Number(p.published) + Math.log(1 + Math.max(0, p.likes))
    if (w <= 0) continue
    activePosts++
    if (p.score === null) continue
    scored++
    W += w
    S += w * p.score
    Q += w * p.score ** 2
  }
  return { volume, activePosts, scored, traction, sentiment: S / W, spread: Math.sqrt(Math.max(0, Q / W - (S / W) ** 2)) }
}

packageTest('real package counts and current/historical activity windows match an independent calculation', () => {
  const agg = new Aggregator(dataset.companies)
  agg.addBatch(dataset.events)
  assert.equal(agg.posts, dataset.counts.posts)
  assert.equal(agg.likes, dataset.counts.likes)
  assert.equal(dataset.events.length, dataset.counts.posts + dataset.counts.likes)
  for (const now of [Date.parse('2026-08-21T00:00:00Z'), Date.parse('2026-09-12T10:37:00Z')]) {
    const series = agg.snapshot(now)
    for (const company of ['google', 'anthropic', 'openai']) {
      for (const lag of [0, 1, 6]) {
        const end = now - lag * BUCKET_MS
        const start = end - 86400000
        const actual = windowStat(series.find(s => s.id === company)!.buckets, start, end)
        const expected = referenceWindow(company, start, end)
        for (const field of ['volume', 'activePosts', 'scored', 'traction'] as const) assert.equal(actual[field], expected[field])
        near(actual.sentiment, expected.sentiment)
        near(actual.spread, expected.spread)
      }
    }
  }
})

packageTest('real historical line points and their popups stay frozen as more activity arrives', () => {
  const agg = new Aggregator(dataset.companies)
  const cutoff = Date.parse('2026-09-02T12:00:00Z')
  agg.addBatch(dataset.events.filter(e => e.t < cutoff))
  const summarize = () => agg.snapshot(dataset.end).map(s => ({ id: s.id, buckets: s.buckets
    .filter(b => b.start + BUCKET_MS <= cutoff && b.activity?.length)
    .map(b => ({ start: b.start, sentiment: b.sentiment, weight: b.weight, sqSum: b.sqSum, volume: b.volume, topPost: b.topPost })) }))
  const frozen = structuredClone(summarize())
  agg.addBatch(dataset.events.filter(e => e.t >= cutoff))
  assert.deepEqual(summarize(), frozen)
})

packageTest('batch sizes do not alter results; historical inspection matches a stopped replay', () => {
  const full = new Aggregator(dataset.companies)
  full.addBatch(dataset.events)
  const partial = new Aggregator(dataset.companies)
  const cutoff = Date.parse('2026-09-02T10:37:00Z')
  const received = dataset.events.filter(e => e.t <= cutoff)
  for (let i = 0; i < received.length; i += 123) partial.addBatch(received.slice(i, i + 123))
  const summarize = (agg: Aggregator) => agg.snapshot(cutoff).map(s => ({ id: s.id,
    stat: windowStat(s.buckets, cutoff - 86400000, cutoff),
    points: s.buckets.map(b => ({ start: b.start, volume: b.volume, sentiment: b.sentiment, top: b.topPost })) }))
  assert.deepEqual(summarize(full), summarize(partial))
})
