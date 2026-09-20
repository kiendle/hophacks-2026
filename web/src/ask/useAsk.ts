import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { buildAskRequest, type AskContext } from './context'
import type { AskClient, AskScope, EvidencePost } from './protocol'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  /** User turns: the scope the question was asked about. */
  scope?: AskScope
  /** Assistant turns: cited posts, resolved from the evidence that was sent. */
  citations: EvidencePost[]
  status: 'streaming' | 'done' | 'stopped' | 'error'
}

const newId = () => crypto.randomUUID()

/** One conversation with the chatbot behind `client`. */
export function useAsk(client: AskClient, getContext: () => AskContext | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const conversationId = useRef(newId())
  const inFlight = useRef<AbortController | null>(null)
  const history = useRef<ChatMessage[]>([])
  useLayoutEffect(() => {
    history.current = messages
  })

  const update = (id: string, fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)))

  const send = useCallback(
    async (question: string) => {
      const ctx = getContext()
      if (!ctx || !question.trim() || inFlight.current) return
      const request = buildAskRequest(
        ctx,
        question.trim(),
        history.current
          .filter((m) => m.status === 'done' && m.text)
          .map((m) => ({ role: m.role, content: m.text })),
        conversationId.current,
      )
      const evidence = new Map(request.evidence.map((p) => [p.id, p]))
      const answerId = newId()
      setMessages((ms) => [
        ...ms,
        { id: newId(), role: 'user', text: request.question, scope: request.scope, citations: [], status: 'done' },
        { id: answerId, role: 'assistant', text: '', citations: [], status: 'streaming' },
      ])

      const controller = new AbortController()
      inFlight.current = controller
      try {
        await client.ask(
          request,
          (event) => {
            if (event.type === 'text') update(answerId, (m) => ({ ...m, text: m.text + event.delta }))
            else if (event.type === 'citation') {
              const post = evidence.get(event.postId)
              if (post) update(answerId, (m) => ({ ...m, citations: [...m.citations, post] }))
            } else if (event.type === 'error') throw new Error(event.message)
          },
          controller.signal,
        )
        update(answerId, (m) => ({ ...m, status: 'done' }))
      } catch (error) {
        update(answerId, (m) => ({ ...m, text: m.text || (error instanceof Error ? error.message : 'Unable to reach the assistant.'), status: controller.signal.aborted ? 'stopped' : 'error' }))
      } finally {
        inFlight.current = null
      }
    },
    [client, getContext],
  )

  const stop = useCallback(() => inFlight.current?.abort(), [])
  const busy = messages.at(-1)?.status === 'streaming'

  return { messages, send, stop, busy }
}
