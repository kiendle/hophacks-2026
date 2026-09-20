import type { Session } from '../app/session'
import type { DataSource, StreamSnapshot } from './source'
import { Aggregator } from './aggregator'
import type { ReplayDataset } from './replayTypes'

/** Poll complete snapshots: overlapping replay windows never double-count posts. */
export function createHarnessSource(session: Session): DataSource {
  return {
    subscribe(onUpdate) {
      const live = /\b(?:bluesky|live|real[ -]?time)\b/i.test(session.query) && !/\b(?:no|not|without)\s+(?:live|bluesky|real[ -]?time)\b/i.test(session.query)
      const controller = new AbortController()
      let timer: ReturnType<typeof setTimeout>
      let latest: StreamSnapshot = { series: [], now: live ? Date.now() : null, streaming: live, read: 0, kept: 0, note: live ? 'Reading live Bluesky posts...' : 'Loading the Jev-classified Twitter dataset...' }
      onUpdate(latest)
      const poll = async () => {
        try {
          const response = await fetch('/api/ui/scan', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ terms: session.terms, subtopics: session.subtopics, source: live ? 'bluesky_live' : 'twitter_archive' }), signal: controller.signal,
          })
          const result = await response.json()
          if (!response.ok) throw new Error(result.error || `Scan failed (${response.status})`)
          if (result.format === 'jev-classified-events') {
            const dataset = result.dataset as ReplayDataset
            const aggregator = new Aggregator(dataset.companies)
            // Yield between batches so large exports do not freeze the interface.
            for (let i = 0; i < dataset.events.length; i += 3000) {
              if (controller.signal.aborted) return
              aggregator.addBatch(dataset.events.slice(i, i + 3000))
              await new Promise(resolve => setTimeout(resolve, 0))
            }
            latest = { series: aggregator.snapshot(dataset.end), now: null, streaming: false,
              read: result.read, kept: result.kept, start: dataset.start, end: dataset.end,
              note: dataset.events.length ? undefined : 'No matching posts. Try another keyword.' }
          } else latest = result as StreamSnapshot
          if (!controller.signal.aborted) onUpdate(latest)
        } catch (error) {
          if (controller.signal.aborted) return
          onUpdate({ ...latest, note: error instanceof Error ? error.message : 'Unable to read live data.' })
        }
        if (live && !controller.signal.aborted) timer = setTimeout(poll, 30_000)
      }
      void poll()
      return () => { controller.abort(); clearTimeout(timer) }
    },
  }
}
