import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AskContext } from '../ask'
import '../bubble.css'
import { BubbleChart } from '../components/BubbleChart'
import { ChatSidebar } from '../components/ChatSidebar'
import { LineChart } from '../components/LineChart'
import { LiveButton } from '../components/LiveButton'
import { TimeSlider } from '../components/TimeSlider'
import { TopBar, type ViewMode } from '../components/TopBar'
import { BUCKET_MS } from '../data/config'
import { createDevMockSource, useStream } from '../data/source'
import { createHarnessSource } from '../data/harnessSource'
import { createStreamSource } from '../data/streamSource'
import { devEventConnect } from '../data/devEvents'
import type { Selection, TimeRange } from '../data/types'
import type { Session } from './session'
import { useLiveFollow } from '../hooks/useLiveFollow'
import { usePlayback } from '../hooks/usePlayback'
import { MARGIN } from '../layout'

/** Firehose posts scanned for each one kept, for the event readout. */
const EVENTS_PER_KEPT = 38
/** Subtopics shown when the line view first opens. */
const LINE_DEFAULT_SHOWN = 2
/** Length of the bubble view's trailing window. */
const BUBBLE_WINDOW_MS = 6 * BUCKET_MS
const NO_SELECTION: Selection = { range: null, subtopics: [] }

interface Props {
  session: Session
  onSessionChange: (session: Session) => void
  /** Chrome only, with no data and no replay, while the run is being set up. */
  preview?: boolean
}

const EMPTY_SOURCE = { subscribe: () => () => {} }

/** ?stream=<speed> replays the fixture as a live event stream at that speed. */
const STREAM_SPEED = Number(new URLSearchParams(location.search).get('stream')) || 0

