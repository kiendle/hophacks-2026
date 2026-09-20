import { ActivityAccumulator } from './activity'
import { BUCKET_MS, LINE_INTERVALS, TREND_WINDOW_MS } from './config'
import type { Bucket, Series, TimeRange } from './types'
import { windowStat } from './window'

export interface ChartPoint {
  /** Interval end, or the current playhead for the incomplete interval. */
  t: number
  s: number
  v: number
  bucket: Bucket
}

export interface TrendPoint { t: number; s: number }

// Completed samples survive replay ticks and viewport changes. Bucket identities
// change if their contents change; weak keys also let restarted runs be collected.
const completedTrends = new WeakMap<Bucket, { sources: Bucket[]; sentiment: number }>()
function trendSentiment(buckets: Bucket[], end: number): number {
  const start = end - TREND_WINDOW_MS
  const sources = buckets.filter(bucket => bucket.start < end && bucket.start + BUCKET_MS > start)
  const last = sources.at(-1)
  const complete = last && last.start + BUCKET_MS === end && (last.asOf ?? -Infinity) >= end - 1
  if (complete) {
    const cached = completedTrends.get(last)
    if (cached && cached.sources.length === sources.length && cached.sources.every((b, i) => b === sources[i])) {
      return cached.sentiment
    }
  }
  const sentiment = windowStat(sources, start, end).sentiment
  if (complete) completedTrends.set(last, { sources, sentiment })
  return sentiment
}

/** Display-only projection. It never changes the stream's four-hour storage buckets. */
export function chartData(series: Series, view: TimeRange, cutoff: number, intervalMs: number) {
  if (!LINE_INTERVALS.some(interval => interval.value === intervalMs)) throw new Error('Unsupported chart interval')
  const points: ChartPoint[] = []
  const trend: TrendPoint[] = []
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
    const bucket: Bucket = { start, sentiment: result.sentiment, weight: result.weight,
      sqSum: result.sqSum, volume: result.volume, activePosts: result.activePosts, scored: result.scored,
      traction: result.traction, snapshots: [], asOf: Math.ceil(t) - 1,
      topPost: result.topPost ?? { id: '', handle: '', text: '', time: start, sentiment: NaN,
        likes: 0, replies: 0, retweets: 0, quotes: 0, likesKnown: false, otherMetricsKnown: false } }
    points.push({ t, s: result.sentiment, v: result.volume, bucket })
  }
  // Fixed four-hour samples keep the 24h trend independent of the point interval.
  const trendStart = Math.max(first + BUCKET_MS, Math.floor(view.start / BUCKET_MS) * BUCKET_MS - BUCKET_MS)
  const trendEnd = Math.min(view.end + BUCKET_MS, cutoff)
  const sample = (t: number) => trend.push({ t, s: trendSentiment(series.buckets, t) })
  for (let t = trendStart; t <= trendEnd; t += BUCKET_MS) sample(t)
  // Include the live endpoint, but don't draw an invented point past the clock.
  if (cutoff > first && cutoff <= view.end + BUCKET_MS && (!trend.length || trend.at(-1)!.t < cutoff)) sample(cutoff)
  return { points, trend }
}
