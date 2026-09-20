import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Aggregator } from '../src/data/aggregator.ts'
import { chartData } from '../src/data/chartData.ts'
import { BUCKET_MS, DEFAULT_LINE_INTERVAL, LINE_INTERVALS } from '../src/data/config.ts'
import { windowStat } from '../src/data/window.ts'
import type { Grade, ReplayEvent } from '../src/data/replayTypes.ts'
import { AXIS_SHRINK_DELAY_MS, SentimentAxis, sentimentDomain } from '../src/sentimentDomain.ts'

const HOUR = 3600000
const DAY = 24 * HOUR
const companies = [{ id: 'a', name: 'A' }]
const grade = (score: number): Grade => ({ company: 'a', score, choice: score > 5 ? 'positive' : 'negative',
  confidence: 0.8, probabilities: { positive: 0.5, negative: 0.5, neutral: 0, mixed: 0, insufficient_evidence: 0 } })
const post = (id: string, t: number, score: number): ReplayEvent => ({ id, kind: 'post', postId: id, t, postTime: t,
  text: id, authorId: null, contentVersion: id, observedAt: t, grades: [grade(score)] })
const like = (id: string, t: number, postId: string, delta: number, score: number, opening = false): ReplayEvent => ({
  ...post(id, t, score), kind: 'like', postId, postTime: 100, delta, opening,
})
const near = (a: number, b: number) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`)

test('12h and daily points regroup the underlying events, including popup ranking', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('p', 100, 0), like('open-p', 200, 'p', 50, 0, true), post('q', BUCKET_MS + 100, 10),
    like('gain-q', BUCKET_MS + 200, 'q', 70, 10), like('gain-p', 2 * BUCKET_MS + 100, 'p', 50, 0)])
  const series = agg.snapshot(DAY)[0]
  const view = { start: 0, end: DAY }
  const fine = chartData(series, view, DAY, BUCKET_MS)
  const medium = chartData(series, view, DAY, DEFAULT_LINE_INTERVAL)
  const daily = chartData(series, view, DAY, DAY)
  assert.equal(fine.points.length, 6)
  assert.equal(medium.points.length, 2)
  assert.equal(daily.points.length, 1)
  const expected = 10 * (1 + Math.log(71)) / (2 + Math.log(101) + Math.log(71))
  near(medium.points[0].s, expected)
  near(daily.points[0].s, expected)
  assert.equal(medium.points[0].bucket.topPost.id, 'p')
  assert.equal(medium.points[0].bucket.topPost.periodLikes, 100)
  assert.equal(fine.points[1].bucket.topPost.id, 'q')
  assert.equal(medium.points[0].v, 2)
  assert.equal(daily.points[0].v, 2)
  assert.deepEqual(fine.trend, medium.trend)
  assert.deepEqual(medium.trend, daily.trend)
  assert.equal(series.buckets[0].topPost.periodLikes, 50, 'Projection must not mutate source buckets')
})

test('24h trend uses only past activity and preserves completed samples', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('low', 100, 0), post('high', DAY + 100, 10)])
  const view = { start: 0, end: 2 * DAY }
  const cutoff = DAY + 2 * HOUR
  const full = agg.snapshot(2 * DAY)[0]
  const before = chartData(full, view, cutoff, DEFAULT_LINE_INTERVAL)
  assert.equal(before.trend.at(-1)!.t, cutoff)
  assert.ok(before.points.every(point => point.t <= cutoff))
  assert.ok(before.trend.every(point => point.t <= cutoff))
  assert.equal(before.trend.find(point => point.t === DAY)!.s, 0)
  assert.equal(before.trend.at(-1)!.s, 10, 'Posts older than 24h leave the trend window')
  for (const point of before.trend) {
    near(point.s, windowStat(full.buckets, point.t - DAY, point.t).sentiment)
  }
  agg.add(like('later', DAY + 3 * HOUR, 'low', 1000, 0))
  const after = chartData(agg.snapshot(2 * DAY)[0], view, 2 * DAY, DEFAULT_LINE_INTERVAL)
  assert.deepEqual(after.trend.filter(point => point.t <= DAY), before.trend.filter(point => point.t <= DAY))
  assert.deepEqual(after.points.filter(point => point.t <= DAY), before.points.filter(point => point.t <= DAY))
})

test('live and historical projections agree; bins align to UTC and gaps stay empty', () => {
  const events = [post('first', HOUR, 2), post('last', 2 * DAY + HOUR, 8)]
  const full = new Aggregator(companies), stopped = new Aggregator(companies)
  full.addBatch(events)
  stopped.add(events[0])
  const cutoff = 3 * HOUR, view = { start: 0, end: cutoff }
  for (const interval of LINE_INTERVALS) {
    const actual = chartData(full.snapshot(3 * DAY)[0], view, cutoff, interval.value)
    assert.deepEqual(actual, chartData(stopped.snapshot(cutoff)[0], view, cutoff, interval.value))
    assert.equal(actual.points[0].bucket.start, 0)
    assert.equal(actual.points[0].t, cutoff)
  }
  const daily = chartData(full.snapshot(3 * DAY)[0], { start: 0, end: 3 * DAY }, 3 * DAY, DAY)
  assert.ok(Number.isNaN(daily.points[1].s))
  assert.ok(daily.trend.some(point => point.t > DAY + HOUR && point.t < 2 * DAY && Number.isNaN(point.s)))
})

test('restrict intervals to the supported display options', () => {
  assert.throws(() => chartData({ ...companies[0], color: '#000', buckets: [] }, { start: 0, end: DAY }, DAY, 0))
})

test('cached completed trends invalidate when an earlier contributing bucket changes', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('early', 100, 0), post('late', DAY - 100, 10)])
  const view = { start: 0, end: DAY }
  const original = agg.snapshot(DAY)[0]
  const before = chartData(original, view, DAY, DAY)
  near(before.trend.at(-1)!.s, 5)
  assert.deepEqual(chartData(original, view, DAY, DAY), before)
  agg.add(post('extra', 200, 10))
  const revised = agg.snapshot(DAY)[0]
  assert.equal(revised.buckets.at(-1), original.buckets.at(-1))
  near(chartData(revised, view, DAY, DAY).trend.at(-1)!.s, 20 / 3)
})

test('axis limits use whole numbers, include data, and never zoom narrower than four points', () => {
  assert.deepEqual(sentimentDomain(4.9, 5.1), [3, 7])
  assert.deepEqual(sentimentDomain(0, 0.2), [0, 4])
  assert.deepEqual(sentimentDomain(9.8, 10), [6, 10])
  assert.deepEqual(sentimentDomain(NaN, NaN), [3, 7])
  for (let lo = 0; lo <= 10; lo += 0.25) {
    for (let hi = lo; hi <= 10; hi += 0.25) {
      const [a, b] = sentimentDomain(lo, hi)
      assert.ok(Number.isInteger(a) && Number.isInteger(b))
      assert.ok(a <= lo && b >= hi && a >= 0 && b <= 10 && b - a >= 4)
    }
  }
})

test('axis expands promptly but requires a stable target before shrinking', () => {
  const axis = new SentimentAxis()
  assert.deepEqual(axis.update([3, 7], 0), [3, 7])
  assert.deepEqual(axis.update([1, 9], 100), [1, 9])
  assert.deepEqual(axis.update([3, 7], 200), [1, 9])
  assert.deepEqual(axis.update([3, 7], 200 + AXIS_SHRINK_DELAY_MS - 1), [1, 9])
  assert.deepEqual(axis.update([3, 7], 200 + AXIS_SHRINK_DELAY_MS), [3, 7])
  assert.deepEqual(axis.update([0, 4], 3000), [0, 7])
  assert.deepEqual(axis.update([0, 4], 3000 + AXIS_SHRINK_DELAY_MS), [0, 4])
})

test('an unstable axis target resets the contraction delay', () => {
  const axis = new SentimentAxis()
  axis.update([0, 10], 0)
  axis.update([2, 8], 100)
  assert.deepEqual(axis.update([3, 7], 2000), [0, 10])
  assert.deepEqual(axis.update([3, 7], 2600), [0, 10])
  assert.deepEqual(axis.update([3, 7], 2000 + AXIS_SHRINK_DELAY_MS), [3, 7])
})


test('saved archive buckets remain visible at every display interval without fabricated like events', () => {
  const buckets = [2, 8, 5].map((sentiment, i) => ({ start: i * BUCKET_MS, sentiment, weight: 2,
    sqSum: 2 * sentiment ** 2, volume: 1, traction: 4, snapshots: [],
    topPost: { id: String(i), handle: 'author', text: 'saved post', time: i * BUCKET_MS,
      sentiment, likes: i, replies: 0, retweets: 0, quotes: 0 } }))
  for (const interval of LINE_INTERVALS) {
    const result = chartData({ id: 'archive', name: 'Archive', color: '#000', buckets },
      { start: 0, end: 3 * BUCKET_MS }, 3 * BUCKET_MS, interval.value)
    assert.equal(result.points.reduce((sum, point) => sum + point.v, 0), 3)
    assert(result.points.some(point => Number.isFinite(point.s)))
    if (interval.value >= 3 * BUCKET_MS) assert.equal(result.points[0].s, 5)
  }
})
