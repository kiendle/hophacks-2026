import assert from 'node:assert/strict'
import { test } from 'node:test'
import { LiveFollowController } from '../src/liveFollow.ts'
import { dragTimeView, MIN_WINDOW, type TimeDrag } from '../src/view.ts'

const DAY = MIN_WINDOW
const extent = { start: 0, end: 10 * DAY }
const view = { start: 8 * DAY, end: 10 * DAY }
const drag: TimeDrag = { mode: 'left', x: 800, width: 1000, extent, view, trailing: false }

test('live resize freezes geometry and follow until release, then preserves the wider span', () => {
  const follow = new LiveFollowController()
  follow.beginInteraction(extent.end)
  const wider = dragTimeView(drag, 600)
  assert.deepEqual(wider, { start: 6 * DAY, end: 10 * DAY })
  for (const now of [11 * DAY, 12 * DAY, 13 * DAY]) {
    follow.setUserView(dragTimeView(drag, 600), now)
    assert.equal(follow.following, true)
    assert.equal(follow.viewAt({ start: 0, end: now }, now), null)
  }
  follow.endInteraction()
  assert.deepEqual(follow.viewAt({ start: 0, end: 13 * DAY }, 13 * DAY), { start: 9 * DAY, end: 13 * DAY })
})

test('panning or resizing the right edge away stays detached; Go Live retains custom width', () => {
  const follow = new LiveFollowController()
  follow.beginInteraction(extent.end)
  const past = dragTimeView({ ...drag, mode: 'move' }, 600)
  follow.setUserView(past, 12 * DAY)
  follow.endInteraction()
  assert.equal(follow.following, false)
  assert.equal(follow.viewAt(extent, 12 * DAY), null)
  follow.resume()
  assert.deepEqual(follow.viewAt({ start: 0, end: 12 * DAY }, 12 * DAY), { start: 10 * DAY, end: 12 * DAY })
  follow.setUserView({ start: 6 * DAY, end: 12 * DAY }, 12 * DAY)
  follow.resume()
  assert.equal(follow.width, 6 * DAY)
  follow.resume(true)
  assert.equal(follow.width, 0)
})

test('dragging back to the captured live edge reattaches even after new data arrives', () => {
  const follow = new LiveFollowController()
  follow.beginInteraction(extent.end)
  follow.setUserView(dragTimeView({ ...drag, mode: 'right' }, 700), 12 * DAY)
  assert.equal(follow.following, false)
  follow.setUserView(dragTimeView({ ...drag, mode: 'right' }, 800), 12 * DAY)
  assert.equal(follow.following, true)
  follow.endInteraction()
  assert.deepEqual(follow.viewAt({ start: 0, end: 12 * DAY }, 12 * DAY), { start: 10 * DAY, end: 12 * DAY })
})

test('short startup extents clamp safely; bubble scrubbing retains its duration', () => {
  const short = { start: 0, end: DAY / 4 }
  for (const mode of ['left', 'right'] as const) {
    const result = dragTimeView({ ...drag, mode, extent: short, view: short }, 0)
    assert.deepEqual(result, short)
  }
  const trail = dragTimeView({ ...drag, trailing: true }, 700)
  assert.equal(trail.end - trail.start, view.end - view.start)
  assert.equal(trail.end, 9 * DAY)
})
