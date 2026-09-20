import { useState } from 'react'
import type { AskCard } from '../ask'
import { automationRequest, type TrackerCreated } from '../data/automationSource'

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []

export function AutomationProposalCard({ card }: { card: AskCard }) {
  const [notice, setNotice] = useState('')
  const [budget, setBudget] = useState('0.10')
  const [starting, setStarting] = useState(false)
  const config = object(card.configuration)
  const filter = object(config.keyword_filter)
  const groups = Array.isArray(filter.groups) ? filter.groups.map(object) : []
  const questions = strings(card.open_questions)
  const final = card.status === 'final' && !card.superseded
  const json = JSON.stringify(config, null, 2)
  const download = () => {
    const url = URL.createObjectURL(new Blob([json + '\n'], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = final ? 'automation-config.json' : 'automation-proposal-draft.json'
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return <section className="tool-card automation-proposal" aria-label="Automation proposal">
    <div className="proposal-status">{card.superseded ? 'Earlier revision' : final ? 'Confirmed configuration' : questions.length ? 'Draft proposal' : 'Ready for review'} · Revision {Number(card.revision) || 1}</div>
    <h4>{typeof card.title === 'string' ? card.title : 'Automation proposal'}</h4>
    <p><strong>Track:</strong> {strings(card.target_labels).join(', ')}</p>
    <p><strong>Measure:</strong> positive, negative, neutral, mixed, or insufficient evidence for each relevant target.</p>
    <details><summary>Retrieval terms and relevance rules</summary>
      {groups.map((group, index) => <div className="proposal-group" key={index}>
        <strong>{String(group.label || group.id || 'Search group')}</strong>
        {(['direct', 'ambiguous', 'contextual'] as const).map(kind => strings(group[kind]).length > 0 && <p key={kind}>{kind === 'direct' ? 'Direct matches' : kind === 'ambiguous' ? 'Ambiguous matches' : 'Needs context'}: {strings(group[kind]).join(', ')}</p>)}
        {strings(group.direct_patterns).length > 0 && <p>Additional patterns: {strings(group.direct_patterns).join(', ')}</p>}
      </div>)}
      <p><strong>Context:</strong> {strings(filter.context_terms).join(', ') || 'None'}</p>
      <p><strong>Discovery:</strong> {strings(filter.discovery_terms).join(', ') || 'None'}</p>
      <ul>{strings(card.rules).map((rule, index) => <li key={index}>{rule}</li>)}</ul>
    </details>
    <p className="tool-muted">Relevance cutoff: {typeof card.cutoff === 'number' ? `${Math.round(card.cutoff * 100)}%` : 'Not set'}. Uncalibrated; configuration validation does not establish accuracy.</p>
    {questions.length > 0 && <div><strong>Still to decide</strong><ul>{questions.map((question, index) => <li key={index}>{question}</li>)}</ul></div>}
    <p className="tool-muted">{final ? 'Ready to start. Closing the tracker stops filtering and Jev; your observations are saved.' : 'Validated offline. Keep chatting to refine this schema, then confirm when ready.'}</p>
    {final && <div className="tracker-launch">
      <label>Total Jev budget (USD) <input type="number" min="0" max="100" step="0.01" value={budget}
        onChange={event => setBudget(event.target.value)} aria-label="Total Jev budget" /></label>
      <p className="tool-muted">$0 captures matching text without sentiment scoring. This is a total spending cap, not a recurring charge.</p>
      <button className="primary-btn" disabled={starting || budget === '' || !Number.isFinite(Number(budget)) || Number(budget) < 0 || Number(budget) > 100} onClick={async () => {
        setStarting(true); setNotice('Starting your tracker…')
        try {
          const tracker = await automationRequest<TrackerCreated>('start', { session_id: card.session_id,
            proposal_hash: card.id, max_usd: Number(budget) })
          window.dispatchEvent(new CustomEvent<TrackerCreated>('sentimeter:tracker-created', { detail: tracker }))
        } catch (error) { setNotice(error instanceof Error ? error.message : String(error)) }
        finally { setStarting(false) }
      }}>{starting ? 'Starting…' : 'Start live tracking'}</button>
    </div>}
    <details><summary>Configuration JSON</summary><pre>{json}</pre></details>
    {!card.superseded && <div className="tool-actions">
      <button type="button" className="tool-button" onClick={download}>{final ? 'Download configuration' : 'Download draft'}</button>
      <button type="button" className="tool-button" onClick={async () => {
        try { await navigator.clipboard.writeText(json); setNotice('Configuration copied.') }
        catch { setNotice('Copy failed. Use Download or select the configuration JSON.') }
      }}>Copy JSON</button>
    </div>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