export function Workspace({ session, onSessionChange, preview }: Props) {
  const source = useMemo(() => {
    if (preview) return EMPTY_SOURCE
    if (!STREAM_SPEED) return import.meta.env.VITE_DEMO_MODE === 'true' ? createDevMockSource(session.subtopics) : createHarnessSource(session)
    const names = new Map(session.subtopics.map((n) => [n.toLowerCase().replace(/[^a-z0-9]+/g, '-'), n]))
    return createStreamSource(devEventConnect(session.subtopics, STREAM_SPEED), { names })
  }, [preview, session])
  const stream = useStream(source)
  const series = stream.series
  const [mode, setMode] = useState<ViewMode>('line')
  // Each view keeps its own shown subtopics. The line view starts with a few so it stays readable.
  const [hiddenByMode, setHiddenByMode] = useState<Record<ViewMode, Set<string> | null>>({ line: null, bubble: null })
  const hidden = useMemo(
    () =>
      hiddenByMode[mode] ??
      new Set(mode === 'line' ? series.slice(LINE_DEFAULT_SHOWN).map((s) => s.id) : []),
    [hiddenByMode, mode, series],
  )
  const setHidden = (update: (prev: Set<string>) => Set<string>) =>
    setHiddenByMode((h) => ({ ...h, [mode]: update(hidden) }))
  const visible = useMemo(() => series.filter((s) => !hidden.has(s.id)), [series, hidden])

  const extent = useMemo<TimeRange | null>(() => {
    const all = series.flatMap((s) => s.buckets)
    if (!all.length) return null
    return {
      start: Math.min(...all.map((b) => b.start)),
      end: Math.max(...all.map((b) => b.start)) + BUCKET_MS,
    }
  }, [series])

  const [viewState, setView] = useState<TimeRange | null>(null)
  const view = viewState ?? extent
  const [selection, setSelection] = useState<Selection>(NO_SELECTION)
  const live = stream.streaming
  const playback = usePlayback(live ? null : extent, view, setView)
  const follow = useLiveFollow(extent, view, setView, stream.now, live)
  const playhead = live ? stream.now : playback.playhead
  const playing = live ? follow.following : playback.playing
  const toggle = live ? follow.toggle : playback.toggle
  const setUserView = live ? follow.setUserView : playback.setUserView

  // During replay nothing past the playhead is reachable.
  const liveExtent = useMemo(
    () => (extent && playhead !== null ? { start: extent.start, end: playhead } : extent),
    [extent, playhead],
  )
  // The current moment, shared by both views: the right edge of the view.
  const now = view && liveExtent ? Math.min(view.end, liveExtent.end) : 0
  // Tracking live means the view ends at the newest data; the Live control snaps back there.
  const isLive = !!view && !!liveExtent && view.end >= liveExtent.end - BUCKET_MS
  const goLive = () => {
    if (!view || !liveExtent) return
    setUserView({ start: liveExtent.end - (view.end - view.start), end: liveExtent.end })
  }

  // What the Ask panel sends with a question, read at send time so it is never stale.
  const askContext = useRef<AskContext | null>(null)
  useLayoutEffect(() => {
    askContext.current =
      view && liveExtent
        ? {
            topic: session.query,
            series,
            hidden,
            selection,
            mode,
            view: mode === 'bubble' ? { start: now - BUBBLE_WINDOW_MS, end: now } : { start: view.start, end: now },
            now,
          }
        : { topic: session.query, series, hidden, selection, mode, view: { start: Date.now() - 900_000, end: Date.now() }, now: Date.now() }
  })
  const getAskContext = useCallback(() => askContext.current, [])

  // Events read from the firehose and kept after filtering, up to the current moment.
  const events = useMemo(() => {
    if (live) return { read: stream.read, kept: stream.kept }
    let kept = 0
    for (const s of series) for (const b of s.buckets) if (b.start + BUCKET_MS <= now) kept += b.volume
    return { kept, read: kept * EVENTS_PER_KEPT }
  }, [live, stream.read, stream.kept, series, now])

  const started = useRef(false)
  useEffect(() => {
    if (preview || live || started.current || !series.length) return
    started.current = true
    toggle()
  }, [preview, live, series, toggle])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelection(NO_SELECTION)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const isolateSeries = (id: string) =>
    setHidden((prev) => {
      const isolated = series.every((s) => (s.id === id) !== prev.has(s.id))
      return isolated ? new Set() : new Set(series.filter((s) => s.id !== id).map((s) => s.id))
    })

  const toggleSeries = (id: string) =>
    setHidden((prev) => {
      const next = new Set(prev)
      if (!next.delete(id)) next.add(id)
      return next
    })

  /** Scrubbing the bubble view moves "now", keeping the line view's zoom where the data allows. */
  const scrubTo = (end: number) => {
    if (!view || !extent) return
    const start = Math.max(extent.start, end - (view.end - view.start))
    setUserView({ start: Math.min(start, end - BUCKET_MS), end })
  }

  const toggleSubtopic = (id: string) =>
    setSelection((s) => {
      const subtopics = s.subtopics.includes(id) ? s.subtopics.filter((x) => x !== id) : [...s.subtopics, id]
      // Without a chosen range, a bubble stands for the window it currently shows.
      const range = s.range ?? (extent && { start: Math.max(extent.start, now - BUBBLE_WINDOW_MS), end: now })
      return { range, subtopics }
    })

  return (
    <>
      <main className="main">
        <TopBar
          topic={session.query}
          series={series}
          hidden={hidden}
          onToggleSeries={toggleSeries}
          onIsolateSeries={isolateSeries}
          playing={playing}
          onTogglePlay={toggle}
          terms={session.terms}
          onTermsChange={(terms) => onSessionChange({ ...session, terms })}
          events={events}
          mode={mode}
          onModeChange={setMode}
        />
        {!preview && stream.note && <p className="setup-label" role="status" style={{ margin: '8px 24px' }}>{stream.note}</p>}
        {extent && liveExtent && view && mode === 'line' && (
          <>
            <LineChart
              series={visible}
              view={view}
              extent={liveExtent}
              playhead={playhead}
              selection={selection.range}
              onSelect={(range) => setSelection((s) => ({ ...s, range }))}
              onViewChange={setUserView}
              selectedSubtopics={selection.subtopics}
              onToggleSubtopic={(id) =>
                setSelection((s) => ({
                  ...s,
                  subtopics: s.subtopics.includes(id) ? s.subtopics.filter((x) => x !== id) : [...s.subtopics, id],
                }))
              }
            />
            <div className="timeline" style={{ position: 'relative', paddingLeft: MARGIN.left, paddingRight: MARGIN.right }}>
              <TimeSlider series={visible} extent={liveExtent} view={view} onChange={setUserView} />
              <LiveButton live={isLive} onGoLive={goLive} />
            </div>
          </>
        )}
        {extent && liveExtent && view && mode === 'bubble' && (
          <>
            <BubbleChart
              series={visible}
              now={now}
              windowMs={BUBBLE_WINDOW_MS}
              selected={selection.subtopics}
              onToggle={toggleSubtopic}
              onClearSubtopics={() => setSelection((s) => ({ ...s, subtopics: [] }))}
            />
            <div className="timeline" style={{ position: 'relative', paddingLeft: MARGIN.left, paddingRight: MARGIN.right }}>
              <TimeSlider
                series={visible}
                extent={liveExtent}
                view={{ start: now - BUBBLE_WINDOW_MS, end: now }}
                onChange={(r) => scrubTo(r.end)}
                trailing
              />
              <LiveButton live={isLive} onGoLive={goLive} />
            </div>
          </>
        )}
      </main>
      <ChatSidebar
        selection={selection}
        series={series}
        onClearSelection={() => setSelection(NO_SELECTION)}
        getContext={getAskContext}
      />
    </>
  )
}
