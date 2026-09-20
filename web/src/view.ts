import type { TimeRange } from './data/types'
import { BUCKET_MS } from './data/config'

/** Narrowest time window the chart can zoom to. */
export const MIN_WINDOW = 24 * 60 * 60 * 1000

/** Fits a window inside the extent, keeping its width where possible. */
export function clampView(view: TimeRange, extent: TimeRange): TimeRange {
  const width = Math.min(Math.max(view.end - view.start, MIN_WINDOW), extent.end - extent.start)
  const start = Math.min(Math.max(view.start, extent.start), extent.end - width)
  return { start, end: start + width }
}

export type DragMode = 'move' | 'left' | 'right'
export interface TimeDrag {
  mode: DragMode
  x: number
  width: number
  view: TimeRange
  extent: TimeRange
  trailing: boolean
}

const clamp = (value: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, value))

export function trailingView(t: number, width: number, extent: TimeRange): TimeRange {
  const end = clamp(t, Math.min(extent.start + BUCKET_MS, extent.end), extent.end)
  return { start: end - width, end }
}

/** Convert movement using the scale at pointer-down, never an advancing live scale. */
export function dragTimeView(drag: TimeDrag, x: number): TimeRange {
  const { view: { start, end }, extent } = drag
  const dt = (x - drag.x) / Math.max(1, drag.width) * (extent.end - extent.start)
  if (drag.trailing) return trailingView(end + dt, end - start, extent)
  if (drag.mode === 'move') return clampView({ start: start + dt, end: end + dt }, extent)
  const minWindow = Math.min(MIN_WINDOW, extent.end - extent.start)
  if (drag.mode === 'left') return { start: clamp(start + dt, extent.start, end - minWindow), end }
  return { start, end: clamp(end + dt, start + minWindow, extent.end) }
}
