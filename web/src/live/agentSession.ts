import type { PartialOptions } from '@elevenlabs/client'

export type AgentState = 'Ended' | 'Connecting' | 'Listening' | 'Thinking' | 'Speaking'
export interface Transcript { id: string; role: 'user' | 'assistant'; text: string }
export interface AgentView {
  session: number
  state: AgentState; active: boolean; opening: boolean; muted: boolean; level: number; note: string
  transcript: Transcript[]
}
export interface AgentConnection {
  endSession(): Promise<void>
  setMicMuted(muted: boolean): void
  setVolume(options: { volume: number }): void
  getInputVolume(): number
  getOutputVolume(): number
  sendContextualUpdate(text: string): void
}
export interface AgentOptions {
  fetch: typeof fetch
  connect(options: PartialOptions): Promise<AgentConnection>
  askWorkspace(question: string): Promise<string | undefined>
  onTranscript?(message: Transcript): void
  stopWorkspace?(): void
  context?(): string
}

export function createAgentSession(options: AgentOptions) {
  let view: AgentView = { session: 0, state: 'Ended', active: false, opening: false, muted: false, level: 0, note: '', transcript: [] }
  let connection: AgentConnection | null = null
  let request: AbortController | null = null
  let generation = 0
  let messageNumber = 0
  let toolPending = false
  let toolGeneration = 0
  let timer: ReturnType<typeof setInterval> | undefined
  const listeners = new Set<(value: AgentView) => void>()
  const update = (patch: Partial<AgentView>) => {
    view = { ...view, ...patch }
    listeners.forEach(listener => listener(view))
  }
  const stop = (note = '') => {
    generation++
    request?.abort()
    request = null
    clearInterval(timer)
    timer = undefined
    const closing = connection
    connection = null
    if (toolPending) options.stopWorkspace?.()
    toolGeneration++
    toolPending = false
    if (closing) void closing.endSession().catch(() => {})
    update({ active: false, opening: false, state: 'Ended', level: 0, muted: false, note })
  }
  const start = async () => {
    if (view.active || view.opening) return
    const turn = ++generation
    const current = () => turn === generation
    request = new AbortController()
    const controller = request
    update({ session: turn, state: 'Connecting', opening: true, note: '', muted: false, transcript: [] })
    const timeout = setTimeout(() => { if (current()) stop('The voice connection timed out. Please try again.') }, 30000)
    try {
      const response = await options.fetch('/api/live/agent/session', { method: 'POST', signal: controller.signal })
      const body = await response.json()
      if (!response.ok || typeof body.token !== 'string') throw new Error(body.error || 'The voice agent could not start.')
      if (!current()) return
      const made = await options.connect({
        conversationToken: body.token, connectionType: 'webrtc', textOnly: false,
        onMessage: ({ message, role, event_id }) => {
          if (!current() || !message.trim() || /^[.\s…]+$/.test(message)) return
          const item: Transcript = { id: `voice-${turn}-${role}-${event_id ?? ++messageNumber}`, role: role === 'user' ? 'user' : 'assistant', text: message }
          const exists = view.transcript.some(row => row.id === item.id)
          update({ transcript: exists ? view.transcript.map(row => row.id === item.id ? item : row) : [...view.transcript, item].slice(-50),
            ...(role === 'user' ? { state: 'Thinking' as const } : {}) })
          options.onTranscript?.(item)
        },
        onAgentResponseCorrection: ({ corrected_agent_response, event_id }) => {
          if (!current()) return
          const item: Transcript = { id: `voice-${turn}-agent-${event_id}`, role: 'assistant', text: corrected_agent_response }
          update({ transcript: view.transcript.map(row => row.id === item.id ? item : row) })
          options.onTranscript?.(item)
        },
        onModeChange: ({ mode }) => { if (current()) update({ state: mode === 'speaking' ? 'Speaking' : toolPending ? 'Thinking' : 'Listening' }) },
        onInterruption: () => {
          if (!current()) return
          if (toolPending) options.stopWorkspace?.()
          toolPending = false
          toolGeneration++
          update({ state: 'Listening' })
        },
        onDisconnect: details => { if (current()) stop(details.reason === 'error' ? 'The voice connection ended unexpectedly. Please reconnect.' : '') },
        onError: () => { if (current()) stop('Voice audio could not continue. Check microphone and speaker access, then reconnect.') },
        clientTools: {
          ask_workspace: async ({ question }: { question: string }) => {
            if (!current()) return 'The voice session ended. Do not start another action.'
            if (toolPending) return 'A workspace request is already running. Wait for its result.'
            if (typeof question !== 'string' || !question.trim()) return 'Ask a specific workspace question.'
            toolPending = true
            const tool = ++toolGeneration
            update({ state: 'Thinking' })
            try {
              const result = await options.askWorkspace(question.slice(0, 4000))
              return current() && tool === toolGeneration ? result || 'The workspace is busy or unavailable. Do not claim an action succeeded.' : 'The user interrupted this request. Do not read its result.'
            } catch {
              return 'The workspace request failed. Tell the user it did not complete.'
            } finally { if (current() && tool === toolGeneration) toolPending = false }
          },
        },
      })
      if (!current()) { await made.endSession(); return }
      connection = made
      // Entering live voice explicitly opts into hearing this conversation.
      made.setVolume({ volume: 1 })
      update({ active: true, opening: false, state: 'Listening' })
      const context = options.context?.()
      if (context) made.sendContextualUpdate(`Prior on-screen conversation, for reference only. Do not respond to this update.\n${context.slice(-10000)}`)
      timer = setInterval(() => {
        if (!current()) return
        const level = view.state === 'Speaking' ? made.getOutputVolume() : view.muted ? 0 : made.getInputVolume()
        update({ level: Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0 })
      }, 80)
    } catch (error) {
      if (current()) stop(error instanceof DOMException && error.name === 'NotAllowedError'
        ? 'Allow microphone access to start live voice.'
        : error instanceof Error ? error.message : 'The voice agent could not start.')
    } finally { clearTimeout(timeout) }
  }
  return {
    view: () => view,
    watch(listener: (value: AgentView) => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    start, stop,
    toggle() { if (view.active || view.opening) stop(); else void start() },
    toggleMute() {
      if (!connection) return
      connection.setMicMuted(!view.muted)
      update({ muted: !view.muted, level: 0 })
    },
  }
}
