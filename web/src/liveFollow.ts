import { BUCKET_MS } from './data/config'
import type { TimeRange } from './data/types'

const DEFAULT_WINDOW = 2 * 24 * 60 * 60 * 1000
const LIVE_SNAP = 1

/** Pure viewport projection, also used by React before the follow effect runs. */
export function followView(extent: TimeRange | null, now: number | null, chosenWidth = 0): TimeRange | null {
  if (!extent || now === null) return null
  const width = chosenWidth || Math.min(Math.max(now - extent.start, BUCKET_MS), DEFAULT_WINDOW)
  return { start: Math.max(extent.start, now - width), end: now }
}

/** Viewport state only: interacting never pauses event delivery. */
export class LiveFollowController {
  following = true
  width = 0
  private interaction: { edge: number | null } | null = null

  beginInteraction(now: number | null) {
    this.interaction = { edge: now }
  }

  endInteraction() {
    this.interaction = null
  }

  setUserView(view: TimeRange, now: number | null) {
    this.width = view.end - view.start
    // During a drag, compare against the live edge the user actually grabbed.
    const edge = this.interaction ? this.interaction.edge : now
    if (edge !== null) this.following = view.end >= edge - LIVE_SNAP
  }

  resume(resetWidth = false) {
    this.following = true
    if (resetWidth) this.width = 0
  }

  viewAt(extent: TimeRange | null, now: number | null): TimeRange | null {
    if (!this.following || this.interaction || !extent || now === null) return null
    return followView(extent, now, this.width)
  }
}
