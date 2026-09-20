import { Aggregator } from './aggregator'
import type { DataSource, StreamSnapshot } from './source'
import type { ReplayEvent } from './replayTypes'

export interface LiveConfiguration {
  targets: { id: string; label: string }[]
  keyword_filter: { groups: { direct: string[] }[] }
  [key: string]: unknown
}
export interface TrackerCreated { id: string; title: string; config: LiveConfiguration; max_usd: number }
export interface IngestionActivity {
  window_seconds: number; posts_last_5m: number; events_last_5m: number
  last_post_at: string | null; last_event_at: string | null; as_of: string
}
export interface LiveState {
  budget_usage?: { spent: number; reserved: number; uncertain: number }
  enabled: boolean; worker_state: string; last_error?: string; max_usd: number
  counts: Record<string, number>; source: { status: string; error?: string; ingestion?: IngestionActivity }
  config: LiveConfiguration
}

export function ingestionHealth(state?: LiveState, connectionError?: string) {
  if (connectionError) return { label: 'Stream status unavailable', tone: 'warning' }
  if (!state) return { label: 'Connecting to tracker', tone: 'waiting' }
  if (!state.enabled) return { label: 'Tracking stopped', tone: 'stopped' }
  if (state.source.status === 'gap') return { label: 'Source gap', tone: 'warning' }
  if (state.source.status === 'retrying' || state.source.error) return { label: 'Reconnecting to Bluesky', tone: 'warning' }
  if (state.source.status !== 'connected') return { label: 'Waiting for stream connection', tone: 'waiting' }
  const activity = state.source.ingestion
  if (!activity) return { label: 'Checking stream activity', tone: 'waiting' }
  const age = (stamp: string | null) => stamp ? Date.parse(activity.as_of) - Date.parse(stamp) : Infinity
  if (age(activity.last_post_at) < 15_000) return { label: 'Receiving posts', tone: 'active' }
  if (age(activity.last_event_at) < 15_000) return { label: 'Stream connected', tone: 'active' }
  return { label: 'No recent stream activity', tone: 'warning' }
}
interface Batch {
  automation: LiveState & { created_at: string }
  source: LiveState['source']; counts: Record<string, number>
  events: ReplayEvent[]; cursor: number; more: boolean
}

export async function automationRequest<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/automations/${path}`, { method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined, signal })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'The automation service could not complete the request.')
  return data as T
}

/** Completion-cursor delivery, replayed in observation order so late grades cannot reorder likes. */
export function createAutomationSource(id: string): DataSource {
  let aggregator: Aggregator | undefined
  // Observations and the completion cursor outlive a viewer subscription. A
  // returning workspace can show its chart before the next request completes.
  let cursor = 0
  const events = new Map<string, ReplayEvent>()
  let snapshot: StreamSnapshot = { series: [], now: Date.now(), start: Date.now(), streaming: true,
    read: 0, kept: 0, status: 'loading', note: 'Connecting to your tracker…' }
  return {
    snapshotAt: time => aggregator?.snapshot(time) ?? [],
    subscribe(update) {
      let disposed = false, timer: ReturnType<typeof setTimeout>
      const viewer = crypto.randomUUID()
      const controller = new AbortController()
      const release = () => {
        void fetch(`/api/automations/${id}/release`, { method: 'POST', keepalive: true,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ viewer }) }).catch(() => {})
      }
      const poll = async () => {
        let more = false
        try {
          const batch = await automationRequest<Batch>(`${id}/events?viewer=${viewer}&after=${cursor}`, undefined, controller.signal)
          if (disposed) return
          for (const event of batch.events) events.set(event.id, event)
          if (!aggregator || batch.events.length) {
            aggregator = new Aggregator(batch.automation.config.targets.map(target => ({ id: target.id, name: target.label })))
            aggregator.addBatch([...events.values()].sort((a, b) => a.t - b.t || a.id.localeCompare(b.id, undefined, { numeric: true })))
          }
          cursor = batch.cursor
          more = batch.more
          const state = { ...batch.automation, source: batch.source, counts: batch.counts }
          const pending = batch.counts.pending ?? 0, ready = batch.counts.ready ?? 0
          const now = Date.now()
          const first = Math.min(Date.parse(batch.automation.created_at), ...batch.events.map(e => e.t), snapshot.start ?? now)
          const note = !state.enabled ? 'Tracking stopped. Saved observations remain available.'
            : state.worker_state === 'capture_only' ? `${pending} matching texts captured. Set a Jev budget to classify them.`
            : state.worker_state === 'budget_exhausted' ? `Jev budget reached. ${pending} texts remain unclassified.`
            : state.worker_state === 'credentials_missing' ? 'Jev credentials are missing. Captured texts are waiting for classification.'
            : state.last_error ? `Jev: ${state.last_error}`
            : batch.source.status === 'gap' ? 'The source has a gap. Review it before resuming live capture.'
            : batch.source.error ? `Source: ${batch.source.error}`
            : `${ready} classified texts · ${pending} awaiting Jev${ready === 0 ? ' · Waiting for matching observations' : ''}`
          snapshot = { series: aggregator.snapshot(now), now, start: first, end: now, streaming: state.enabled,
            read: ready + pending + (batch.counts.others ?? 0), kept: events.size,
            status: state.enabled ? 'playing' : 'paused', live: state, note }
          update(snapshot)
        } catch (error) {
          if (disposed) return
          update({ ...snapshot, error: error instanceof Error ? error.message : String(error), status: 'error' })
        }
        if (!disposed) timer = setTimeout(poll, more ? 0 : 2000)
      }
      window.addEventListener('pagehide', release)
      update(snapshot)
      void poll()
      return () => { disposed = true; clearTimeout(timer); controller.abort(); window.removeEventListener('pagehide', release); release() }
    },
  }
}
