import { BUCKET_MS } from './config'
import { engagementWeight, spread } from './sentiment'
import type { Bucket, Snapshot } from './types'
import { atOrBefore } from './aggregator'

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
  /** Signed engagement updates observed in the window (excluding opening balances). */
  traction: number
  /** Traction-weighted mean sentiment, NaN when the window is empty. */
  sentiment: number
  /** Weighted standard deviation of sentiment across the window, 0 to 5. */
  spread: number
  scored?: number
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
 * Streamed posts use exact publication times in [start, end), with engagement
 * known before end, capped by the snapshot time. Synthetic buckets retain
 * their legacy proportional-overlap approximation.
 */
export function windowStat(buckets: Bucket[], start: number, end: number): WindowStat {
  let volume = 0
  let traction = 0
  let weighted = 0
  // Spreads cannot be averaged, so the moments are summed and combined at the end.
  let weight = 0
  let sqSum = 0
  let scored = 0
  for (let i = firstAfter(buckets, start); i < buckets.length; i++) {
    const b = buckets[i]
    if (b.start >= end) break
    if (b.opinions) {
      const cutoff = Math.min(Math.ceil(end) - 1, b.asOf ?? Infinity)
      for (const post of b.opinions) {
        if (post.t > cutoff) break
        if (post.t < start) continue
        volume++
        if (post.score === null) continue
        scored++
        const balance = atOrBefore(post.balances, cutoff)
        const w = engagementWeight(balance?.known ? balance.value : undefined)
        weight += w
        weighted += w * post.score
        sqSum += w * post.score ** 2
      }
      if (cutoff >= start) {
        traction += (atOrBefore(b.likeHistory ?? [], cutoff)?.traction ?? 0)
          - (atOrBefore(b.likeHistory ?? [], Math.ceil(start) - 1)?.traction ?? 0)
      }
      continue
    }
    if (b.history) {
      const last = atOrBefore(b.history, Math.ceil(end) - 1)
      const first = atOrBefore(b.history, Math.ceil(start) - 1)
      volume += (last?.volume ?? 0) - (first?.volume ?? 0)
      weight += (last?.weight ?? 0) - (first?.weight ?? 0)
      scored += (last?.weight ?? 0) - (first?.weight ?? 0)
      weighted += (last?.sum ?? 0) - (first?.sum ?? 0)
      sqSum += (last?.sqSum ?? 0) - (first?.sqSum ?? 0)
      traction += (atOrBefore(b.likeHistory ?? [], Math.ceil(end) - 1)?.traction ?? 0) - (atOrBefore(b.likeHistory ?? [], Math.ceil(start) - 1)?.traction ?? 0)
      continue
    }
    const from = Math.max(start, b.start)
    // Posts after `end` do not exist yet.
    const seen = (Math.min(end, b.start + BUCKET_MS) - from) / BUCKET_MS
    // Snapshots cover the whole bucket; drop only the share that left the window.
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
