import { useEffect, useState } from 'react'
import type { AskCard, AskStepEvent, DraftLine, ExamplePost } from '../ask/protocol'
import type { Confirmation } from '../ask/useAsk'
import { postLink } from '../ask/links'
import type { Brief } from '../brief/types'
import './tool-cards.css'

const words = (value: unknown) => typeof value === 'string' ? value : ''
const rows = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.filter(x => x && typeof x === 'object') : []

function activityTitle(step: AskStepEvent) {
  const title = step.title || 'Working'
  if (title.startsWith('Searching X/Twitter')) return 'Searching X/Twitter'
  if (title.startsWith('Searching') && title.includes('Bluesky')) return 'Searching Bluesky'
  return title
}

export function ToolActivity({ steps }: { steps: AskStepEvent[] }) {
  if (!steps.length) return null
  return <section className="tool-activity" aria-label="Tool activity" aria-live="polite">
    <div className="tool-heading">Activity</div>
    {steps.map(step => <div className="tool-step" key={step.id} data-state={step.phase === 'start' ? 'running' : step.ok ? 'done' : 'error'}>
      <div className="tool-step-title"><span className="tool-step-dot" />{activityTitle(step)}<span className="tool-time">{step.phase === 'start' ? 'Running' : step.ms !== undefined ? `${(step.ms / 1000).toFixed(1)}s` : ''}</span></div>
      {(step.why || step.outcome || step.request || step.facts?.length || (step.title && step.title !== activityTitle(step))) && <details><summary>Details</summary>
        {step.title && step.title !== activityTitle(step) && <p>{step.title}</p>}
        {step.why && <p className="tool-muted">{step.why}</p>}
        {step.outcome && <p>{step.outcome}</p>}
        {step.facts?.map((fact, i) => <p key={i}><strong>{fact.label}: </strong>{fact.value}</p>)}
        {step.request && <><p>{step.request.tool}</p><pre>{JSON.stringify(step.request.input, null, 2)}</pre></>}
      </details>}
    </div>)}
  </section>
}

export function ConfirmationCard({ value, busy, decide }: { value: Confirmation; busy: boolean; decide: (approved: boolean) => void }) {
  const [expired, setExpired] = useState(() => Boolean(value.expiresMs && value.expiresMs <= Date.now()))
  useEffect(() => {
    if (!value.expiresMs) return
    const timer = setTimeout(() => setExpired(true), Math.max(0, value.expiresMs - Date.now()))
    return () => clearTimeout(timer)
  }, [value.expiresMs])
  return <section className="tool-card"><h4>Confirm project</h4><p>{value.summary}</p>
    {value.decision ? <p>{value.decision === 'approved' ? 'Confirmed.' : 'Cancelled.'}</p>
      : expired ? <p>This confirmation expired. Ask for a new one.</p>
      : <div className="tool-actions"><button className="primary-btn" disabled={busy} onClick={() => decide(true)}>Confirm</button><button className="tool-button" disabled={busy} onClick={() => decide(false)}>Cancel</button></div>}
  </section>
}

