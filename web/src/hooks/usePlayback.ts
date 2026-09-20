import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { BUCKET_MS } from '../data/config'
import type { TimeRange } from '../data/types'

/** Wall-clock ms spent drawing one bucket. */
const MS_PER_BUCKET = 140
/** How much history is on screen when a replay starts. */
const LEAD_IN = 6 * BUCKET_MS
/** A view whose right edge is within this of "now" counts as live. */
const LIVE_SNAP = BUCKET_MS

/**
 * Replays `extent` as if live. The playhead is "now". While the view is live
 * its right edge stays pinned to now, so new points enter on the right; until
 * there is a full window of history it stretches from the first bucket, then
 * scrolls. `playhead` is null when the full series should be shown.
 *
 * User pans and zooms must go through `setUserView`: moving the window away
 * from now stops following, and bringing it back to the edge resumes it.
 */
export function usePlayback(
  extent: TimeRange | null,
  view: TimeRange | null,
  setView: (r: TimeRange) => void,
) {
  const [playing, setPlaying] = useState(false)
  const [playhead, setPlayhead] = useState<number | null>(null)
  const latest = useRef({ extent, view, setView })
  useLayoutEffect(() => {
    latest.current = { extent, view, setView }
  })

  /** Live playhead, ahead of React state between renders. */
  const pos = useRef<number | null>(null)
  /** Window width to follow at, and whether the view tracks now. */
  const follow = useRef({ width: 0, on: true })

  const setUserView = useCallback((v: TimeRange) => {
    follow.current.width = v.end - v.start
    if (pos.current !== null) follow.current.on = v.end >= pos.current - LIVE_SNAP
    latest.current.setView(v)
  }, [])

  const toggle = useCallback(() => {
    const { extent, view } = latest.current
    if (!extent || !view) return
    if (playing) return setPlaying(false)
    if (pos.current === null || pos.current >= extent.end) {
      // Play forward from wherever the view ends; from the top if it ends at the finish.
      pos.current = view.end < extent.end - BUCKET_MS ? Math.max(view.end, extent.start + LEAD_IN) : extent.start + LEAD_IN
      follow.current = { width: view.end - view.start, on: true }
    }
    setPlayhead(pos.current)
    setPlaying(true)
  }, [playing])

  useEffect(() => {
    if (!playing) return
    let raf = 0
    let last = performance.now()
    const step = (now: number) => {
      const { extent, setView } = latest.current
      if (!extent || pos.current === null) return
      const next = pos.current + ((now - last) / MS_PER_BUCKET) * BUCKET_MS
      last = now
      const { width, on } = follow.current
      if (next >= extent.end) {
        pos.current = null
        setPlayhead(null)
        setPlaying(false)
        if (on) setView({ start: Math.max(extent.start, extent.end - width), end: extent.end })
        return
      }
      pos.current = next
      setPlayhead(next)
      if (on) setView({ start: Math.max(extent.start, next - width), end: next })
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [playing])

  return { playing, playhead, toggle, setUserView }
}
