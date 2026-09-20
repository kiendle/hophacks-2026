import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ActivityAccumulator } from '../src/data/activity.ts'
import { Aggregator } from '../src/data/aggregator.ts'
import { chartData, type ChartPoint, type TrendPoint } from '../src/data/chartData.ts'
import { BUCKET_MS, LINE_INTERVALS, TREND_WINDOW_MS } from '../src/data/config.ts'
import type { Grade, ReplayEvent } from '../src/data/replayTypes.ts'
import type { Series, TimeRange } from '../src/data/types.ts'
import { windowStat } from '../src/data/window.ts'

const DAY = 6 * BUCKET_MS
const companies = [{ id: 'a', name: 'A' }]
const grade = (score: number | null): Grade => ({ company: 'a', score,
  choice: score === null ? 'insufficient_evidence' : 'neutral', confidence: 0.8,
  probabilities: { positive: 0, negative: 0, neutral: 1, mixed: 0, insufficient_evidence: 0 } })
const post = (id: string, t: number, score: number | null): ReplayEvent => ({ id, kind: 'post', postId: id,
  t, postTime: t, text: id, authorId: 'author', contentVersion: id, observedAt: t, grades: [grade(score)] })
const like = (id: string, t: number, postId: string, delta: number, score: number | null, opening = false): ReplayEvent => ({
  ...post(id, t, score), kind: 'like', postId, postTime: null, delta, opening,
})

// Independent, uncached version of the original streamed projection: collect
// activity across the viewport first, then group by its receipt timestamp.
function uncached(series: Series, view: TimeRange, cutoff: number, intervalMs: number) {
  const points: ChartPoint[] = [], trend: TrendPoint[] = []
  if (!series.buckets.length || !Number.isFinite(cutoff)) return { points, trend }
  const first = series.buckets[0].start
  const plotStart = Math.max(Math.floor(first / intervalMs) * intervalMs,
    Math.floor(view.start / intervalMs) * intervalMs - intervalMs)
  const plotEnd = Math.min((Math.floor(view.end / intervalMs) + 2) * intervalMs, cutoff)
  const groups = new Map<number, ActivityAccumulator>()
  for (const bucket of series.buckets) {
    if (bucket.start + BUCKET_MS <= plotStart) continue
    if (bucket.start >= plotEnd) break
    for (const activity of bucket.activity ?? []) {
      const t = activity.event.t
      if (t >= plotEnd || t > (bucket.asOf ?? cutoff)) break
      if (t < plotStart) continue
      const start = Math.floor(t / intervalMs) * intervalMs
      let group = groups.get(start)
      if (!group) { group = new ActivityAccumulator(); groups.set(start, group) }
      group.add(activity)
    }
  }
  for (let start = plotStart; start < plotEnd; start += intervalMs) {
    const t = Math.min(start + intervalMs, cutoff)
    const result = (groups.get(start) ?? new ActivityAccumulator()).result()
    points.push({ t, s: result.sentiment, v: result.volume,
      bucket: { start, sentiment: result.sentiment, weight: result.weight, sqSum: result.sqSum,
        volume: result.volume, activePosts: result.activePosts, scored: result.scored, traction: result.traction,
        snapshots: [], asOf: Math.ceil(t) - 1,
        topPost: result.topPost ?? { id: '', handle: '', text: '', time: start, sentiment: NaN,
          likes: 0, replies: 0, retweets: 0, quotes: 0, likesKnown: false, otherMetricsKnown: false } } })
  }
  const trendStart = Math.max(first + BUCKET_MS, Math.floor(view.start / BUCKET_MS) * BUCKET_MS - BUCKET_MS)
  const trendEnd = Math.min(view.end + BUCKET_MS, cutoff)
  const sample = (t: number) => trend.push({ t, s: windowStat(series.buckets, t - TREND_WINDOW_MS, t).sentiment })
  for (let t = trendStart; t <= trendEnd; t += BUCKET_MS) sample(t)
  if (cutoff > first && cutoff <= view.end + BUCKET_MS && (!trend.length || trend.at(-1)!.t < cutoff)) sample(cutoff)
  return { points, trend }
}

function assertProjection(series: Series, view: TimeRange, cutoff: number, interval: number) {
  const expected = uncached(series, view, cutoff, interval)
  assert.deepEqual(chartData(series, view, cutoff, interval), expected)
  assert.deepEqual(chartData(series, view, cutoff, interval), expected, 'A warm cache must remain exact')
}

