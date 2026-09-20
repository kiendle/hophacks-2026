/**
 * The harness chat protocol, mapped one to one onto Ask events.
 *
 * The backend (harness/claude_runner.py) streams server sent events of its own: delta, progress,
 * step, preview, spec, card, confirm_request, message, error, done. This file turns each of them
 * into the Ask events the panel draws, and nothing else: no network, no DOM, no state beyond the one
 * flag the `message` rule needs. Everything arriving here was written by a model or came out of a
 * post, so every field is read defensively and nothing may throw.
 */
import { projectLines } from './facts'
import { oneLine, plainText, readable } from './plain'
import type { AskCard, AskEvent, EvidencePost, ExamplePost, StepFact } from './protocol'

const asObject = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : [])

const asText = (value: unknown, limit = 2000): string => (typeof value === 'string' ? value.slice(0, limit) : '')

/** A real number, or null: a boolean, a blank string and anything unparsable are not numbers. */
function asNumber(value: unknown): number | null {
  if (typeof value === 'boolean' || value === null || value === undefined || value === '') return null
  const found = Number(value)
  return Number.isFinite(found) ? found : null
}

/** How many examples a preview keeps, and how many of them are cited under the answer. */
export const PREVIEW_EXAMPLES = 6
export const PREVIEW_CITATIONS = 2
const FULL_TEXT_LIMIT = 2000
/** The server writes confirmation ids itself; anything else cannot be answered, so it is dropped. */
const CONFIRMATION = /^[A-Za-z0-9_-]{16}$/

export interface MapState {
  /** Whether any streamed text arrived this turn: the final `message` only speaks when none did. */
  sawDelta: boolean
}

export const freshMapState = (): MapState => ({ sawDelta: false })

export interface Mapped {
  events: AskEvent[]
  state: MapState
}

function facts(value: unknown): StepFact[] | undefined {
  const lines = asArray(value)
    .map((line) => ({ label: oneLine(asObject(line).label, 60), value: oneLine(asObject(line).value, 240) }))
    .filter((line) => line.value)
  return lines.length ? lines : undefined
}

/** One example post, however the backend found it. A post is someone else's words and is never reworded. */
function toExample(value: unknown): ExamplePost {
  const post = asObject(value)
  const body = asText(post.body ?? post.text, FULL_TEXT_LIMIT)
  const full = asText(post.full_text, FULL_TEXT_LIMIT)
  return {
    id: asText(post.id ?? post.uri, 300),
    day: oneLine(post.day ?? post.time_label, 24),
    likes: asNumber(post.like_count ?? post.likes),
    body,
    fullText: full || null,
    url: typeof post.url === 'string' ? post.url.slice(0, 500) : null,
  }
}

/** A preview example as evidence, so a citation to it resolves against the same map as the rest. */
function toEvidence(post: ExamplePost): EvidencePost {
  return {
    id: post.id,
    subtopic: '',
    handle: '',
    text: post.fullText || post.body,
    time: post.day,
    // Nothing scored these posts, so the number is a placeholder and `scored` says so.
    sentiment: 5,
    likes: post.likes ?? 0,
    replies: 0,
    retweets: 0,
    quotes: 0,
    scored: false,
  }
}

function previewCard(event: Record<string, unknown>, examples: ExamplePost[]): AskCard {
  return {
    kind: 'preview',
    title: readable(event.title, 120) || 'What we found',
    total: asNumber(event.total ?? event.matched),
    note: readable(event.note, 300),
    exact: typeof event.exact === 'boolean' ? event.exact : null,
    perDay: asArray(event.per_day)
      .slice(0, 62)
      .map((row) => ({ day: oneLine(asObject(row).day, 24), count: asNumber(asObject(row).count) ?? 0 }))
      .filter((row) => row.day),
    examples,
  }
}

/**
 * One backend event in, the Ask events it becomes out, plus the state the next call needs. Pure:
 * the same event and state always give the same result.
 */
