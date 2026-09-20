import { useEffect, useRef, useState } from 'react'
import { VoiceConversation } from '@elevenlabs/client'
import { createAgentSession, type AgentView, type Transcript } from './agentSession'

export interface TalkLiveOptions {
  enabled?: boolean
  send(question: string): Promise<string | undefined>
  stop?(): void
  onTranscript?(message: Transcript): void
  context?(): string
  workspaceContext?(): string
  onActiveChange?(active: boolean): void
}
export interface TalkLive extends AgentView {
  available: boolean
  start(): void
  stop(): void
  toggle(): void
  toggleMute(): void
}

export function useTalkLive(options: TalkLiveOptions): TalkLive {
  const latest = useRef(options)
  useEffect(() => { latest.current = options })
  // oxlint-disable-next-line react/refs -- These callbacks are stored for later events, never invoked during render.
  const [session] = useState(() => createAgentSession({
    fetch: (...args) => fetch(...args),
    connect: settings => VoiceConversation.startSession(settings),
    askWorkspace: question => latest.current.send(question),
    stopWorkspace: () => latest.current.stop?.(),
    onTranscript: message => latest.current.onTranscript?.(message),
    context: () => latest.current.context?.() || '',
    workspaceContext: () => latest.current.workspaceContext?.() || '',
  }))
  const [view, setView] = useState(session.view)
  useEffect(() => {
    if (options.enabled === false && (session.view().active || session.view().opening)) session.stop()
  }, [session, options.enabled])
  useEffect(() => { session.syncWorkspaceContext() })
  const [available, setAvailable] = useState(false)
  useEffect(() => session.watch(setView), [session])
  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/live/agent/status', { signal: controller.signal }).then(async response => {
      const status = await response.json()
      if (!controller.signal.aborted) setAvailable(Boolean(response.ok && status.available && navigator.mediaDevices?.getUserMedia))
    }).catch(() => {})
    return () => controller.abort()
  }, [])
  useEffect(() => { latest.current.onActiveChange?.(view.active || view.opening) }, [view.active, view.opening])
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const leaving = () => session.stop()
    const visibility = () => {
      clearTimeout(timer)
      if (document.hidden && (session.view().active || session.view().opening)) {
        timer = setTimeout(() => session.stop('Live voice ended while this page was away.'), 60000)
      }
    }
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('pagehide', leaving)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('pagehide', leaving)
      session.stop()
    }
  }, [session])
  return { ...view, available, start: () => { void session.start() }, stop: () => session.stop(), toggle: session.toggle, toggleMute: session.toggleMute }
}
