import { generateDevSeries } from './devMock'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { Series } from './types'
import type { ReplayCommand, ReplayStatus } from './replayTypes'

/** What the views read: the series so far, plus where the stream has reached. */
export interface StreamSnapshot {
  live?: import('./automationSource').LiveState
  series: Series[]
  /** The moment the stream has reached, or null when all data is already in. */
  now: number | null
  /** True while a stream is still delivering. */
  streaming: boolean
  /** Events scanned upstream, and events kept after filtering. */
  read: number
  kept: number
  note?: string
  start?: number
  end?: number
  run?: string
  status?: ReplayStatus
  speed?: number
  error?: string
}

export interface DataSource {
  subscribe(onUpdate: (snapshot: StreamSnapshot) => void): () => void
  command?: (command: ReplayCommand) => void
  snapshotAt?: (time: number) => Series[]
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

const EMPTY_SNAPSHOT: StreamSnapshot = { series: [], now: null, streaming: false, read: 0, kept: 0 }

/** Buffer background replay updates without redrawing hidden charts. Live sources
 * disconnect while hidden and resume from their saved cursor when shown again. */
export function useStream(source: DataSource, active = true, keepConnected = false): StreamSnapshot {
  const [current, setCurrent] = useState({ source, snapshot: EMPTY_SNAPSHOT })
  const latest = useRef(current)
  const visible = useRef(active)
  useLayoutEffect(() => {
    visible.current = active
    if (active && latest.current.source === source) setCurrent(latest.current)
  }, [source, active])
  const connected = active || keepConnected
  useEffect(() => {
    if (!connected) return
    let disposed = false
    const unsubscribe = source.subscribe(snapshot => {
      if (disposed) return
      latest.current = { source, snapshot }
      if (visible.current) setCurrent(latest.current)
    })
    return () => { disposed = true; unsubscribe() }
  }, [source, connected])
  return current.source === source ? current.snapshot : EMPTY_SNAPSHOT
}
