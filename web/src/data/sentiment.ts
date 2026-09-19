import type { Post } from './types'

/** Engagement score. Amplifying actions count more than passive ones. */
export function traction(p: Pick<Post, 'likes' | 'replies' | 'retweets' | 'quotes'>): number {
  return p.likes + p.replies + 2 * (p.retweets + p.quotes)
}

/**
 * Weight of a post in the bucket average: w = 1 + ln(1 + traction).
 * Log damping keeps one viral post from drowning out the bucket; the +1
 * means posts with no engagement still count.
 */
export function weight(p: Post): number {
  return 1 + Math.log1p(traction(p))
}

/** Traction-weighted mean sentiment: sum(w * s) / sum(w). */
export function weightedSentiment(posts: Post[]): number {
  let num = 0
  let den = 0
  for (const p of posts) {
    const w = weight(p)
    num += w * p.sentiment
    den += w
  }
  return den ? num / den : NaN
}

export interface SentimentStats {
  /** Traction-weighted mean, NaN when there is no weight. */
  mean: number
  /** Sum of weights. */
  weight: number
  /** Sum of weight * sentiment^2. */
  sqSum: number
}

/** Mean plus the moments needed to combine buckets and take a spread. */
export function sentimentStats(posts: Post[]): SentimentStats {
  let total = 0
  let wSum = 0
  let sqSum = 0
  for (const p of posts) {
    const w = weight(p)
    total += w
    wSum += w * p.sentiment
    sqSum += w * p.sentiment * p.sentiment
  }
  return { mean: total ? wSum / total : NaN, weight: total, sqSum }
}

/**
 * Weighted standard deviation of sentiment, 0 to 5. Low mean with high spread
 * is a fight; low mean with low spread is agreement that it is bad.
 */
export function spread(weight: number, mean: number, sqSum: number): number {
  if (!weight || !isFinite(mean)) return 0
  return Math.sqrt(Math.max(0, sqSum / weight - mean * mean))
}
