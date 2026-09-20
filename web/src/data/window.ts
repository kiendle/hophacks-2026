import { BUCKET_MS } from './config'
import { spread } from './sentiment'
import type { Bucket, Snapshot } from './types'

/**
 * Traction of a bucket's posts as known at time `t`: the most recent snapshot
 * at or before `t`, never a later one, so replays cannot leak future engagement.
 */
export function tractionAt(bucket: Bucket, t: number): number {
  const s = bucket.snapshots
  let lo = 0
  let hi = s.length - 1
  let found: Snapshot | null = null
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (s[mid].t <= t) {
      found = s[mid]
      lo = mid + 1
    } else hi = mid - 1
  }
  if (found) return found.traction
  // A streamed bucket carries no snapshot until it closes, and its running
  // total is current rather than final, so it is safe to read directly.
  return s.length === 0 ? bucket.traction : 0
}

export interface WindowStat {
  /** Posts inside the window. */
  volume: number
  /** Engagement of those posts as of the window's end. */
  traction: number
  /** Traction-weighted mean sentiment, NaN when the window is empty. */
  sentiment: number
  /** Weighted standard deviation of sentiment across the window, 0 to 5. */
  spread: number
}

/** First bucket index whose span ends after `t`. */
function firstAfter(buckets: Bucket[], t: number): number {
  let lo = 0
  let hi = buckets.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (buckets[mid].start + BUCKET_MS <= t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * Aggregates the posts created in [start, end], as seen at `end`. Posts are
 * assumed spread evenly within a bucket, so partial overlap counts
 * proportionally and values move continuously as the window slides.
 */
export function windowStat(buckets: Bucket[], start: number, end: number): WindowStat {
  let volume = 0
  let traction = 0
  let weighted = 0
  // Spreads cannot be averaged, so the moments are summed and combined at the end.
  let weight = 0
  let sqSum = 0
  for (let i = firstAfter(buckets, start); i < buckets.length; i++) {
    const b = buckets[i]
    if (b.start >= end) break
    const from = Math.max(start, b.start)
    // Posts after `end` do not exist yet.
    const seen = (Math.min(end, b.start + BUCKET_MS) - from) / BUCKET_MS
    // Snapshots cover the whole bucket; drop only the share that left the window.
    const kept = (b.start + BUCKET_MS - from) / BUCKET_MS
    volume += seen * b.volume
    weight += seen * b.weight
    weighted += seen * b.weight * b.sentiment
    sqSum += seen * b.sqSum
    traction += kept * tractionAt(b, end)
  }
  const mean = weight > 0 ? weighted / weight : NaN
  return { volume, traction, sentiment: mean, spread: spread(weight, mean, sqSum) }
}