export function mapHarnessEvent(raw: unknown, state: MapState): Mapped {
  const event = asObject(raw)
  const type = typeof event.type === 'string' ? event.type : ''
  const none: Mapped = { events: [], state }

  switch (type) {
    case 'delta': {
      const delta = asText(event.text)
      if (!delta) return none
      return { events: [{ type: 'text', delta }], state: { ...state, sawDelta: true } }
    }

    case 'message': {
      // The whole answer, sent once at the end. It only speaks when nothing was streamed.
      const text = asText(event.text, 8000)
      if (state.sawDelta || !text.trim()) return none
      return { events: [{ type: 'text', delta: text }], state }
    }

    case 'error':
      return { events: [{ type: 'error', message: readable(event.text, 400) || 'Something went wrong.' }], state }

    case 'step': {
      const id = asText(event.id, 120)
      if (!id) return none
      if (event.phase === 'end') {
        return {
          events: [
            {
              type: 'step',
              phase: 'end',
              id,
              ok: event.ok !== false,
              outcome: readable(event.outcome, 400) || 'Done.',
              ms: asNumber(event.ms) ?? undefined,
            },
          ],
          state,
        }
      }
      if (event.phase !== 'start') return none
      const detail = asObject(event.detail)
      return {
        events: [
          {
            type: 'step',
            phase: 'start',
            id,
            n: asNumber(event.n) ?? undefined,
            title: readable(event.title, 160) || 'Working on it',
            why: readable(event.why, 240) || undefined,
            facts: facts(event.facts),
            // The exact request, shown only inside an open details panel.
            request: { tool: asText(detail.tool ?? event.tool, 120), input: detail.input ?? null },
          },
        ],
        state,
      }
    }

    case 'preview': {
      const examples = asArray(event.examples).slice(0, PREVIEW_EXAMPLES).map(toExample).filter((post) => post.body || post.fullText)
      const evidence = examples.filter((post) => post.id).map(toEvidence)
      const events: AskEvent[] = [{ type: 'card', card: previewCard(event, examples) }]
      if (evidence.length) {
        events.push({ type: 'posts', posts: evidence })
        for (const post of evidence.slice(0, PREVIEW_CITATIONS)) events.push({ type: 'citation', postId: post.id })
      }
      return { events, state }
    }

    case 'spec':
      // The draft, as labelled plain lines. The reader never meets the JSON behind it.
      return { events: [{ type: 'card', card: { kind: 'draft', lines: projectLines(event.spec) } }], state }

    case 'card': {
      const card = asObject(event.card)
      const kind = oneLine(card.kind, 40)
      if (!kind) return none
      return { events: [{ type: 'card', card: { ...card, kind } }], state }
    }

    case 'confirm_request': {
      const confirmationId = asText(event.confirmation_id, 64)
      if (!CONFIRMATION.test(confirmationId)) return none
      return {
        events: [
          {
            type: 'confirm',
            confirmationId,
            kind: event.kind === 'automation_proposal' || event.kind === 'brief_telegram' ? event.kind : undefined,
            summary: plainText(asText(event.summary, 1200)).trim(),
            expiresMs: asNumber(event.expires_ms) ?? 0,
          },
        ],
        state,
      }
    }

    // `progress` is one line about a tool call that the activity list already shows in full, and
    // `done` is the client's own business: it says the turn ended, not what to draw.
    default:
      return none
  }
}

/**
 * Server sent events reader. `feed(chunk)` returns the payloads that became complete with this
 * chunk: a frame may be split anywhere, a chunk may hold several, and a comment line or an
 * unparsable payload is dropped.
 */
export function createFrameParser(): (chunk: string) => unknown[] {
  let buffer = ''
  return function feed(chunk: string) {
    buffer += chunk
    const events: unknown[] = []
    for (;;) {
      buffer = buffer.replace(/\r\n/g, '\n')
      const end = buffer.indexOf('\n\n')
      if (end === -1) break
      const frame = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      for (const raw of frame.split('\n')) {
        const line = raw.replace(/\r$/, '')
        if (!line || line.startsWith(':') || !line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          events.push(JSON.parse(payload))
        } catch {
          // a half written frame that still ended in a blank line: ignore it
        }
      }
    }
    return events
  }
}
