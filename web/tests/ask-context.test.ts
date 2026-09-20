import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildAskRequest, type AskContext } from '../src/ask/context'
import { message, harnessAskClient } from '../src/ask/harnessClient'
import { Aggregator } from '../src/data/aggregator'
import { chartData } from '../src/data/chartData'
import type { ReplayEvent } from '../src/data/replayTypes'
import type { Series } from '../src/data/types'
import { failureMessage, voiceFailure } from '../src/ask/failure'

const HOUR = 3600000
const companies = Array.from({ length: 27 }, (_, i) => ({ id: `company${i}`, name: `Company ${i}` }))

test('clearing the composer selection preserves the sent scope but not the next request', () => {
  const context: AskContext = { topic: 'AI', series: [], hidden: new Set(), mode: 'line', now: 10 * HOUR,
    view: { start: 0, end: 10 * HOUR }, selection: { range: { start: HOUR, end: 5 * HOUR }, subtopics: ['openai'] } }
  const sent = buildAskRequest(context, 'What happened here?', [], 'selection-test')
  context.selection.subtopics.length = 0
  context.selection.range = null
  assert.deepEqual(sent.scope.subtopics, ['openai'])
  assert.equal(sent.scope.range.start, HOUR)
  assert.equal(sent.scope.range.end, 5 * HOUR)
  assert.equal(sent.scope.rangeSource, 'selection')
  const next = buildAskRequest(context, 'What about now?', [], 'selection-test')
  assert.deepEqual(next.scope.subtopics, [])
  assert.equal(next.scope.rangeSource, 'view')
  assert.equal(next.scope.range.end, 10 * HOUR)
})

test('chat retains the 27th company, exact selection, dataset filter and replay cutoff', () => {
  const series: Series[] = companies.map(c => ({ ...c, color: '', buckets: [] }))
  const request = buildAskRequest({ topic: 'AI', dataset: { source: 'twitter_archive', keywords: ['safety'] },
    series, hidden: new Set(companies.slice(0, 26).map(c => c.id)),
    selection: { subtopics: ['company26'], range: { start: HOUR, end: 10 * HOUR } }, mode: 'line',
    view: { start: 0, end: 24 * HOUR }, now: 8 * HOUR }, 'Why dip?', [], 'test')
  const payload = JSON.parse(message(request).split('\n').at(-1)!)
  assert.deepEqual(payload.scope.company_ids, ['company26'])
  assert.deepEqual(payload.dataset_keywords, ['safety'])
  assert.equal(payload.companies.length, 27)
  assert.equal(payload.scope.date_from, new Date(HOUR).toISOString())
  assert.equal(payload.scope.date_to, new Date(8 * HOUR).toISOString())
  assert.equal(payload.view.replay_cutoff, payload.scope.date_to)
  assert.equal(payload.trends, undefined, 'Do not send old fitted four-hour bucket values')
  assert(message(request).includes("An explicit date in the user's question overrides the chart dates and cutoff"))
  assert(!message(request).includes('Never query beyond replay_cutoff'))
})

test('voice receives the same failure detail as chat, including the interrupted step', () => {
  const detail = failureMessage(new TypeError('network error'), 'Reading posts behind the chart')
  assert.match(detail, /connection to the workspace was interrupted while reading posts/)
  const voice = JSON.parse(voiceFailure(new Error(detail)))
  assert.equal(voice.completed, false)
  assert.equal(voice.error, detail)
  assert.equal(failureMessage(new Error('The query timed out after 30 seconds.')), 'The query timed out after 30 seconds.')
})

test('chat readings are the displayed twelve-hour points and trailing trend, not fitted endpoints', () => {
  const agg = new Aggregator([companies[0]])
  for (const [i, score] of [1, 3, 8, 2, 9, 10].entries()) {
    const event = { id: String(i), postId: String(i), kind: 'post', t: (i * 4 + 1) * HOUR,
      postTime: (i * 4 + 1) * HOUR, text: 'test', authorId: '', contentVersion: '', observedAt: 0,
      grades: [{ company: 'company0', score, choice: 'positive', confidence: 1, probabilities: {} }] } as ReplayEvent
    agg.add(event)
  }
  const series = agg.snapshot(24 * HOUR)
  const view = { start: 0, end: 24 * HOUR }
  const request = buildAskRequest({ topic: 'AI', series, hidden: new Set(),
    selection: { subtopics: [], range: null }, mode: 'line', view, now: view.end,
    intervalMs: 12 * HOUR, lineDisplay: 'both' }, 'What happened?', [], 'test')
  const displayed = chartData(series[0], view, view.end, 12 * HOUR)
  const readings = request.chart!.summaries
  assert.equal(readings[0].line, 'points')
  assert.equal(readings[0].low.value, Math.round(Math.min(...displayed.points.map(p => p.s)) * 10000) / 10000)
  assert.equal(readings[0].low.date_from, new Date(0).toISOString())
  assert.equal(readings[0].low.date_to, new Date(12 * HOUR).toISOString())
  assert.equal(readings[1].line, '24h trend')
  const trendHigh = displayed.trend.reduce((a, b) => a.s >= b.s ? a : b)
  assert.equal(readings[1].high.date_to, new Date(trendHigh.t).toISOString())
  assert.equal(readings[1].high.date_from, new Date(trendHigh.t - 24 * HOUR).toISOString())
})

test('a server restart renews an expired chat once while preserving the full query context', async () => {
  const request = buildAskRequest({ topic: 'AI', series: [], hidden: new Set(),
    selection: { subtopics: [], range: null }, mode: 'line', view: { start: 0, end: HOUR }, now: HOUR },
    'Which posts?', [], 'expired-session-test')
  const previousFetch = globalThis.fetch
  const bodies: string[] = []
  let created = 0
  globalThis.fetch = async (url, options) => {
    if (url === '/api/sessions') return Response.json({ session_id: ++created === 1 ? 'expired' : 'renewed' })
    bodies.push(String(options!.body))
    return String(url).includes('/expired/') ? Response.json({ error: 'No such session.' }, { status: 404 })
      : new Response('data: {"type":"done"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } })
  }
  try {
    await harnessAskClient.ask(request, () => {}, new AbortController().signal)
    assert.equal(created, 2)
    assert.equal(bodies.length, 2)
    assert.equal(bodies[0], bodies[1])
  } finally { globalThis.fetch = previousFetch }
})
