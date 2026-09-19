import { useEffect, useState } from 'react'
import { generateDevSeries } from './devMock'
import type { Series } from './types'

/** What the views read: the series so far, plus where the stream has reached. */
export interface StreamSnapshot {
  series: Series[]
  /** The moment the stream has reached, or null when all data is already in. */
  now: number | null
  /** True while a stream is still delivering. */
  streaming: boolean
  /** Events scanned upstream, and events kept after filtering. */
  read: number
  kept: number
}

export interface DataSource {
  subscribe(onUpdate: (snapshot: StreamSnapshot) => void): () => void
}

/** Firehose posts scanned for each one kept, for the fixture's event readout. */
const EVENTS_PER_KEPT = 38

/** Development only: the whole fixture at once, with no streaming. */
export function createDevMockSource(subtopics?: string[]): DataSource {
  return {
    subscribe(onUpdate) {
      const series = generateDevSeries(subtopics)
      let kept = 0
      for (const s of series) for (const b of s.buckets) kept += b.volume
      onUpdate({ series, now: null, streaming: false, read: kept * EVENTS_PER_KEPT, kept })
      return () => {}
    },
  }
}

export const devMockSource = createDevMockSource()

export function useStream(source: DataSource): StreamSnapshot {
  const [snapshot, setSnapshot] = useState<StreamSnapshot>({
    series: [],
    now: null,
    streaming: false,
    read: 0,
    kept: 0,
  })
  useEffect(() => source.subscribe(setSnapshot), [source])
  return snapshot
}
