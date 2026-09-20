import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { buildAskRequest, type AskContext } from './context'
import { failureMessage, voiceFailure } from './failure'
import type { AskCard, AskClient, AskEvent, AskScope, AskStepEvent, EvidencePost } from './protocol'

export interface Confirmation {
  kind?: string
  id: string
  summary: string
  expiresMs: number
  decision?: 'approved' | 'declined'
}
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  scope?: AskScope
  citations: EvidencePost[]
  steps: AskStepEvent[]
  cards: AskCard[]
  confirmation?: Confirmation
  status: 'streaming' | 'done' | 'stopped' | 'error'
}
const newId = () => crypto.randomUUID()
const answer = (id: string): ChatMessage => ({ id, role: 'assistant', text: '', citations: [], steps: [], cards: [], status: 'streaming' })

export function useAsk(client: AskClient, getContext: () => AskContext | null, onSent?: () => void) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const conversationId = useRef(newId())
  const inFlight = useRef<AbortController | null>(null)
  const history = useRef<ChatMessage[]>([])
  useLayoutEffect(() => { history.current = messages }, [messages])
  useEffect(() => () => inFlight.current?.abort(), [])

  const update = useCallback((id: string, fn: (m: ChatMessage) => ChatMessage) => {
    setMessages(ms => ms.map(m => m.id === id ? fn(m) : m))
  }, [])

  const run = useCallback(async (id: string, evidence: Map<string, EvidencePost>, task: (receive: (event: AskEvent) => void, signal: AbortSignal) => Promise<void>) => {
    const controller = new AbortController()
    inFlight.current = controller
    let resultText = ''
    let lastStep = ''
    const resultCards: AskCard[] = []
    let needsConfirmation = ''
    const receive = (event: AskEvent) => {
      if (controller.signal.aborted) return
      if (event.type === 'step' && event.phase === 'start') lastStep = event.title || 'running the query'
      if (event.type === 'error') throw new Error(event.message)
      if (event.type === 'text') resultText += event.delta
      if (event.type === 'card') resultCards.push(event.card)
      if (event.type === 'card' && event.card.kind === 'automation_proposal') {
        setMessages(ms => {
          const previous = ms.flatMap(m => m.cards).filter(card => card.kind === 'automation_proposal' && !card.superseded).at(-1)
          if (!previous || (previous.id === event.card.id && previous.status === event.card.status)) return ms
          return ms.map(m => m.id === id ? m : { ...m,
          cards: m.cards.map(card => card.kind === 'automation_proposal' ? { ...card, superseded: true } : card),
          confirmation: m.confirmation?.kind === 'automation_proposal' && !m.confirmation.decision
            ? { ...m.confirmation, expiresMs: Date.now() } : m.confirmation,
          })
        })
      }
      if (event.type === 'confirm') needsConfirmation = event.summary
      if (event.type === 'posts') { event.posts.forEach(post => evidence.set(post.id, post)); return }
      update(id, m => {
        if (event.type === 'text') return { ...m, text: m.text + event.delta }
        if (event.type === 'citation') {
          const post = evidence.get(event.postId)
          return post && !m.citations.some(p => p.id === post.id) ? { ...m, citations: [...m.citations, post] } : m
        }
        if (event.type === 'step') {
          const existing = m.steps.find(step => step.id === event.id)
          const steps = existing ? m.steps.map(step => step.id === event.id ? { ...step, ...event } : step) : [...m.steps, event]
          return { ...m, steps }
        }
        if (event.type === 'card') {
          const key = event.card.brief_id ?? event.card.id
          const existing = key ? m.cards.findIndex(card => card.kind === event.card.kind && (card.brief_id ?? card.id) === key) : -1
          return { ...m, cards: existing < 0 ? [...m.cards, event.card] : m.cards.map((card, index) => index === existing ? event.card : card) }
        }
        if (event.type === 'confirm') return { ...m, confirmation: { id: event.confirmationId, kind: event.kind, summary: event.summary, expiresMs: event.expiresMs } }
        return m
      })
    }
    try {
      await task(receive, controller.signal)
      update(id, m => ({ ...m, status: controller.signal.aborted ? 'stopped' : 'done', steps: m.steps.map(step => step.phase === 'start' ? { ...step, phase: 'end', ok: false, outcome: 'Finished without a reported result.' } : step) }))
      return JSON.stringify({ answer: resultText, cards: resultCards, confirmation_required: needsConfirmation || undefined, stopped: controller.signal.aborted })
    } catch (error) {
      const detail = failureMessage(error, lastStep)
      update(id, m => ({ ...m,
        text: controller.signal.aborted ? m.text : [m.text, detail].filter(Boolean).join('\n\n'),
        status: controller.signal.aborted ? 'stopped' : 'error',
        steps: m.steps.map(step => step.phase === 'start' ? { ...step, phase: 'end', ok: false, outcome: controller.signal.aborted ? 'Stopped.' : detail } : step),
      }))
      throw new Error(detail)
    } finally { if (inFlight.current === controller) inFlight.current = null }
  }, [update])

  const send = useCallback(async (question: string, options?: { fromVoice?: boolean }) => {
    const ctx = getContext()
    if (!ctx || !question.trim() || inFlight.current) return
    const request = buildAskRequest(ctx, question.trim(), history.current.filter(m => m.status === 'done' && m.text).map(m => ({ role: m.role, content: m.text })), conversationId.current)
    const id = newId()
    setMessages(ms => [...ms,
      ...(!options?.fromVoice ? [{ id: newId(), role: 'user' as const, text: request.question, scope: request.purpose === 'automation_proposal' ? undefined : request.scope, citations: [], steps: [], cards: [], status: 'done' as const }] : []), answer(id)])
    // The request and message already own their scope; clear only the composer selection.
    onSent?.()
    return await run(id, new Map(request.evidence.map(p => [p.id, p])), (receive, signal) => client.ask(request, receive, signal)).catch(voiceFailure)
  }, [client, getContext, onSent, run])

  const confirm = useCallback(async (messageId: string, approved: boolean) => {
    const pending = history.current.find(m => m.id === messageId)?.confirmation
    if (!pending || pending.decision || inFlight.current || !client.confirm) return
    if (pending.expiresMs && pending.expiresMs <= Date.now()) {
      update(messageId, m => ({ ...m, text: `${m.text}\n\nThis confirmation expired. Ask for a fresh one.` }))
      return
    }
    const id = newId()
    update(messageId, m => ({ ...m, confirmation: m.confirmation && { ...m.confirmation, decision: approved ? 'approved' : 'declined' } }))
    setMessages(ms => [...ms, answer(id)])
    await run(id, new Map(), (receive, signal) => client.confirm!(conversationId.current, pending.id, approved, receive, signal)).catch(() => {
      update(messageId, m => ({ ...m, confirmation: m.confirmation && { ...m.confirmation, decision: undefined } }))
    })
  }, [client, run, update])

  const stop = useCallback(() => inFlight.current?.abort(), [])
  const addVoiceMessage = useCallback((message: { id: string; role: 'user' | 'assistant'; text: string }) => {
    setMessages(ms => ms.some(m => m.id === message.id)
      ? ms.map(m => m.id === message.id ? { ...m, text: message.text } : m)
      : [...ms, { ...message, citations: [], steps: [], cards: [], status: 'done' }])
  }, [])
  return { messages, send, stop, confirm, addVoiceMessage, busy: messages.some(m => m.status === 'streaming') }
}