function BriefCard({ card }: { card: AskCard }) {
  const id = words(card.brief_id ?? card.id)
  const [brief, setBrief] = useState<Brief | null>(null)
  const [delivery, setDelivery] = useState<'idle' | 'sending' | 'sent'>('idle')
  const [deliveryError, setDeliveryError] = useState('')
  const sendToTelegram = async () => {
    if (delivery !== 'idle') return
    setDelivery('sending')
    setDeliveryError('')
    try {
      const response = await fetch(`/api/briefs/${id}/telegram`, { method: 'POST' })
      const result = await response.json().catch(() => null)
      if (!result || typeof result !== 'object') {
        throw new Error(response.status === 404 || response.status === 405
          ? 'Telegram delivery is unavailable on this server. Restart the app server and try again.'
          : 'Telegram delivery could not be confirmed. Check your Telegram chat before trying again.')
      }
      if (!response.ok || !result.sent) throw new Error(words(result.error) || 'Could not send this brief.')
      setDelivery('sent')
    } catch (error) {
      setDelivery('idle')
      setDeliveryError(error instanceof Error ? error.message : 'Delivery could not be confirmed. Check Telegram.')
    }
  }
  const [status, setStatus] = useState('Preparing your audio brief…')
  useEffect(() => {
    if (!/^\d{8}-\d{6}$/.test(id)) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let attempts = 0
    const poll = async () => {
      try {
        const response = await fetch(`/api/briefs/${id}`, { signal: controller.signal })
        if (!response.ok) throw new Error(response.status === 404 ? 'This brief could not be found.' : 'The brief service is unavailable. Please try again.')
        const next = await response.json() as Brief
        if (controller.signal.aborted) return
        setBrief(next)
        setStatus(next.status === 'ready' ? 'Ready to listen.' : next.step || 'Preparing your audio brief…')
        if (next.status !== 'working') return
        if (++attempts >= 100) { setStatus('This is taking longer than expected. Ask for the brief status.'); return }
        timer = setTimeout(poll, 3000)
      } catch (error) { if (!controller.signal.aborted) setStatus(error instanceof Error ? error.message : 'Unable to load the brief.') }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [id])
  const audio = brief?.audio?.full
  const source = /^\d{8}-\d{6}$/.test(id) && typeof audio === 'string' && /^[a-z0-9-]+\.mp3$/.test(audio) ? `/api/briefs/${id}/audio/${audio}` : null
  return <section className="tool-card"><h4>{brief?.title || words(card.title) || 'Your audio brief'}</h4><p role="status">{status}</p>
    {source && <audio controls preload="metadata" src={source} aria-label="Play audio brief" />}
    {source && brief?.status === 'ready' && <div className="tool-actions">
      <button type="button" className="tool-button" disabled={delivery !== 'idle'} onClick={() => void sendToTelegram()}>
        {delivery === 'sent' ? 'Sent to Telegram' : delivery === 'sending' ? 'Sending…' : 'Send to Telegram'}
      </button>
      {delivery === 'sent' && <span role="status">Ready for you to listen later.</span>}
    </div>}
    {deliveryError && <p role="alert" className="msg-error">{deliveryError}</p>}
    {brief?.segments?.map((segment, i) => <div key={i}><strong>{segment.headline}</strong>{segment.stories?.map((story, j) => <p key={j}>{story.title}</p>)}</div>)}
    {brief?.notes?.map((note, i) => <p className="tool-muted" key={i}>{note}</p>)}
  </section>
}

export function ResultCard({ card }: { card: AskCard }) {
  if (card.kind === 'brief') return <BriefCard card={card} />
  if (card.kind === 'draft') return <details className="tool-card"><summary>Project draft</summary>
    {(Array.isArray(card.lines) ? card.lines as DraftLine[] : []).map((line, i) => <p key={i}><strong>{line.label}: </strong>{line.value}{line.groups?.map(group => <span className="tool-block" key={group.name}>{group.name}: {group.description}</span>)}</p>)}
  </details>
  if (card.kind === 'preview') return <section className="tool-card"><h4>{words(card.title) || 'Preview'}</h4>
    {typeof card.total === 'number' && <p><strong>{card.total.toLocaleString()}</strong> matching posts</p>}
    {words(card.note) && <p className="tool-muted">{words(card.note)}</p>}
    {!!rows(card.perDay).length && <div className="tool-counts">{rows(card.perDay).map((row, i) => <span key={i}>{words(row.day)} <strong>{Number(row.count).toLocaleString()}</strong></span>)}</div>}
    {(Array.isArray(card.examples) ? card.examples as ExamplePost[] : []).map((post, i) => {
      const link = postLink(post.url)
      return <div className="tool-example" key={post.id || i}><p>{post.body}</p><small>{post.day}{post.likes !== null ? ` · ${post.likes.toLocaleString()} likes` : ''}</small>
        {post.fullText && post.fullText !== post.body && <details><summary>Show full post</summary><p>{post.fullText}</p></details>}
        {link && <a href={link.href} target="_blank" rel="noopener noreferrer">{link.label}</a>}</div>
    })}
  </section>
  if (card.kind === 'chart') {
    const png = words(card.png_url)
    const safe = /^\/api\/projects\/[a-z0-9_-]{1,60}\/charts\/[a-z0-9_-]{1,60}\.png$/.test(png)
    return <section className="tool-card"><h4>{words(card.title) || 'Analysis'}</h4>
      {safe && <img className="tool-chart" src={png} alt={words(card.caption) || words(card.title)} onError={event => { event.currentTarget.hidden = true }} />}
      {rows(card.bars).map((bar, i) => <div className="tool-bar" key={i}><span>{words(bar.label)}</span><progress max={1} value={Math.min(1, Math.max(0, Number(bar.share) || 0))} /><span>{Number.isFinite(Number(bar.share)) ? `${Math.round(Number(bar.share) * 100)}%` : `${bar.posts ?? bar.value ?? ''}`}</span></div>)}
      {words(card.caption) && <p>{words(card.caption)}</p>}
      {rows(card.points).map((point, i) => <p key={i}>{[point.label, point.value, point.note].filter(x => typeof x === 'string').join(', ')}</p>)}
    </section>
  }
  return <section className="tool-card"><h4>{words(card.title) || 'Result'}</h4><p>{words(card.caption) || words(card.text) || words(card.summary) || 'The assistant returned a result.'}</p></section>
}
