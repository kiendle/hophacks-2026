import { useState } from 'react'
import { automationRequest, ingestionHealth, type LiveState } from '../data/automationSource'

export function TrackingControls({ id, state, connectionError, onClose }: { id: string; state?: LiveState; connectionError?: string; onClose?: () => void }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [budget, setBudget] = useState('')
  const health = ingestionHealth(state, connectionError)
  const activity = state?.source.ingestion
  const action = async (name: string, body = {}) => {
    setBusy(true); setError('')
    try { await automationRequest(`${id}/${name}`, body) }
    catch (error) { setError(error instanceof Error ? error.message : String(error)) }
    finally { setBusy(false) }
  }
  return <div className="tracking-controls">
    <span className="tracking-mode">Live data</span>
    <button className="tool-button" disabled={busy || !state} onClick={() => void action(state?.enabled ? 'pause' : 'start')}>
      {state?.enabled ? 'Stop tracking' : 'Resume tracking'}
    </button>
    <button className="tool-button" onClick={onClose}>Close tracker</button>
    {state && <details><summary>Configuration & budget</summary>
      <p>Jev total budget: ${state.max_usd.toFixed(2)}. Closing this tracker stops filtering and inference.</p>
      {state.budget_usage && <p>Spent: ${state.budget_usage.spent.toFixed(5)} · Reserved or uncertain: ${(state.budget_usage.reserved + state.budget_usage.uncertain).toFixed(5)}</p>}
      <label>Total budget (USD) <input type="number" min="0" max="100" step="0.01" aria-label="Update Jev budget"
        value={budget} placeholder={String(state.max_usd)} onChange={e => setBudget(e.target.value)} /></label>
      <button className="tool-button" disabled={busy || budget === ''} onClick={() => void action('budget', { max_usd: Number(budget) })}>Apply budget</button>
      <pre>{JSON.stringify(state.config, null, 2)}</pre>
    </details>}
    {state?.source.status === 'gap' && <button className="tool-button" disabled={busy} onClick={() => void action('resume-live')}>Acknowledge missed history and resume live</button>}
    <div className="ingestion-activity" role="status" aria-label="Live ingestion activity">
      <span className={`ingestion-health ingestion-${health.tone}`}>{health.label}</span>
      <span><strong>{activity ? activity.posts_last_5m.toLocaleString() : '—'}</strong> posts ingested in the last 5 minutes</span>
      <span className="ingestion-context">Bluesky stream · before keyword filtering</span>
      <span className="ingestion-progress">{state ? Object.values(state.counts).reduce((sum, count) => sum + count, 0).toLocaleString() : '—'} matching texts · {state?.counts.pending ?? 0} awaiting Jev · {state?.counts.ready ?? 0} classified</span>
      {activity?.last_post_at && <span className="ingestion-context">Last post received {new Date(activity.last_post_at).toLocaleTimeString()}</span>}
    </div>
    {error && <p role="alert">{error}</p>}
  </div>
}
