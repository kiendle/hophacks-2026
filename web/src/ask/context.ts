import { traction } from '../data/sentiment'
import type { Post, Selection, Series, TimeRange } from '../data/types'
import { ASK_PROTOCOL_VERSION, type AskRequest, type EvidencePost, type IsoRange } from './protocol'
import { computeTrends } from './trend'
import { chartSummaries } from './chartContext'
import { DEFAULT_LINE_INTERVAL } from '../data/config'

/** Evidence posts sent with each question. */
const EVIDENCE_LIMIT = 30

/** What the app knows when the user hits send. */
export interface AskContext {
  purpose?: 'automation_proposal'
  topic: string
  dataset?: AskRequest['dataset']
  intervalMs?: number
  lineDisplay?: 'both' | 'points' | 'trend'
  /** Every tracked subtopic. */
  series: Series[]
  hidden: Set<string>
  selection: Selection
  mode: 'line' | 'bubble'
  /** The visible window: the line view's range, or the bubble view's trailing window. */
  view: TimeRange
  now: number
}

export const isoRange = ({ start, end }: TimeRange): IsoRange => ({
  start,
  end,
  startIso: new Date(start).toISOString(),
  endIso: new Date(end).toISOString(),
})

/** Top posts inside the range for the given subtopics, by traction. */
export function collectEvidence(series: Series[], range: TimeRange, limit = EVIDENCE_LIMIT): EvidencePost[] {
  const posts: { post: Post; subtopic: string }[] = []
  for (const s of series)
    for (const b of s.buckets) {
      const t = b.topPost.time
      if (t >= range.start && t < range.end) posts.push({ post: b.topPost, subtopic: s.id })
    }
  return posts
    .sort((a, b) => traction(b.post) - traction(a.post))
    .slice(0, limit)
    .map(({ post, subtopic }) => ({
      id: post.id,
      subtopic,
      handle: post.handle,
      text: post.text,
      time: new Date(post.time).toISOString(),
      sentiment: Math.round(post.sentiment * 10) / 10,
      likes: post.likes,
      replies: post.replies,
      retweets: post.retweets,
      quotes: post.quotes,
    }))
}

export function buildAskRequest(
  ctx: AskContext,
  question: string,
  history: AskRequest['history'],
  conversationId: string,
): AskRequest {
  const range = ctx.selection.range ?? ctx.view
  // Answers never see past "now", even if a selection reaches further.
  const scopeRange = { start: range.start, end: Math.min(range.end, ctx.now) }
  const focus = ctx.selection.subtopics.length
    ? ctx.series.filter((s) => ctx.selection.subtopics.includes(s.id))
    : ctx.series.filter((s) => !ctx.hidden.has(s.id))

  return {
    version: ASK_PROTOCOL_VERSION,
    purpose: ctx.purpose,
    conversationId,
    question,
    dataset: ctx.dataset,
    chart: { intervalHours: (ctx.intervalMs ?? DEFAULT_LINE_INTERVAL) / 3600000,
      display: ctx.lineDisplay ?? 'both',
      summaries: ctx.mode === 'line' ? chartSummaries(focus, scopeRange, ctx.now,
        ctx.intervalMs ?? DEFAULT_LINE_INTERVAL, ctx.lineDisplay ?? 'both') : [] },
    history,
    scope: {
      range: isoRange(scopeRange),
      rangeSource: ctx.selection.range ? 'selection' : 'view',
      subtopics: [...ctx.selection.subtopics],
    },
    view: { mode: ctx.mode, range: isoRange(ctx.view), now: new Date(ctx.now).toISOString() },
    topic: {
      name: ctx.topic,
      subtopics: ctx.series.map((s) => ({ id: s.id, name: s.name, visible: !ctx.hidden.has(s.id) })),
    },
    trends: computeTrends(focus, scopeRange, ctx.now),
    evidence: collectEvidence(focus, scopeRange),
  }
}
