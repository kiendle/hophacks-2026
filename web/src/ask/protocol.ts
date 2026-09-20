/**
 * The contract between the Ask panel and whatever chatbot answers it.
 *
 * The frontend sends one AskRequest per question and receives a stream of
 * AskEvents. Transport is pluggable (see AskClient): HTTP with NDJSON or SSE
 * today, a WebSocket or an in-process mock tomorrow, with no UI changes.
 */

export const ASK_PROTOCOL_VERSION = 1

export interface AskRequest {
  purpose?: 'automation_proposal'
  version: typeof ASK_PROTOCOL_VERSION
  /** Stable per conversation, so the backend can keep its own state if it wants. */
  conversationId: string
  /** The user's text, verbatim. */
  question: string
  /** Exact replay filter and display settings, retained when asking the local data tools. */
  dataset?: { source: 'twitter_archive' | 'bluesky_live'; keywords: string[] }
  chart?: { intervalHours: number; display: 'both' | 'points' | 'trend'; summaries: import('./chartContext').ChartSummary[] }
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
  /**
   * False for a post the app did not score itself, such as one the backend found mid-answer: its
   * `sentiment` is only a placeholder and no number is shown for it.
   */
  scored?: boolean
}

/**
 * One line of the response stream.
 *
 * The first four members are the original protocol. The rest were added for the harness backend and
 * are optional to produce: a client that sends none of them behaves exactly as before, and `useAsk`
 * still ignores an event type it does not know.
 */
export type AskEvent =
  | { type: 'text'; delta: string }
  /** A post the answer relies on; the UI shows it under the answer. */
  | { type: 'citation'; postId: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
  /** One real thing the assistant did, opened by `start` and closed by `end` with the same id. */
  | AskStepEvent
  /** Posts the backend found while answering, so citations to them resolve. */
  | { type: 'posts'; posts: EvidencePost[] }
  /** Something to draw under the answer. */
  | { type: 'card'; card: AskCard }
  /** A decision only the user may make. Answered with `confirmDecision`, never by the model. */
  | { type: 'confirm'; confirmationId: string; summary: string; expiresMs: number; kind?: string }

export interface AskStepEvent {
  type: 'step'
  phase: 'start' | 'end'
  /** Matched across the two phases: parallel calls come back out of order. */
  id: string
  /** Which step of the turn this is, counting from one. */
  n?: number
  /** What is being done, in plain words. */
  title?: string
  /** The reason the assistant gave for doing it, in its own words. */
  why?: string
  /** What came back, written from the real result. Only on `end`. */
  outcome?: string
  ok?: boolean
  /** How long the step took, in milliseconds. Only on `end`. */
  ms?: number
  /** Plain lines for the details panel. */
  facts?: StepFact[]
  /** The exact request that was sent, shown only inside an open details panel. */
  request?: { tool: string; input: unknown }
}

export interface StepFact {
  label: string
  value: string
}

/**
 * A card under the answer. `kind` says how to draw it and the rest of the object belongs to that
 * kind, so an unknown kind is simply not drawn. Everything in one is treated as untrusted text.
 */
export interface AskCard {
  kind: string
  [key: string]: unknown
}

/** One labelled line of the draft card. Never JSON. */
export interface DraftLine {
  label: string
  value: string
  groups?: { name: string; description: string }[]
}

/** One example post on a preview card, exactly as the backend found it. */
export interface ExamplePost {
  id: string
  /** A day like 2026-09-10, or a live clock label like 19:05. */
  day: string
  likes: number | null
  /** The short form the card shows first. */
  body: string
  /** What "Show full post" opens, or null when the backend sent none. */
  fullText: string | null
  /** Where the post lives. Checked again before anyone can click it. */
  url: string | null
}

export interface AskClient {
  /**
   * Sends the request and reports events as they arrive. Resolves when the
   * stream ends. Rejects on transport failure or when `signal` aborts.
   */
  ask(request: AskRequest, onEvent: (event: AskEvent) => void, signal: AbortSignal): Promise<void>
  /**
   * Answers a `confirm` event with the user's own decision and streams the turn that follows.
   * Only a client that sends `confirm` events needs this.
   */
  confirm?(
    conversationId: string,
    confirmationId: string,
    approved: boolean,
    onEvent: (event: AskEvent) => void,
    signal: AbortSignal,
  ): Promise<void>
}
