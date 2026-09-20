import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildAskRequest, type AskContext } from '../src/ask/context'
import { message } from '../src/ask/harnessClient'
import { freshMapState, mapHarnessEvent } from '../src/ask/eventMap'

test('proposal questions bypass chart and archive instructions', () => {
  const context: AskContext = { purpose: 'automation_proposal', topic: 'AI companies', series: [], hidden: new Set(),
    selection: { range: null, subtopics: [] }, mode: 'line', view: { start: 0, end: 1 }, now: 1 }
  const request = buildAskRequest(context, 'Compare OpenAI and Anthropic', [], 'proposal-test')
  const text = message(request)
  assert.match(text, /semantic-automation-config-v2/)
  assert.match(text, /Compare OpenAI and Anthropic/)
  assert.doesNotMatch(text, /Chart context|1970|query_classified_posts/)
})

test('proposal cards and confirmations survive the stream mapping', () => {
  const card = { kind: 'automation_proposal', revision: 2, status: 'ready', configuration: { schema_version: 'semantic-automation-config-v2' } }
  assert.deepEqual(mapHarnessEvent({ type: 'card', card }, freshMapState()).events, [{ type: 'card', card }])
  const mapped = mapHarnessEvent({ type: 'confirm_request', kind: 'automation_proposal', confirmation_id: 'abcdefgh12345678',
    summary: 'Confirm this configuration.', expires_ms: 1234 }, freshMapState())
  assert.equal(mapped.events[0].type, 'confirm')
  if (mapped.events[0].type === 'confirm') assert.equal(mapped.events[0].kind, 'automation_proposal')
})
