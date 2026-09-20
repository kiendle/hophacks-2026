import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Aggregator } from '../src/data/aggregator'
import { chartData } from '../src/data/chartData'
import { buildAskRequest } from '../src/ask/context'
import { message } from '../src/ask/harnessClient'
import type { ReplayEvent } from '../src/data/replayTypes'
import { ingestionHealth, type LiveState } from '../src/data/automationSource'

test('ingestion health distinguishes zero matches from a stopped or stale source', () => {
  const state: LiveState = { enabled: true, worker_state: 'idle', max_usd: .1, counts: {},
    config: { targets: [], keyword_filter: { groups: [] } },
    source: { status: 'connected', ingestion: { window_seconds: 300, posts_last_5m: 469, events_last_5m: 3241,
      as_of: '2026-09-20T13:00:00.000Z', last_post_at: '2026-09-20T12:59:59.000Z', last_event_at: '2026-09-20T12:59:59.000Z' } } }
  assert.equal(ingestionHealth(state).label, 'Receiving posts')
  assert.equal(ingestionHealth({ ...state, enabled: false }).label, 'Tracking stopped')
  assert.equal(ingestionHealth(state, 'network error').tone, 'warning')
  assert.equal(ingestionHealth({ ...state, source: { ...state.source, status: 'retrying' } }).label, 'Reconnecting to Bluesky')
  state.source.ingestion!.as_of = '2026-09-20T13:01:00.000Z'
  assert.equal(ingestionHealth(state).label, 'No recent stream activity')
})

const t = Date.UTC(2026, 8, 20, 12)
const grade = { company: 'transit', choice: 'positive' as const, score: 9.75, confidence: .96,
  probabilities: { positive: .96, negative: .01, neutral: .01, mixed: .01, insufficient_evidence: .01 } }
const event = (id: string, offset: number, extra: Partial<ReplayEvent> = {}): ReplayEvent => ({ id, kind: 'post', t: t + offset,
  postId: 'at://did:plc:example/app.bsky.feed.post/post', postTime: t, text: 'Transit is great', authorId: 'did:plc:example',
  contentVersion: id, observedAt: t + offset, grades: [grade], ...extra })

test('live minute points grow with observations and edits never manufacture publications', () => {
  const acc = new Aggregator([{ id: 'transit', name: 'Transit' }])
  acc.add(event('post:1', 1000))
  acc.add(event('post:2', 61_000, { publication: false }))
  acc.add(event('like:3', 62_000, { kind: 'like', delta: 2, opening: false }))
  acc.add(event('like:3', 62_000, { kind: 'like', delta: 2, opening: false }))
  const series = acc.snapshot(t + 121_000)[0]
  const plotted = chartData(series, { start: t, end: t + 121_000 }, t + 121_000, 60_000)
  assert.equal(plotted.points[0].v, 1)
  assert.equal(plotted.points[1].v, 0)
  assert.equal(plotted.points[1].bucket.traction, 2)
  assert.equal(plotted.points[1].s, 9.75)
})

test('live chat routes to this automation and never asks the archive to explain it', () => {
  const request = buildAskRequest({ topic: 'Transit', dataset: { source: 'bluesky_live', keywords: ['transit'], automationId: 'track-123' },
    series: [], hidden: new Set(), selection: { range: null, subtopics: [] }, mode: 'line',
    view: { start: t, end: t + 60_000 }, now: t + 60_000, intervalMs: 60_000 }, 'Why did it move?', [], 'test')
  const text = message(request)
  assert.match(text, /get_live_tracking_data/)
  assert.match(text, /"automation_id":"track-123"/)
  assert.doesNotMatch(text, /query_classified_posts/)
})
