import { Aggregator } from './aggregator'
import { DEFAULT_COMPANIES, DEFAULT_SPEED, type ReplayMessage } from './replayTypes'

/** Ingest every frame; coalesce expensive chart snapshots to at most 10Hz. */
export const REPLAY_PUBLISH_INTERVAL_MS = 100
import type { DataSource, StreamSnapshot } from './source'

/** Reuses the chart subscription boundary, fed by the local event stream. */
export function createStreamSource(terms: string[] = ['AI']): DataSource {
  let socket: WebSocket | null = null
  let agg: Aggregator | null = null
  return {
    command(command) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(command)) },
    snapshotAt(time) { return agg?.snapshot(time) ?? [] },
    subscribe(onUpdate) {
      let closed = false
      let sequence = -1
      let pending: ReturnType<typeof setTimeout> | undefined
      let dirty = false
      let snapshot: StreamSnapshot = { series: [], now: null, streaming: true, read: 0, kept: 0, status: 'loading', speed: DEFAULT_SPEED }
      const cancel = () => { clearTimeout(pending); pending = undefined }
      const publish = () => {
        cancel()
        if (closed) return
        if (dirty && agg && snapshot.now !== null) {
          snapshot = { ...snapshot, series: agg.snapshot(snapshot.now), read: agg.posts, kept: agg.likes }
        }
        dirty = false
        onUpdate(snapshot)
      }
      const schedule = () => {
        if (pending === undefined) pending = setTimeout(publish, REPLAY_PUBLISH_INTERVAL_MS)
      }
      publish()
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/replay?terms=${encodeURIComponent(JSON.stringify(terms))}`)
      socket = ws
      ws.onmessage = ({ data }) => {
        if (closed) return
        try {
          const message = JSON.parse(data) as ReplayMessage
          if (message.type === 'error') {
            snapshot = { ...snapshot, status: 'error', error: message.message }
            publish()
          } else if (message.type === 'init') {
            cancel()
            const priority = (id: string) => { const i = DEFAULT_COMPANIES.indexOf(id); return i < 0 ? 100 : i }
            agg = new Aggregator([...message.companies].map((c) => ({ ...c, name: c.id === 'google' ? 'Google' : c.id === 'nvidia' ? 'Nvidia' : c.name }))
              .sort((a, b) => priority(a.id) - priority(b.id)))
            sequence = -1
            snapshot = { series: [], now: message.start, start: message.start, end: message.end, run: message.run,
              streaming: true, read: 0, kept: 0, status: 'playing', speed: message.speed }
            dirty = false
            publish()
          } else {
            if (message.run !== snapshot.run || message.sequence <= sequence || !agg) return
            if (message.sequence !== sequence + 1) throw new Error('Stream interrupted. Reload to restart.')
            sequence = message.sequence
            agg.addBatch(message.events)
            const controlChanged = snapshot.status !== message.status || snapshot.speed !== message.speed
            snapshot = { ...snapshot, now: message.now, status: message.status, speed: message.speed }
            dirty = true
            // Controls and the final frame must be visible immediately. Ordinary
            // delivery bursts share a snapshot; no events or timestamps are lost.
            if (controlChanged || message.status === 'complete') publish()
            else schedule()
          }
        } catch (error) {
          snapshot = { ...snapshot, status: 'error', error: error instanceof Error ? error.message : 'Invalid stream data.' }
          publish()
          ws.close()
        }
      }
      ws.onclose = () => {
        if (!closed && snapshot.status !== 'complete' && snapshot.status !== 'error') {
          snapshot = { ...snapshot, status: 'error', error: 'Stream disconnected. Reload to restart.' }
          publish()
        }
      }
      return () => {
        closed = true
        cancel()
        // StrictMode unsubscribes its first mount before the handshake finishes.
        if (ws.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close()
        else ws.close()
        if (socket === ws) socket = null
      }
    },
  }
}
