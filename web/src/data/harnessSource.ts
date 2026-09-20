import type { Session } from '../app/session'
import type { DataSource, StreamSnapshot } from './source'

/** Poll complete snapshots: overlapping replay windows never double-count posts. */
export function createHarnessSource(session: Session): DataSource {
  return {
    subscribe(onUpdate) {
      const controller = new AbortController()
      let timer: ReturnType<typeof setTimeout>
      let latest: StreamSnapshot = { series: [], now: Date.now(), streaming: true, read: 0, kept: 0, note: 'Reading recent Bluesky posts and scoring sentiment…' }
      onUpdate(latest)
      const poll = async () => {
        try {
          const response = await fetch('/api/ui/scan', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ terms: session.terms, subtopics: session.subtopics }), signal: controller.signal,
          })
          const result = await response.json()
          if (!response.ok) throw new Error(result.error || `Scan failed (${response.status})`)
          latest = result as StreamSnapshot
          if (!controller.signal.aborted) onUpdate(latest)
        } catch (error) {
          if (controller.signal.aborted) return
          onUpdate({ ...latest, note: error instanceof Error ? error.message : 'Unable to read live data.' })
        }
        if (!controller.signal.aborted) timer = setTimeout(poll, 30_000)
      }
      void poll()
      return () => { controller.abort(); clearTimeout(timer) }
    },
  }
}
