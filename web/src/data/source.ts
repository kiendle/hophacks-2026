import { useEffect, useState } from 'react'
import { generateDevSeries } from './devMock'
import type { Series } from './types'

/**
 * Anything that can feed the views. A WebSocket source will accumulate bucket
 * messages and call `onUpdate` with the full series list on each change.
 */
export interface DataSource {
  subscribe(onUpdate: (series: Series[]) => void): () => void
}

/** Development only. Swap for a live source (WebSocket) when the backend is up. */
export const devMockSource: DataSource = {
  subscribe(onUpdate) {
    onUpdate(generateDevSeries())
    return () => {}
  },
}

export function useSeries(source: DataSource): Series[] {
  const [series, setSeries] = useState<Series[]>([])
  useEffect(() => source.subscribe(setSeries), [source])
  return series
}
