import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { TimeRange } from '../data/types'
import { LiveFollowController } from '../liveFollow'

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
  const state = useRef(new LiveFollowController())
  const latest = useRef({ extent, view, setView, now })
  useLayoutEffect(() => {
    latest.current = { extent, view, setView, now }
  })

  const setUserView = useCallback((v: TimeRange) => {
    const { now, setView } = latest.current
    state.current.setUserView(v, now)
    setFollowing(state.current.following)
    setView(v)
  }, [])

  const toggle = useCallback(() => {
    state.current.following = !state.current.following
    setFollowing(state.current.following)
  }, [])

  useEffect(() => {
    if (!enabled) return
    const { extent, view, setView } = latest.current
    if (!extent || !view) return
    const next = state.current.viewAt(extent, now)
    if (next) setView(next)
  }, [enabled, now])

  const resume = useCallback((resetWidth = false) => {
    state.current.resume(resetWidth)
    setFollowing(true)
    const { extent, now, setView } = latest.current
    const next = state.current.viewAt(extent, now)
    if (next) setView(next)
  }, [])
  const beginInteraction = useCallback(() => {
    const { view, now } = latest.current
    // A render may receive a new tick before its follow effect moves the view.
    // Anchor a live gesture to the edge actually shown, not that newer tick.
    state.current.beginInteraction(state.current.following ? view?.end ?? now : now)
  }, [])
  const endInteraction = useCallback(() => {
    state.current.endInteraction()
    const { extent, now, setView } = latest.current
    const next = state.current.viewAt(extent, now)
    if (next) setView(next)
  }, [])
  return { following, toggle, setUserView, resume, beginInteraction, endInteraction }
}
