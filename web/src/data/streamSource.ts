import { Aggregator } from './aggregator'
import { BUCKET_MS } from './config'
import type { EventBatch, StreamEvent } from './events'
import type { DataSource } from './source'

/** How often the accumulated stream is handed to React, in ms. */
const FLUSH_MS = 80

interface Options {
  bucketMs?: number
  /** Subtopic id to display name, so series keep the user's names and order. */
  names?: Map<string, string>
}

/**
 * Buckets a batched event stream and publishes snapshots at a fixed rate, so a
 * replay running at hundreds of times real speed cannot flood React.
 */
export function createStreamSource(
  connect: (onBatch: (batch: EventBatch) => void) => () => void,
  { bucketMs = BUCKET_MS, names }: Options = {},
): DataSource {
  return {
    subscribe(onUpdate) {
      const agg = new Aggregator(bucketMs, names)
      let now = 0
      let read = 0
      let kept = 0
      let dirty = false

      const flush = () => {
        if (!dirty) return
        dirty = false
        onUpdate({ series: agg.snapshot(now), now, streaming: true, read, kept })
      }

      const timer = setInterval(flush, FLUSH_MS)
      const close = connect((batch) => {
        agg.addBatch(batch.events)
        now = Math.max(now, batch.now)
        kept += batch.events.length
        read += batch.read ?? batch.events.length
        dirty = true
      })

      return () => {
        clearInterval(timer)
        close()
      }
    },
  }
}

/** Connects to the backend's event stream. */
export function websocketConnect(url: string) {
  return (onBatch: (batch: EventBatch) => void) => {
    const ws = new WebSocket(url)
    ws.onmessage = (e) => {
      try {
        onBatch(JSON.parse(e.data) as EventBatch)
      } catch {
        // A malformed batch is dropped; the next one still lands.
      }
    }
    return () => ws.close()
  }
}

/** Replays events in time order, compressing wall clock by `speed`. */
export function replayConnect(events: StreamEvent[], speed: number, readPerKept = 38) {
  return (onBatch: (batch: EventBatch) => void) => {
    if (!events.length) return () => {}
    const start = events[0].t
    const startedAt = performance.now()
    let i = 0
    let raf = 0
    const step = () => {
      const now = start + (performance.now() - startedAt) * speed
      const batch: StreamEvent[] = []
      while (i < events.length && events[i].t <= now) batch.push(events[i++])
      if (batch.length) onBatch({ events: batch, now, read: batch.length * readPerKept })
      else onBatch({ events: [], now, read: 0 })
      if (i < events.length) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }
}
