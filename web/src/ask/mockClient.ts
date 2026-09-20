import type { AskClient, AskRequest, EvidencePost } from './protocol'

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const id = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      clearTimeout(id)
      reject(signal.reason)
    })
  })

const day = (iso: string) =>
  new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', hour12: false })

const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length

/** Stands in for the real chatbot: answers from the request alone, streamed word by word. */
export const mockAskClient: AskClient = {
  async ask(request, onEvent, signal) {
    await wait(350, signal)
    for (const word of answer(request).split(/(?<= )/)) {
      onEvent({ type: 'text', delta: word })
      await wait(18, signal)
    }
    const cited = [...request.evidence].sort((a, b) => a.sentiment - b.sentiment).slice(0, 2)
    for (const post of cited) onEvent({ type: 'citation', postId: post.id })
    onEvent({ type: 'done' })
  },
}

function answer(req: AskRequest): string {
  const names = new Map(req.topic.subtopics.map((s) => [s.id, s.name]))
  const focus = req.scope.subtopics.length ? req.scope.subtopics.map((id) => names.get(id)).join(' and ') : req.topic.name
  const span = `${day(req.scope.range.startIso)} to ${day(req.scope.range.endIso)} UTC`
  const posts = req.evidence
  if (!posts.length) return `I found no posts about ${focus} between ${span}.`

  const avg = mean(posts.map((p) => p.sentiment))
  const bySubtopic = new Map<string, EvidencePost[]>()
  for (const p of posts) bySubtopic.set(p.subtopic, [...(bySubtopic.get(p.subtopic) ?? []), p])
  const lowest = [...bySubtopic].map(([id, ps]) => ({ name: names.get(id), avg: mean(ps.map((p) => p.sentiment)) }))
  lowest.sort((a, b) => a.avg - b.avg)

  const mood = avg < 4.5 ? 'mostly negative' : avg > 6 ? 'mostly positive' : 'mixed'
  const lead = lowest.length > 1 ? ` ${lowest[0].name} drew the most criticism (${lowest[0].avg.toFixed(1)} of 10).` : ''
  const main = req.trends.find((t) => t.subtopic !== 'all') ?? req.trends[0]
  const trend = main
    ? ` Sentiment ${main.sentiment.change < 0 ? 'fell' : 'rose'} ${Math.abs(main.sentiment.change).toFixed(1)} points ` +
      `(${main.sentiment.slopePerDay.toFixed(1)} per day, r2 ${main.sentiment.r2.toFixed(2)}), from ` +
      `${main.sentiment.start.toFixed(1)} to ${main.sentiment.end.toFixed(1)}, while traction moved ` +
      `${main.traction.changeRatio?.toFixed(1) ?? '?'}x.`
    : ''
  return (
    `(Mock answer) You asked: "${req.question}". Looking at ${focus} from ${span}, ` +
    `the ${posts.length} highest-traction posts are ${mood}, averaging ${avg.toFixed(1)} of 10.${lead}${trend} ` +
    `The posts below carry the most negative weight.`
  )
}
