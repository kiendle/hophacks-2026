import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AskContext } from '../ask'
import '../bubble.css'
import { BubbleChart } from '../components/BubbleChart'
import { ChartLoading } from '../components/ChartLoading'
import { ChatSidebar } from '../components/ChatSidebar'
import { LineChart } from '../components/LineChart'
import { LineDisplayToggle, type LineDisplay } from '../components/LineDisplayToggle'
import { LiveButton } from '../components/LiveButton'
import { TimeSlider } from '../components/TimeSlider'
import { TopBar, type ViewMode } from '../components/TopBar'
import { BUCKET_MS, DEFAULT_LINE_INTERVAL } from '../data/config'
import { useStream, type DataSource } from '../data/source'
import { createStreamSource } from '../data/streamSource'
import { createAutomationSource } from '../data/automationSource'
import { TrackingControls } from '../components/TrackingControls'
import { DEFAULT_COMPANIES } from '../data/replayTypes'
import type { Selection, TimeRange } from '../data/types'
import type { Session } from './session'
import { useLiveFollow } from '../hooks/useLiveFollow'
import { MARGIN } from '../layout'

/** Subtopics shown when the line view first opens. */
const LINE_DEFAULT_SHOWN = 2
/** Length of the bubble view's trailing window. */
const BUBBLE_WINDOW_MS = 6 * BUCKET_MS
const NO_SELECTION: Selection = { range: null, subtopics: [] }

interface Props {
  active?: boolean
  onClose?: () => void
  session: Session
  onSessionChange: (session: Session) => void
  /** Chrome only, with no data and no replay, while the run is being set up. */
  preview?: boolean
}

const EMPTY_SOURCE: DataSource = { subscribe: () => () => {} }

