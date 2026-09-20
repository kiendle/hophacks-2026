import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { BUCKET_MS } from '../data/config'
import type { TimeRange } from '../data/types'

/** A view whose right edge is within this of "now" counts as live. */
const LIVE_SNAP = 1
/** How much history a live view shows before it starts scrolling. */
const DEFAULT_WINDOW = 2 * 24 * 60 * 60 * 1000

/**
 * Keeps the view pinned to a stream's `now` while the user leaves it there.
 * Panning away stops following; returning to the right edge resumes it. The
 * stream keeps arriving either way, so this only moves the window.
 */
export function useLiveFollow(
  extent: TimeRange | null,
  view: TimeRange | null,
  setView: (r: TimeRange) => void,
  now: number | null,
  enabled: boolean,
) {
  const [following, setFollowing] = useState(true)
  const state = useRef({ width: 0, on: true })
  const latest = useRef({ extent, view, setView, now })
  useLayoutEffect(() => {
    latest.current = { extent, view, setView, now }
  })

  const setUserView = useCallback((v: TimeRange) => {
    const { now, setView } = latest.current
    state.current.width = v.end - v.start
    if (now !== null) {
      state.current.on = v.end >= now - LIVE_SNAP
      setFollowing(state.current.on)
    }
    setView(v)
  }, [])

  const toggle = useCallback(() => {
    state.current.on = !state.current.on
    setFollowing(state.current.on)
  }, [])

  useEffect(() => {
    if (!enabled || now === null || !state.current.on) return
    const { extent, view, setView } = latest.current
    if (!extent || !view) return
    // Until the user sizes the window, grow with the data up to a default span.
    const width = state.current.width || Math.min(Math.max(now - extent.start, BUCKET_MS), DEFAULT_WINDOW)
    setView({ start: Math.max(extent.start, now - width), end: now })
  }, [enabled, now])

  const resume = useCallback(() => {
    state.current.on = true
    state.current.width = 0
    setFollowing(true)
  }, [])
  return { following, toggle, setUserView, resume }
}
