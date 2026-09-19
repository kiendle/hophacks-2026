/**
 * The contract between the Ask panel and whatever chatbot answers it.
 *
 * The frontend sends one AskRequest per question and receives a stream of
 * AskEvents. Transport is pluggable (see AskClient): HTTP with NDJSON or SSE
 * today, a WebSocket or an in-process mock tomorrow, with no UI changes.
 */

export const ASK_PROTOCOL_VERSION = 1

export interface AskRequest {
  version: typeof ASK_PROTOCOL_VERSION
  /** Stable per conversation, so the backend can keep its own state if it wants. */
  conversationId: string
  /** The user's text, verbatim. */
  question: string
  /** Earlier turns in this conversation, oldest first. */
  history: { role: 'user' | 'assistant'; content: string }[]
  /** What the question is about. */
  scope: AskScope
  /** Where the user was looking, for answers like "in the spike on the right". */
  view: {
    mode: 'line' | 'bubble'
    /** Visible time window. */
    range: IsoRange
    /** The current moment ("now" in a replay). */
    now: string
  }
  topic: {
    name: string
    /** All subtopics the user is tracking, including hidden ones. */
    subtopics: { id: string; name: string; visible: boolean }[]
  }
  /**
   * How sentiment and traction moved across the scope, per subtopic, plus a
   * combined "all" row when several are in scope. Saves the backend from
   * re-deriving the shape of what the user is looking at.
   */
  trends: TrendStat[]
  /**
   * Posts the client already holds for this scope, highest traction first.
   * Optional reading: a backend with its own store should query by `scope`
   * instead, and may treat these as hints.
   */
  evidence: EvidencePost[]
}

export interface AskScope {
  /** Time range to reason about. */
  range: IsoRange
  /** Whether the range was chosen by the user or defaulted to the visible window. */
  rangeSource: 'selection' | 'view'
  /** Subtopic ids to focus on. Empty means all visible subtopics. */
  subtopics: string[]
}

/** Epoch ms plus ISO strings, so both code and models read it easily. */
export interface IsoRange {
  start: number
  end: number
  startIso: string
  endIso: string
}

export interface TrendStat {
  /** Subtopic id, or "all" for the combined row. */
  subtopic: string
  /** Time buckets the fit is based on. */
  buckets: number
  /** Posts in the scope. */
  volume: number
  sentiment: {
    /** Fitted value at the first and last bucket, on the 0 to 10 scale. */
    start: number
    end: number
    /** end - start: the rise or fall across the scope. */
    change: number
    /** Points per day; negative means falling. */
    slopePerDay: number
    mean: number
    /** 0 to 1: how much of the movement the straight line explains. Low means noisy or not linear. */
    r2: number
    min: { value: number; time: string }
    max: { value: number; time: string }
  }
  traction: {
    total: number
    /** Traction of the first and last bucket in scope. */
    first: number
    last: number
    /** last / first, or null when the first bucket had none. */
    changeRatio: number | null
  }
}

export interface EvidencePost {
  id: string
  subtopic: string
  handle: string
  text: string
  time: string
  /** 0 to 10. */
  sentiment: number
  likes: number
  replies: number
  retweets: number
  quotes: number
}

/** One line of the response stream. */
export type AskEvent =
  | { type: 'text'; delta: string }
  /** A post the answer relies on; the UI shows it under the answer. */
  | { type: 'citation'; postId: string }
  | { type: 'done' }
  | { type: 'error'; message: string }

export interface AskClient {
  /**
   * Sends the request and reports events as they arrive. Resolves when the
   * stream ends. Rejects on transport failure or when `signal` aborts.
   */
  ask(request: AskRequest, onEvent: (event: AskEvent) => void, signal: AbortSignal): Promise<void>
}