test('warm display projections match the original algorithm across replay ticks, intervals, views, and restart', () => {
  const events: ReplayEvent[] = []
  for (let bucket = 0; bucket < 30; bucket++) {
    if (bucket >= 8 && bucket < 14) continue
    const t = bucket * BUCKET_MS
    events.push(post(`p${bucket}`, t, bucket % 11), post(`unscored${bucket}`, t + 1, null),
      like(`opening${bucket}`, t + 2, `p${bucket}`, 100, bucket % 11, true),
      like(`repeat${bucket}`, t + 3, `p${bucket}`, 120, bucket % 11, true),
      like(`resurface${bucket}`, t + 4, 'p0', 30, 2),
      like(`correction${bucket}`, t + BUCKET_MS - 1, 'p0', -10, 7))
  }
  const views = [{ start: 0, end: 5 * DAY }, { start: DAY + 123, end: 2 * DAY + 789 },
    { start: 3 * DAY, end: 4 * DAY }, { start: 8 * DAY, end: 9 * DAY }]
  const agg = new Aggregator(companies)
  let received = 0
  for (const cutoff of [1, BUCKET_MS - 1, DAY, DAY + 4, 2 * DAY + 0.5, 4 * DAY, 5 * DAY]) {
    while (received < events.length && events[received].t <= cutoff) agg.add(events[received++])
    const series = agg.snapshot(cutoff)[0]
    for (const interval of LINE_INTERVALS) for (const view of views) assertProjection(series, view, cutoff, interval.value)
  }
  const full = agg.snapshot(5 * DAY)[0]
  for (const cutoff of [0, 1, 3.5, BUCKET_MS, DAY - 0.5, DAY + 4, 2 * DAY, 4 * DAY]) {
    for (const interval of LINE_INTERVALS) for (const view of views) assertProjection(full, view, cutoff, interval.value)
  }
  const restarted = new Aggregator(companies)
  restarted.addBatch([post('new run', 0, 10), post('later', DAY, 0)])
  for (const interval of LINE_INTERVALS) assertProjection(restarted.snapshot(5 * DAY)[0], views[0], 5 * DAY, interval.value)
})

test('shared activity appends, earlier bucket replacements, array replacements, and asOf changes invalidate caches', () => {
  const agg = new Aggregator(companies)
  agg.addBatch([post('early', 100, 0), post('late', DAY - 100, 10)])
  const original = agg.snapshot(DAY)[0]
  const view = { start: 0, end: DAY }
  assertProjection(original, view, DAY, DAY)
  agg.add(post('appended', 200, 10))
  assertProjection(original, view, DAY, DAY)
  assert.equal(chartData(original, view, DAY, DAY).points[0].v, 3)
  const revised = agg.snapshot(DAY)[0]
  assert.equal(revised.buckets.at(-1), original.buckets.at(-1), 'The cache anchor itself is unchanged')
  assertProjection(revised, view, DAY, DAY)
  original.buckets[0].asOf = 100
  assertProjection(original, view, DAY, DAY)
  assert.equal(chartData(original, view, DAY, DAY).points[0].v, 2)
  original.buckets[0].asOf = undefined
  assertProjection(original, view, DAY, DAY)
  const replacement = post('replacement', 100, 5)
  original.buckets[0].activity = [{ event: replacement, grade: replacement.grades[0], likes: 0 }]
  assertProjection(original, view, DAY, DAY)
})

test('completed display intervals and trends reuse results without revisiting their activity', () => {
  const agg = new Aggregator(companies)
  for (let i = 0; i < 12; i++) agg.add(post(String(i), i * BUCKET_MS, i % 11))
  const series = agg.snapshot(2 * DAY)[0]
  const view = { start: 0, end: 2 * DAY }
  for (const interval of LINE_INTERVALS) chartData(series, view, 2 * DAY, interval.value)
  const originalAdd = ActivityAccumulator.prototype.add
  let visits = 0
  ActivityAccumulator.prototype.add = function (activity) { visits++; originalAdd.call(this, activity) }
  try {
    for (const interval of LINE_INTERVALS) chartData(series, view, 2 * DAY, interval.value)
    assert.equal(visits, 0, 'Warm completed projections must not scan the event history again')
  } finally { ActivityAccumulator.prototype.add = originalAdd }
})

test('mutating a returned popup cannot corrupt cached projection results', () => {
  const agg = new Aggregator(companies)
  agg.add(post('first', 100, 2))
  const series = agg.snapshot(DAY)[0]
  const view = { start: 0, end: DAY }
  const before = chartData(series, view, DAY, DAY)
  before.points[0].bucket.topPost.text = 'changed by consumer'
  before.points[0].bucket.volume = 999
  assertProjection(series, view, DAY, DAY)
})
