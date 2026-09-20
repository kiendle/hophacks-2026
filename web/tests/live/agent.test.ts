import assert from 'node:assert/strict'
import type { PartialOptions } from '@elevenlabs/client'
import { createAgentSession, type AgentConnection } from '../../src/live/agentSession'

let callbacks: PartialOptions
let stopped = 0, volume = 0, muted = false, workspaceStopped = 0, workspaceCalls = 0
const messages: string[] = []
const contexts: string[] = []
let focus = 'Selected Aug 23 to Sep 12 UTC'
const audio: AgentConnection = {
  async endSession() { stopped++ }, setMicMuted(value) { muted = value }, setVolume(value) { volume = value.volume },
  getInputVolume: () => .2, getOutputVolume: () => .7, sendContextualUpdate: text => { contexts.push(text) },
}
const session = createAgentSession({
  fetch: async (url, init) => { assert.equal(url, '/api/live/agent/session'); assert.equal(init?.method, 'POST'); return Response.json({ token: 'temporary' }) },
  connect: async options => { callbacks = options; return audio },
  askWorkspace: async question => { workspaceCalls++; assert.equal(question, 'What does the chart say?'); return 'The chart has 12 posts.' },
  stopWorkspace: () => { workspaceStopped++ }, onTranscript: message => messages.push(message.text),
  workspaceContext: () => focus,
})
assert.equal(session.view().active, false)
assert.equal(volume, 0, 'No sound before an explicit start')
await session.start()
assert.match(contexts[0], /Selected Aug 23 to Sep 12 UTC/)
session.syncWorkspaceContext()
assert.equal(contexts.length, 1, 'Unchanged selection is not resent')
focus = 'Selected Sep 1 to Sep 3 UTC'
session.syncWorkspaceContext()
assert.match(contexts[1], /Selected Sep 1 to Sep 3 UTC/)
focus = 'No selection; visible chart window'
session.syncWorkspaceContext()
assert.match(contexts[2], /No selection/, 'Clearing selection updates an active voice session')
assert.equal(callbacks!.connectionType, 'webrtc')
assert.equal(callbacks!.textOnly, false)
assert.equal(volume, 1, 'Live mode must enable agent output')
callbacks!.onMessage?.({ role: 'user', source: 'user', message: 'Hello', event_id: 1 })
callbacks!.onMessage?.({ role: 'agent', source: 'ai', message: 'Hello there', event_id: 2 })
assert.deepEqual(messages, ['Hello', 'Hello there'])
callbacks!.onMessage?.({ role: 'agent', source: 'ai', message: 'Hello there', event_id: 22 })
assert.deepEqual(messages, ['Hello', 'Hello there'], 'A duplicate agent event must not append or forward another reply')
assert.equal(session.view().transcript.length, 2)
assert.equal(workspaceCalls, 0, 'Transcription alone must not dispatch a second assistant')
const result = await callbacks!.clientTools!.ask_workspace({ question: 'What does the chart say?' })
assert.equal(result, 'The chart has 12 posts.', 'Tool result must return to the voice agent for its spoken response')
callbacks!.onModeChange?.({ mode: 'speaking' })
assert.equal(session.view().state, 'Speaking')
session.toggleMute(); assert(muted); session.toggleMute(); assert(!muted)
callbacks!.onInterruption?.({ event_id: 3 })
assert.equal(session.view().state, 'Listening')
session.stop()
assert.equal(stopped, 1)
callbacks!.onMessage?.({ role: 'agent', source: 'ai', message: 'Stale', event_id: 4 })
assert.equal(messages.length, 2)
assert.equal(workspaceStopped, 0)

let resolveConnection!: (connection: AgentConnection) => void
const pending = createAgentSession({ fetch: async () => Response.json({ token: 'temporary' }),
  connect: () => new Promise(resolve => { resolveConnection = resolve }), askWorkspace: async () => '' })
const starting = pending.start()
await new Promise(resolve => setTimeout(resolve, 0))
pending.stop()
resolveConnection(audio)
await starting
assert.equal(stopped, 2, 'An SDK connection resolving after End must release its microphone')
assert.equal(pending.view().active, false)

const denied = createAgentSession({ fetch: async () => Response.json({ token: 'temporary' }),
  connect: async () => { throw new DOMException('Denied', 'NotAllowedError') }, askWorkspace: async () => '' })
await denied.start()
assert.match(denied.view().note, /Allow microphone/)
assert.equal(denied.view().opening, false)
console.log('PASS: realtime audio enabled, transcript, workspace tool results, mute, interruption, teardown, stale events and microphone denial.')
