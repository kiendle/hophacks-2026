/**
 * How Talk live hears the assistant, without touching the Ask panel.
 *
 * The rule is one brain: a spoken question goes in through the same `send` as a typed one, and the
 * answer that comes back is the same answer the panel draws. So this file does not talk to the
 * server at all. It wraps the Ask client the panel already uses, watches the events going past and
 * repeats them, in the five words core.ts understands, to anyone listening.
 *
 * The integrator writes two lines:
 *
 *     const { messages, send, stop, busy } = useAsk(liveFeed.watch(askClient), getContext)
 *     const live = useTalkLive({ send, events: liveFeed.events })
 *
 * `liveFeed.watch` gives back the same wrapper every time it is handed the same client, so it is
 * safe to call on every render. Without it, or with nothing listening, everything behaves as before:
 * the wrapper adds one function call per event and can fail no request.
 */
import type { LiveEvent } from './core'

export type { LiveEvent }

/** Subscribes to the assistant's turn. Gives back the way to stop listening. */
export type LiveEvents = (listener: (event: LiveEvent) => void) => () => void

/**
 * One event of the Ask stream, as web/src/ask/protocol.ts writes it. Only these four fields are
 * read, so a new event type, or a new field on one, changes nothing here.
 */
export interface AskLikeEvent {
  type: string
  delta?: string
  message?: string
  phase?: string
  title?: string
  summary?: string
}

export type AskLikeListener = (event: AskLikeEvent) => void

export interface AskLikeClient {
  ask(request: never, onEvent: AskLikeListener, signal: AbortSignal): Promise<void>
  /** The turn that follows Yes or No on a confirmation card. Only a client that asks has one. */
  confirm?(
    conversationId: string,
    confirmationId: string,
    approved: boolean,
    onEvent: AskLikeListener,
    signal: AbortSignal,
  ): Promise<void>
}

export interface LiveFeed {
  events: LiveEvents
  /** The assistant has started answering. */
  turnStart(): void
  /** The turn is over, whether it finished, was stopped or failed. */
  turnEnd(): void
  /** One plain sentence about something that went wrong. */
  error(text: string): void
  /** One Ask event, translated and passed on. */
  fromAsk(event: AskLikeEvent): void
  /** The same client, with every event of every turn repeated to the listeners. */
  watch<Client extends AskLikeClient>(client: Client): Client
}

const NO_ANSWER = 'The assistant could not answer.'

export function createLiveFeed(): LiveFeed {
  const listeners = new Set<(event: LiveEvent) => void>()
  const wrapped = new WeakMap<AskLikeClient, AskLikeClient>()
  let open = false // a turn that ends twice, or ends without starting, is still one turn

  const emit = (event: LiveEvent) => {
    for (const listener of [...listeners]) {
      try {
        listener(event)
      } catch {
        /* a listener that throws must never break the answer on the screen */
      }
    }
  }

  const feed: LiveFeed = {
    events: (listener) => {
      listeners.add(listener)
      // Somebody who starts listening in the middle of a turn is told that a turn is running, so
      // they know the chat is busy rather than treating the next thing they hear as a new answer.
      if (open) {
        try {
          listener({ type: 'turn_start' })
        } catch {
          /* a listener that throws must never break the answer on the screen */
        }
      }
      return () => listeners.delete(listener)
    },
    turnStart() {
      if (open) return
      open = true
      emit({ type: 'turn_start' })
    },
    turnEnd() {
      if (!open) return
      open = false
      emit({ type: 'turn_end' })
    },
    error(text: string) {
      emit({ type: 'error', text: String(text || '') })
    },
    fromAsk(event: AskLikeEvent) {
      if (!event || typeof event !== 'object') return
      if (event.type === 'text' && typeof event.delta === 'string') emit({ type: 'text', text: event.delta })
      else if (event.type === 'step' && typeof event.title === 'string') {
        emit({ type: 'step', phase: event.phase === 'end' ? 'end' : 'start', title: event.title })
      } else if (event.type === 'confirm' && typeof event.summary === 'string' && event.summary.trim()) {
        // Somebody with their hands full has to be told that the assistant is waiting on them, or
        // the talk simply goes quiet while a card sits on the screen.
        const asked = event.summary.trim().replace(/[\s.?!]+$/, '')
        emit({ type: 'text', text: `I need a yes before I do that. ${asked}. Press Yes or No in the chat.` })
      } else if (event.type === 'error' && typeof event.message === 'string') {
        emit({ type: 'error', text: event.message })
      }
    },
    watch<Client extends AskLikeClient>(client: Client): Client {
      if (!client) return client
      const already = wrapped.get(client)
      if (already) return already as Client
      // Built on the client rather than copied from it, so anything else it has is still its own
      // method and still works.
      const seen = Object.create(client as object) as Client
      const relay = (turn: (onEvent: AskLikeListener) => Promise<void>) => async (onEvent: AskLikeListener) => {
        feed.turnStart()
        try {
          await turn((event) => {
            feed.fromAsk(event)
            onEvent(event)
          })
        } catch (error) {
          feed.error(error instanceof Error ? error.message : NO_ANSWER)
          throw error
        } finally {
          feed.turnEnd()
        }
      }
      Object.defineProperty(seen, 'ask', {
        value: (request: never, onEvent: AskLikeListener, signal: AbortSignal) =>
          relay((watched) => client.ask(request, watched, signal))(onEvent),
      })
      // The turn that follows Yes on a confirmation card is a turn like any other: it is read out
      // too, and while it runs the spoken side knows the chat is busy.
      const answering = client.confirm
      if (typeof answering === 'function') {
        Object.defineProperty(seen, 'confirm', {
          value: (
            conversationId: string,
            confirmationId: string,
            approved: boolean,
            onEvent: AskLikeListener,
            signal: AbortSignal,
          ) =>
            relay((watched) =>
              answering.call(client, conversationId, confirmationId, approved, watched, signal))(onEvent),
        })
      }
      wrapped.set(client, seen)
      return seen
    },
  }
  return feed
}
