import { Aggregator } from './aggregator'
import { DEFAULT_COMPANIES, DEFAULT_SPEED, type ReplayMessage } from './replayTypes'
import type { DataSource, StreamSnapshot } from './source'

/** Reuses the chart subscription boundary, fed by the local event stream. */
export function createStreamSource(): DataSource {
  let socket: WebSocket | null = null
  let agg: Aggregator | null = null
  return {
    command(command) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(command)) },
    snapshotAt(time) { return agg?.snapshot(time) ?? [] },
    subscribe(onUpdate) {
      let closed = false
      let sequence = -1
      let snapshot: StreamSnapshot = { series: [], now: null, streaming: true, read: 0, kept: 0, status: 'loading', speed: DEFAULT_SPEED }
      const publish = () => onUpdate(snapshot)
      publish()
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/replay`)
      socket = ws
      ws.onmessage = ({ data }) => {
        if (closed) return
        try {
          const message = JSON.parse(data) as ReplayMessage
          if (message.type === 'error') {
            snapshot = { ...snapshot, status: 'error', error: message.message }
          } else if (message.type === 'init') {
            const priority = (id: string) => { const i = DEFAULT_COMPANIES.indexOf(id); return i < 0 ? 100 : i }
            agg = new Aggregator([...message.companies].map((c) => ({ ...c, name: c.id === 'google' ? 'Google' : c.id === 'nvidia' ? 'Nvidia' : c.name }))
              .sort((a, b) => priority(a.id) - priority(b.id)))
            sequence = -1
            snapshot = { series: [], now: message.start, start: message.start, end: message.end, run: message.run,
              streaming: true, read: 0, kept: 0, status: 'playing', speed: message.speed }
          } else {
            if (message.run !== snapshot.run || message.sequence <= sequence || !agg) return
            if (message.sequence !== sequence + 1) throw new Error('Stream interrupted. Reload to restart.')
            sequence = message.sequence
            agg.addBatch(message.events)
            snapshot = { ...snapshot, series: agg.snapshot(message.now), now: message.now,
              read: agg.posts, kept: agg.likes, status: message.status, speed: message.speed }
          }
          publish()
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
        // StrictMode unsubscribes its first mount before the handshake finishes.
        if (ws.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close()
        else ws.close()
        if (socket === ws) socket = null
      }
    },
  }
}