export function Workspace({ session, onSessionChange, preview, onClose, active = true }: Props) {
  const [retry, setRetry] = useState(0)
  const termsKey = JSON.stringify(session.terms)
  const source = useMemo<DataSource>(() => {
    if (preview) return EMPTY_SOURCE
    return session.automationId ? createAutomationSource(session.automationId) : createStreamSource(JSON.parse(termsKey))
  // eslint-disable-next-line react-hooks/exhaustive-deps -- Retrying must create a fresh source and WebSocket.
  }, [preview, session.automationId, termsKey, retry])
  const stream = useStream(source, active, !session.automationId)
  const allSeries = stream.series
  const hasChartData = allSeries.some(series => series.buckets.length > 0)
  const connecting = !stream.status || stream.status === 'loading'
  const [mode, setMode] = useState<ViewMode>('line')
  const [intervalMs, setIntervalMs] = useState(session.automationId ? 60_000 : DEFAULT_LINE_INTERVAL)
  const [lineDisplay, setLineDisplay] = useState<LineDisplay>(session.automationId ? 'points' : 'both')
  // Each view keeps its own shown subtopics. The line view starts with a few so it stays readable.
  const [hiddenByMode, setHiddenByMode] = useState<Record<ViewMode, Set<string> | null>>({ line: null, bubble: null })
  const hidden = useMemo(
    () =>
      hiddenByMode[mode] ??
      new Set(allSeries.filter((s, i) => mode === 'line' ? i >= LINE_DEFAULT_SHOWN : !session.automationId && !DEFAULT_COMPANIES.includes(s.id)).map((s) => s.id)),
    [hiddenByMode, mode, allSeries, session.automationId],
  )
  const setHidden = (update: (prev: Set<string>) => Set<string>) =>
    setHiddenByMode((h) => ({ ...h, [mode]: update(hidden) }))

  const extent = useMemo<TimeRange | null>(() => {
    const start = stream.start ?? Math.min(...allSeries.flatMap(s => s.buckets.map(b => b.start)))
    if (!Number.isFinite(start) || stream.now === null) return null
    return { start, end: Math.max(start + 1, stream.now) }
  }, [stream.start, stream.now, allSeries])

  const [viewState, setView] = useState<TimeRange | null>(null)
  const retainedView = viewState ?? extent
  const [selection, setSelection] = useState<Selection>(NO_SELECTION)
  const follow = useLiveFollow(extent, retainedView, setView, stream.now, true)
  const view = follow.displayView
  const playhead = stream.now
  const playing = source.command ? stream.status === 'playing' : follow.following
  const toggle = () => {
    if (!source.command) { follow.toggle(); return }
    if (stream.status === 'complete') {
      setView(null)
      source.command?.({ type: 'restart' })
      follow.resume(true)
    } else source.command?.({ type: playing ? 'pause' : 'resume' })
  }
  const setUserView = follow.setUserView

  // During replay nothing past the playhead is reachable.
  const liveExtent = useMemo(
    () => (extent && playhead !== null ? { start: extent.start, end: playhead } : extent),
    [extent, playhead],
  )
  // The current moment, shared by both views: the right edge of the view.
  const now = view && liveExtent ? Math.min(view.end, liveExtent.end) : 0
  const series = useMemo(() => now && stream.now !== null && now < stream.now
    ? source.snapshotAt?.(now) ?? allSeries : allSeries, [source, now, stream.now, allSeries])
  const visible = useMemo(() => series.filter((s) => !hidden.has(s.id)), [series, hidden])
  // Tracking live means the view ends at the newest data; the Live control snaps back there.
  const isLive = follow.following
  const goLive = () => {
    if (!view || !liveExtent) return
    setUserView({ start: Math.max(liveExtent.start, liveExtent.end - (view.end - view.start)), end: liveExtent.end })
    follow.resume()
  }

  // What the Ask panel sends with a question, read at send time so it is never stale.
  const askContext = useRef<AskContext | null>(null)
  useLayoutEffect(() => {
    askContext.current =
      view && liveExtent
        ? {
            topic: session.query,
            dataset: { source: session.automationId ? 'bluesky_live' : 'twitter_archive', keywords: session.terms, automationId: session.automationId },
            intervalMs,
            lineDisplay,
            series,
            hidden,
            selection,
            mode,
            view: mode === 'bubble' ? { start: now - BUBBLE_WINDOW_MS, end: now } : { start: view.start, end: now },
            now,
          }
        : null
  })
  const getAskContext = useCallback(() => askContext.current, [])

  // Actual delivered source events, counted once regardless of company overlap.
  const events = { read: stream.read, kept: stream.kept }

  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelection(NO_SELECTION)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [active])

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
          liveAutomation={!!session.automationId}
          topic={session.query}
          series={series}
          hidden={hidden}
          onToggleSeries={toggleSeries}
          onIsolateSeries={isolateSeries}
          playing={playing}
          onTogglePlay={toggle}
          speed={stream.speed ?? 14_400}
          onSpeedChange={(speed) => source.command?.({ type: 'speed', speed })}
          intervalMs={intervalMs}
          onIntervalChange={(interval) => { setIntervalMs(interval); setSelection(NO_SELECTION) }}
          disabled={connecting || stream.status === 'error'}
          loading={!preview && connecting}
          terms={session.terms}
          onTermsChange={terms => onSessionChange({ ...session, terms })}
          events={events}
          mode={mode}
          onModeChange={setMode}
        />
        {!preview && session.automationId &&
          <TrackingControls id={session.automationId} state={stream.live} connectionError={stream.error} onClose={onClose} />}
        {!preview && stream.note && <p role="status">{stream.note}</p>}
        {hasChartData && stream.error && <p role="alert">{stream.error}</p>}
        {!preview && !hasChartData && <ChartLoading
          status={stream.error ? 'error' : connecting ? 'loading' : stream.status === 'playing' ? 'waiting' : stream.status!}
          live={!!session.automationId}
          error={stream.error}
          onRetry={session.automationId ? undefined : () => {
            setView(null)
            setSelection(NO_SELECTION)
            setRetry(value => value + 1)
          }}
        />}
        {hasChartData && extent && liveExtent && view && mode === 'line' && (
          <>
            <div style={{ paddingLeft: MARGIN.left }}>
              <LineDisplayToggle value={lineDisplay} onChange={setLineDisplay} />
            </div>
            <LineChart
              display={lineDisplay}
              intervalMs={intervalMs}
              series={visible}
              view={view}
              extent={liveExtent}
              playhead={now}
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
              <TimeSlider series={visible} extent={liveExtent} view={view} onChange={setUserView}
                onInteractionStart={follow.beginInteraction} onInteractionEnd={follow.endInteraction} />
              <LiveButton live={isLive} onGoLive={goLive} />
            </div>
          </>
        )}
        {hasChartData && extent && liveExtent && view && mode === 'bubble' && (
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
                onInteractionStart={follow.beginInteraction}
                onInteractionEnd={follow.endInteraction}
                trailing
              />
              <LiveButton live={isLive} onGoLive={goLive} />
            </div>
          </>
        )}
      </main>
      <ChatSidebar
        active={active}
        selection={selection}
        series={series}
        onClearSelection={() => setSelection(NO_SELECTION)}
        getContext={getAskContext}
      />
    </>
  )
}
