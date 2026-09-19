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
