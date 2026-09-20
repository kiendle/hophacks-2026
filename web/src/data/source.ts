import { useEffect, useState } from 'react'
import type { Series } from './types'
import type { ReplayCommand, ReplayStatus } from './replayTypes'

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
