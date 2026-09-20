export type SentimentChoice = 'positive' | 'negative' | 'neutral' | 'mixed' | 'insufficient_evidence'

export interface Grade {
  company: string
  choice: SentimentChoice
  score: number | null
  confidence: number
  probabilities: Record<SentimentChoice, number>
}

/** One source event, even when several companies match it. */
export interface ReplayEvent {
  id: string
  kind: 'post' | 'like'
  t: number
  postId: string
  postTime: number | null
  text: string
  authorId: string | null
  contentVersion: string
  observedAt: number
  grades: Grade[]
  delta?: number
  opening?: boolean
  /** A live text edit is an observation, not a new publication. */
  publication?: boolean
}

export interface ReplayDataset {
  version: 1
  start: number
  end: number
  companies: { id: string; name: string }[]
  counts: { posts: number; likes: number }
  events: ReplayEvent[]
}

export type ReplayStatus = 'loading' | 'playing' | 'paused' | 'complete' | 'error'
export type ReplayCommand = { type: 'pause' | 'resume' | 'restart' } | { type: 'speed'; speed: number }
export type ReplayMessage =
  | { type: 'init'; run: string; start: number; end: number; companies: ReplayDataset['companies']; speed: number }
  | { type: 'batch'; run: string; sequence: number; now: number; events: ReplayEvent[]; status: ReplayStatus; speed: number }
  | { type: 'error'; message: string }

export const DEFAULT_SPEED = 14_400
export const DEFAULT_COMPANIES = ['openai', 'anthropic', 'google', 'xai', 'nvidia']
