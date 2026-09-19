/**
 * Development fixture only: replays the synthetic month as an event stream,
 * synthesizing each frame's events from the bucket stats so memory stays flat.
 */
import { BUCKET_MS } from './config'
import { generateDevSeries } from './devMock'
import type { EventBatch, StreamEvent } from './events'
import { spread } from './sentiment'
import type { Series } from './types'

/** Posts folded into one row, to keep a fast replay cheap. */
const POSTS_PER_ROW = 20
/** Like rows emitted per bucket slice. */
const LIKE_ROWS = 3
/** Firehose posts scanned for each one kept. */
const READ_PER_KEPT = 38

function slice(series: Series, from: number, to: number, out: StreamEvent[]) {
  const first = Math.floor(from / BUCKET_MS) * BUCKET_MS
  for (let start = first; start < to; start += BUCKET_MS) {
    const b = series.buckets[Math.round((start - series.buckets[0].start) / BUCKET_MS)]
    if (!b) continue
    const covered = Math.min(to, start + BUCKET_MS) - Math.max(from, start)
    if (covered <= 0) continue
    const share = covered / BUCKET_MS
    const at = (k: number) => Math.max(from, start) + covered * k
    const sd = spread(b.weight, b.sentiment, b.sqSum)
    const base = {
      subtopic: series.id,
      text: b.topPost.text,
      handle: b.topPost.handle,
      postTime: b.topPost.time,
      likes: b.topPost.likes,
      replies: b.topPost.replies,
      reposts: b.topPost.retweets,
    }

    const posts = Math.round(b.volume * share)
    for (let n = 0; n < posts; n += POSTS_PER_ROW) {
      const count = Math.min(POSTS_PER_ROW, posts - n)
      const k = (n + count / 2) / Math.max(posts, 1)
      out.push({
        ...base,
        kind: 'post',
        t: at(k),
        postId: `${series.id}-${start}-${n}`,
        // Spread the rows around the bucket's mean so the variance survives.
        sentiment: Math.min(10, Math.max(0, b.sentiment + sd * (k * 2 - 1) * 1.4)),
        significance: count,
        count,
      })
    }

    const traction = b.traction * share
    if (traction > 0)
      for (let r = 0; r < LIKE_ROWS; r++) {
        // The bucket's top post takes the biggest share, so it stays on top.
        const weight = r === 0 ? 0.5 : 0.25
        out.push({
          ...base,
          kind: 'like',
          t: at((r + 0.5) / LIKE_ROWS),
          postId: r === 0 ? b.topPost.id : `${series.id}-${start}-like-${r}`,
          sentiment: r === 0 ? b.topPost.sentiment : b.sentiment,
          significance: Math.round(traction * weight),
        })
      }
  }
}

/** Replays the fixture as batched events, compressing wall clock by `speed`. */
export function devEventConnect(subtopics: string[] | undefined, speed: number) {
  return (onBatch: (batch: EventBatch) => void) => {
    const series = generateDevSeries(subtopics).filter((s) => s.buckets.length)
    if (!series.length) return () => {}
    const start = Math.min(...series.map((s) => s.buckets[0].start))
    const end = Math.max(...series.map((s) => s.buckets.at(-1)!.start + BUCKET_MS))
    const startedAt = performance.now()
    let cursor = start
    let raf = 0

    const step = () => {
      const now = Math.min(end, start + (performance.now() - startedAt) * speed)
      const events: StreamEvent[] = []
      for (const s of series) slice(s, cursor, now, events)
      events.sort((a, b) => a.t - b.t)
      cursor = now
      const kept = events.reduce((n, e) => n + (e.count ?? 1), 0)
      onBatch({ events, now, read: kept * READ_PER_KEPT })
      if (now < end) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }
}
