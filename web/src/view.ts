import type { TimeRange } from './data/types'

/** Narrowest time window the chart can zoom to. */
export const MIN_WINDOW = 24 * 60 * 60 * 1000

/** Fits a window inside the extent, keeping its width where possible. */
export function clampView(view: TimeRange, extent: TimeRange): TimeRange {
  const width = Math.min(Math.max(view.end - view.start, MIN_WINDOW), extent.end - extent.start)
  const start = Math.min(Math.max(view.start, extent.start), extent.end - width)
  return { start, end: start + width }
}
