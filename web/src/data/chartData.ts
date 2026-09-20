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

interface SourceVersion {
  bucket: Bucket
  activity: Bucket['activity']
  length: number
  asOf: number | undefined
}

// Snapshot activity arrays are shared with the replay accumulator and can grow
// even while an older Bucket object is still being inspected.
const versions = (sources: Bucket[]): SourceVersion[] => sources.map(bucket => ({
  bucket, activity: bucket.activity, length: bucket.activity?.length ?? 0, asOf: bucket.asOf,
}))
const unchanged = (saved: SourceVersion[], sources: Bucket[]) => saved.length === sources.length &&
  saved.every((source, i) => source.bucket === sources[i] && source.activity === sources[i].activity &&
    source.length === (sources[i].activity?.length ?? 0) && source.asOf === sources[i].asOf)

/** Only inspect buckets that overlap the requested window. */
function overlapping(buckets: Bucket[], start: number, end: number): Bucket[] {
  let lo = 0, hi = buckets.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (buckets[mid].start + BUCKET_MS <= start) lo = mid + 1
    else hi = mid
  }
  const sources: Bucket[] = []
  for (let i = lo; i < buckets.length && buckets[i].start < end; i++) sources.push(buckets[i])
  return sources
}

type ProjectionResult = ReturnType<ActivityAccumulator['result']>
interface CompletedProjection {
  start: number
  sources: SourceVersion[]
  result: ProjectionResult
}

// Completed intervals survive ticks and viewport changes. All contributing
// buckets are checked, including earlier buckets receiving late events. Weak
// keys let old replay runs and their projections be collected together.
const completedProjections = new WeakMap<Bucket, Map<number, CompletedProjection>>()
function projectActivity(sources: Bucket[], start: number, end: number, intervalMs: number): ProjectionResult {
  const last = sources.at(-1)
  const complete = last && end === start + intervalMs
  const cached = complete ? completedProjections.get(last)?.get(intervalMs) : undefined
  if (cached && cached.start === start && unchanged(cached.sources, sources)) return cached.result

  const stats = new ActivityAccumulator()
  for (const bucket of sources) {
    for (const activity of bucket.activity ?? []) {
      const t = activity.event.t
      if (t >= end || t > (bucket.asOf ?? end)) break
      if (t >= start) stats.add(activity)
    }
  }
  const result = stats.result()
  if (complete) {
    let intervals = completedProjections.get(last)
    if (!intervals) { intervals = new Map(); completedProjections.set(last, intervals) }
    intervals.set(intervalMs, { start, sources: versions(sources), result })
  }
  return result
}

const completedTrends = new WeakMap<Bucket, { sources: SourceVersion[]; sentiment: number }>()
function trendSentiment(buckets: Bucket[], end: number): number {
  const start = end - TREND_WINDOW_MS
  const sources = overlapping(buckets, start, end)
  const last = sources.at(-1)
  const complete = last && last.start + BUCKET_MS === end && (last.asOf ?? -Infinity) >= end - 1
  if (complete) {
    const cached = completedTrends.get(last)
    if (cached && unchanged(cached.sources, sources)) {
      return cached.sentiment
    }
  }
  const sentiment = windowStat(sources, start, end).sentiment
  if (complete) completedTrends.set(last, { sources: versions(sources), sentiment })
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
  const hasActivity = series.buckets.some(bucket => bucket.activity !== undefined)
  for (let start = plotStart; start < plotEnd; start += intervalMs) {
    const t = Math.min(start + intervalMs, cutoff)
    const sources = overlapping(series.buckets, start, t)
    let result = hasActivity ? projectActivity(sources, start, t, intervalMs) : new ActivityAccumulator().result()
    // Archive snapshots have measured bucket totals, not timestamped like events.
    // Aggregate those saved measurements without inventing an engagement history.
    if (!hasActivity) {
      const stats = windowStat(sources, start, t)
      let weight = 0, sqSum = 0
      for (const bucket of sources) {
        const fraction = (Math.min(t, bucket.start + BUCKET_MS) - Math.max(start, bucket.start)) / BUCKET_MS
        weight += fraction * bucket.weight
        sqSum += fraction * bucket.sqSum
      }
      result = { ...result, ...stats, weight, sqSum, sum: stats.sentiment * weight,
        activePosts: stats.activePosts ?? stats.volume, scored: stats.scored ?? stats.volume,
        topPost: sources.reduce<Bucket | undefined>((top, bucket) => !top || bucket.topPost.likes > top.topPost.likes ? bucket : top, undefined)?.topPost }
    }
    const bucket: Bucket = { start, sentiment: result.sentiment, weight: result.weight,
      sqSum: result.sqSum, volume: result.volume, activePosts: result.activePosts, scored: result.scored,
      traction: result.traction, snapshots: [], asOf: Math.ceil(t) - 1,
      topPost: result.topPost ? (hasActivity ? { ...result.topPost } : result.topPost) : { id: '', handle: '', text: '', time: start, sentiment: NaN,
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
