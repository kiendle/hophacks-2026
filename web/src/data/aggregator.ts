import { SERIES_COLORS } from './config'
import type { StreamEvent } from './events'
import { spread } from './sentiment'
import type { Bucket, Post, Series } from './types'

/** Candidate top posts kept per bucket before pruning. */
const TOP_KEEP = 24
const TOP_PRUNE_AT = 64

interface BucketAcc {
  start: number
  weight: number
  wSum: number
  sqSum: number
  volume: number
  traction: number
  /** Post id to its running significance, pruned to the leaders. */
  top: Map<string, { score: number; post: Post }>
}

interface SeriesAcc {
  id: string
  name: string
  buckets: Map<number, BucketAcc>
}

const emptyPost = (start: number): Post => ({
  id: `none-${start}`,
  handle: '',
  text: '',
  time: start,
  likes: 0,
  replies: 0,
  retweets: 0,
  quotes: 0,
  sentiment: 5,
})

/**
 * Buckets a live event stream into the `Series[]` the views read. Everything is
 * a running sum, so no posts are retained and late engagement is just another
 * event in the current bucket.
 */
export class Aggregator {
  private series = new Map<string, SeriesAcc>()
  private order: string[] = []

  private bucketMs: number

  /** `names` maps subtopic id to display name, so series keep the user's order. */
  constructor(bucketMs: number, names: Map<string, string> = new Map()) {
    this.bucketMs = bucketMs
    for (const [id, name] of names) this.ensure(id, name)
  }

  private ensure(id: string, name?: string): SeriesAcc {
    let s = this.series.get(id)
    if (!s) {
      s = { id, name: name ?? id, buckets: new Map() }
      this.series.set(id, s)
      this.order.push(id)
    }
    return s
  }

  add(e: StreamEvent) {
    const s = this.ensure(e.subtopic)
    const start = Math.floor(e.t / this.bucketMs) * this.bucketMs
    let b = s.buckets.get(start)
    if (!b) {
      b = { start, weight: 0, wSum: 0, sqSum: 0, volume: 0, traction: 0, top: new Map() }
      s.buckets.set(start, b)
    }
    const w = e.significance
    b.weight += w
    b.wSum += w * e.sentiment
    b.sqSum += w * e.sentiment * e.sentiment
    if (e.kind === 'post') b.volume += e.count ?? 1
    else b.traction += w

    const seen = b.top.get(e.postId)
    if (seen) seen.score += w
    else
      b.top.set(e.postId, {
        score: w,
        post: {
          id: e.postId,
          handle: e.handle,
          text: e.text,
          time: e.postTime,
          likes: e.likes ?? 0,
          replies: e.replies ?? 0,
          retweets: e.reposts ?? 0,
          quotes: 0,
          sentiment: e.sentiment,
        },
      })
    if (b.top.size > TOP_PRUNE_AT) {
      const kept = [...b.top].sort((a, c) => c[1].score - a[1].score).slice(0, TOP_KEEP)
      b.top = new Map(kept)
    }
  }

  addBatch(events: StreamEvent[]) {
    for (const e of events) this.add(e)
  }

  /** Contiguous buckets per subtopic, from the first bucket seen to `now`. */
  snapshot(now: number): Series[] {
    return this.order.map((id, i) => {
      const acc = this.series.get(id)!
      const starts = [...acc.buckets.keys()]
      const buckets: Bucket[] = []
      if (starts.length) {
        const first = Math.min(...starts)
        const last = Math.floor(now / this.bucketMs) * this.bucketMs
        let carried = emptyPost(first)
        let carriedSentiment = 5
        for (let start = first; start <= last; start += this.bucketMs) {
          const b = acc.buckets.get(start)
          if (!b || !b.weight) {
            // A quiet bucket holds the line where it was, with no posts.
            buckets.push({
              start,
              sentiment: carriedSentiment,
              weight: 0,
              sqSum: 0,
              volume: 0,
              traction: 0,
              snapshots: [{ t: start + this.bucketMs, traction: 0 }],
              topPost: carried,
            })
            continue
          }
          const mean = b.wSum / b.weight
          let top = carried
          let best = -1
          for (const { score, post } of b.top.values())
            if (score > best) {
              best = score
              top = post
            }
          carried = top
          carriedSentiment = mean
          buckets.push({
            start,
            sentiment: mean,
            weight: b.weight,
            sqSum: b.sqSum,
            volume: b.volume,
            traction: b.traction,
            snapshots: [{ t: start + this.bucketMs, traction: b.traction }],
            topPost: top,
          })
        }
      }
      return { id, name: acc.name, color: SERIES_COLORS[i % SERIES_COLORS.length], buckets }
    })
  }
}

/** Weighted spread of sentiment over a set of buckets, 0 to 5. */
export function spreadOver(buckets: Bucket[]): number {
  let weight = 0
  let wSum = 0
  let sqSum = 0
  for (const b of buckets) {
    weight += b.weight
    wSum += b.weight * b.sentiment
    sqSum += b.sqSum
  }
  return weight ? spread(weight, wSum / weight, sqSum) : 0
}
