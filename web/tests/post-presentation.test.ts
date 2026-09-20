import assert from 'node:assert/strict'
import { test } from 'node:test'
import { displayHandle, hoverCardLayout, postExcerpt, scoredCitations } from '../src/postPresentation'
import { PLAYBACK_SPEEDS } from '../src/data/config'
import type { EvidencePost } from '../src/ask/protocol'

test('post cards hide numeric IDs without removing real handles', () => {
  for (const value of ['', '1234567890', 'ID 1234567890', ' author:1234567890 ']) {
    assert.equal(displayHandle(value), '')
  }
  assert.equal(displayHandle('@example'), '@example')
  assert.equal(displayHandle('Jane Doe'), 'Jane Doe')
})

test('long posts have a fixed cutoff without splitting Unicode characters', () => {
  assert.equal(postExcerpt('Short post'), 'Short post')
  assert.equal(postExcerpt('a'.repeat(400)), 'a'.repeat(400))
  assert.equal(postExcerpt('😀'.repeat(401)), '😀'.repeat(400) + '…')
  assert.equal(postExcerpt('a'.repeat(10000)).length, 401)
})

test('hover cards stay inside the chart at its corners and in narrow layouts', () => {
  for (const bounds of [{ width: 800, height: 500 }, { width: 180, height: 160 }]) {
    for (const anchor of [{ x: 0, y: 0 }, { x: bounds.width, y: bounds.height }]) {
      for (const measured of [0, 90, 280, 1500]) {
        const box = hoverCardLayout(anchor, bounds, measured)
        const height = Math.min(measured || box.maxHeight, box.maxHeight)
        assert.ok(box.left >= 8 && box.top >= 8)
        assert.ok(box.left + box.width <= bounds.width - 8)
        assert.ok(box.top + height <= bounds.height - 8)
        assert.ok(box.maxHeight <= 280)
      }
    }
  }
})

test('unscored citation cards are hidden, without changing source evidence', () => {
  const base: EvidencePost = { id: 'scored', subtopic: 'openai', handle: '', text: 'post', time: '',
    sentiment: 5, likes: 0, replies: 0, retweets: 0, quotes: 0 }
  const posts = [base, { ...base, id: 'explicit', scored: true }, { ...base, id: 'unscored', scored: false },
    { ...base, id: 'missing', sentiment: NaN }]
  assert.deepEqual(scoredCitations(posts).map(post => post.id), ['scored', 'explicit'])
  assert.equal(posts.length, 4)
})

test('faster shared playback options stay within both replay servers limits', () => {
  assert.deepEqual(PLAYBACK_SPEEDS.slice(3).map(option => option.value), [86400, 259200, 604800])
  assert.ok(PLAYBACK_SPEEDS.every(option => option.value >= 1 && option.value <= 1_000_000))
  assert.ok(PLAYBACK_SPEEDS.every((option, i) => !i || option.value > PLAYBACK_SPEEDS[i - 1].value))
})
