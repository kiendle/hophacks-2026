import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AskContext } from './ask'
import { ChatSidebar } from './components/ChatSidebar'
import { LineChart } from './components/LineChart'
import { LiveButton } from './components/LiveButton'
import { TimeSlider } from './components/TimeSlider'
import { TopBar } from './components/TopBar'
import { BUCKET_MS } from './data/config'
import { devMockSource, useSeries } from './data/source'
import type { Selection, TimeRange } from './data/types'
import { usePlayback } from './hooks/usePlayback'
import { MARGIN } from './layout'

const TOPIC = 'AI'
/** Subtopics shown when the view first opens. */
const DEFAULT_SHOWN = 2
const NO_SELECTION: Selection = { range: null, subtopics: [] }

export default function App() {
  const series = useSeries(devMockSource)
  // A few subtopics to start with, so the chart stays readable.
  const [hiddenState, setHiddenState] = useState<Set<string> | null>(null)
  const hidden = useMemo(
    () => hiddenState ?? new Set(series.slice(DEFAULT_SHOWN).map((s) => s.id)),
    [hiddenState, series],
  )
  const setHidden = (update: (prev: Set<string>) => Set<string>) => setHiddenState(update(hidden))
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
  const { playing, playhead, toggle, setUserView } = usePlayback(extent, view, setView)

  // During replay nothing past the playhead is reachable.
  const liveExtent = useMemo(
    () => (extent && playhead !== null ? { start: extent.start, end: playhead } : extent),
    [extent, playhead],
  )
  // The current moment: the right edge of the view.
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
            topic: TOPIC,
            series,
            hidden,
            selection,
            mode: 'line',
            view: { start: view.start, end: now },
            now,
          }
        : null
  })
  const getAskContext = useCallback(() => askContext.current, [])

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



  return (
    <div className="app">
      <main className="main">
        <TopBar
          topic={TOPIC}
          series={series}
          hidden={hidden}
          onToggleSeries={toggleSeries}
          onIsolateSeries={isolateSeries}
          playing={playing}
          onTogglePlay={toggle}
        />
        {extent && liveExtent && view && (
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
      </main>
      <ChatSidebar
        selection={selection}
        series={series}
        onClearSelection={() => setSelection(NO_SELECTION)}
        getContext={getAskContext}
      />
    </div>
  )
}
