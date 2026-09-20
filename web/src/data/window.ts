import { BUCKET_MS } from './config'
import { spread } from './sentiment'
import { ActivityAccumulator } from './activity'
import type { Bucket, Snapshot } from './types'

/** Signed engagement changes known at t (opening balances are not new likes). */
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
  return s.length === 0 ? bucket.traction : 0
}

export interface WindowStat {
  /** New posts inside the window, not the number of like updates. */
  volume: number
  /** Signed non-opening like changes received in the window. */
  traction: number
  sentiment: number
  /** Weighted population standard deviation, 0 to 5. */
  spread: number
  scored?: number
  /** Distinct posts with positive influence, including resurfacing older posts. */
  activePosts?: number
}

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
 * Streamed activity uses receipt times in [start, end). Group a post's initial
 * popularity and deltas over the entire window before applying log damping.
 * Synthetic buckets retain their legacy proportional-overlap approximation.
 */
export function windowStat(buckets: Bucket[], start: number, end: number): WindowStat {
  if (buckets.some(b => b.activity !== undefined)) {
    const stats = new ActivityAccumulator()
    for (let i = firstAfter(buckets, start); i < buckets.length; i++) {
      const b = buckets[i]
      if (b.start >= end) break
      const cutoff = Math.min(Math.ceil(end) - 1, b.asOf ?? Infinity)
      for (const activity of b.activity ?? []) {
        if (activity.event.t > cutoff) break
        if (activity.event.t >= start) stats.add(activity)
      }
    }
    const { volume, activePosts, traction, sentiment, spread, scored } = stats.result()
    return { volume, activePosts, traction, sentiment, spread, scored }
  }
  let volume = 0, traction = 0, weighted = 0, weight = 0, sqSum = 0, scored = 0
  for (let i = firstAfter(buckets, start); i < buckets.length; i++) {
    const b = buckets[i]
    if (b.start >= end) break
    const from = Math.max(start, b.start)
    const seen = (Math.min(end, b.start + BUCKET_MS) - from) / BUCKET_MS
    const kept = (b.start + BUCKET_MS - from) / BUCKET_MS
    volume += seen * b.volume
    weight += seen * b.weight
    scored += seen * (b.scored ?? b.volume)
    weighted += seen * b.weight * b.sentiment
    sqSum += seen * b.sqSum
    traction += kept * tractionAt(b, end)
  }
  const mean = weight > 0 ? weighted / weight : NaN
  return { volume, traction, sentiment: mean, spread: spread(weight, mean, sqSum), scored }
}
