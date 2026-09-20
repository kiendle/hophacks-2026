import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { TimeRange } from '../data/types'
import { followView, LiveFollowController } from '../liveFollow'

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
  const [chosenWidth, setChosenWidth] = useState(0)
  const [interacting, setInteracting] = useState(false)
  const state = useRef(new LiveFollowController())
  // Render the live edge for this tick immediately. Waiting for the follow
  // effect first renders the previous time and rebuilds a historical snapshot.
  // viewAt returns null during gestures or historical inspection, preserving
  // the user's selected viewport in those cases.
  const desiredView = enabled && following && !interacting ? followView(extent, now, chosenWidth) ?? view : view
  const start = desiredView?.start, end = desiredView?.end
  // Reuse the view when the follow effect commits the same coordinates.
  const displayView = useMemo(() => start !== undefined && end !== undefined ? { start, end } : null, [start, end])
  const latest = useRef({ extent, view: displayView, setView, now })
  useLayoutEffect(() => {
    latest.current = { extent, view: displayView, setView, now }
  })

  const setUserView = useCallback((v: TimeRange) => {
    const { now, setView } = latest.current
    state.current.setUserView(v, now)
    setFollowing(state.current.following)
    setChosenWidth(state.current.width)
    setView(v)
  }, [])

  const toggle = useCallback(() => {
    if (state.current.following && latest.current.view) latest.current.setView(latest.current.view)
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
    setChosenWidth(state.current.width)
    const { extent, now, setView } = latest.current
    const next = state.current.viewAt(extent, now)
    if (next) setView(next)
  }, [])
  const beginInteraction = useCallback(() => {
    const { view, now, setView } = latest.current
    // Keep the gesture anchor and retained state at the edge actually shown.
    // While dragging, viewAt is suspended and renders use the retained state.
    state.current.beginInteraction(state.current.following ? view?.end ?? now : now)
    setInteracting(true)
    if (view) setView(view)
  }, [])
  const endInteraction = useCallback(() => {
    state.current.endInteraction()
    setInteracting(false)
    const { extent, now, setView } = latest.current
    const next = state.current.viewAt(extent, now)
    if (next) setView(next)
  }, [])
  return { following, displayView, toggle, setUserView, resume, beginInteraction, endInteraction }
}
