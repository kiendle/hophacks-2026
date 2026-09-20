import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readSharedSession, shareSessionUrl, type Session } from '../src/app/session'

test('sharing preserves search settings but excludes local identity and history', () => {
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { href: 'https://example.com/?stream=2#old' } })
  const session: Session = { id: 'local-session', query: 'AI & work', title: 'Work reactions',
    terms: ['AI', 'copilot'], subtopics: ['OpenAI'], startedAt: 1, pinned: true, archived: true }
  const link = new URL(shareSessionUrl(session))
  assert.equal(link.search, '')
  const restored = readSharedSession(link.hash)!
  assert.equal(restored.query, session.query)
  assert.equal(restored.title, session.title)
  assert.deepEqual(restored.terms, session.terms)
  assert.deepEqual(restored.subtopics, session.subtopics)
  assert.notEqual(restored.id, session.id)
  assert.equal(restored.pinned, undefined)
  assert.equal(restored.archived, undefined)
})

test('invalid shared settings do not open a workspace', () => {
  for (const hash of ['', '#workspace=%', '#workspace=null', '#workspace=' + encodeURIComponent(JSON.stringify({ query: 'AI', terms: [3], subtopics: [] }))]) {
    assert.equal(readSharedSession(hash), null)
  }
})

test('sharing a live tracker preserves its mode without transferring its running identity', () => {
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { href: 'https://example.com/' } })
  const session: Session = { id: 'local', automationId: 'private-tracker', dataMode: 'live',
    query: 'Transit', terms: ['transit'], subtopics: ['Bus service'], startedAt: 1 }
  const restored = readSharedSession(new URL(shareSessionUrl(session)).hash)!
  assert.equal(restored.dataMode, 'live')
  assert.equal(restored.automationId, undefined)
  assert.notEqual(restored.id, session.id)
})
