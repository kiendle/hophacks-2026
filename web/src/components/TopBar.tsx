import { useState } from 'react'
import { FiltersMenu } from '../app/FiltersMenu'
import { textOn } from '../color'
import { formatCount } from '../format'
import type { Series } from '../data/types'
import { LINE_INTERVALS, LIVE_INTERVALS } from '../data/config'
import { BubblesIcon, ChevronLeftIcon, LineChartIcon, PauseIcon, PlayIcon } from './icons'

export type ViewMode = 'line' | 'bubble'

/** Pills shown before the rest collapse into "+N". */
const COLLAPSED = 2

interface Props {
  liveAutomation?: boolean
  topic: string
  series: Series[]
  hidden: Set<string>
  onToggleSeries: (id: string) => void
  /** Cmd or Ctrl click: show only this subtopic, or everything again if it already is. */
  onIsolateSeries: (id: string) => void
  playing: boolean
  onTogglePlay: () => void
  speed: number
  onSpeedChange: (speed: number) => void
  intervalMs: number
  onIntervalChange: (interval: number) => void
  disabled?: boolean
  loading?: boolean
  terms: string[]
  onTermsChange: (terms: string[]) => void
  /** Firehose events seen and kept so far. */
  events: { read: number; kept: number }
  mode: ViewMode
  onModeChange: (mode: ViewMode) => void
}

export function TopBar({
  liveAutomation = false,
  topic,
  series,
  hidden,
  onToggleSeries,
  onIsolateSeries,
  playing,
  onTogglePlay,
  speed,
  onSpeedChange,
  intervalMs,
  onIntervalChange,
  disabled,
  loading,
  terms,
  onTermsChange,
  events,
  mode,
  onModeChange,
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? series : series.slice(0, COLLAPSED)
  const rest = series.length - shown.length

  return (
    <header className="topbar">
      <span className="topic">{topic}</span>
      <div className="pills">
      {shown.map((s) => {
        const on = !hidden.has(s.id)
        return (
          <button
            key={s.id}
            className={on ? 'pill pill-on' : 'pill'}
            style={on ? { background: s.color, borderColor: s.color, color: textOn(s.color) } : { borderColor: s.color }}
            onClick={(e) => (e.metaKey || e.ctrlKey ? onIsolateSeries(s.id) : onToggleSeries(s.id))}
          >
            {s.name}
          </button>
        )
      })}
      </div>
      {rest > 0 && (
        <button className="pill pill-add" onClick={() => setExpanded(true)}>
          +{rest}
        </button>
      )}
      {expanded && series.length > COLLAPSED && (
        <button className="pill pill-add" aria-label="Collapse subtopics" onClick={() => setExpanded(false)}>
          <ChevronLeftIcon size={15} />
        </button>
      )}

      <div className="spacer" />

      <span className="events">
        {loading ? 'Loading posts…' : `${formatCount(events.read)} ${liveAutomation ? 'matching texts' : 'posts'} · ${formatCount(events.kept)} updates`}
      </span>
      <div className="chart-controls">
      {!liveAutomation && <FiltersMenu terms={terms} onChange={onTermsChange} />}
      {mode === 'line' && <label className="interval-control">
        Interval
        <select className="stream-speed" aria-label="Point interval" value={intervalMs}
          onChange={(e) => onIntervalChange(Number(e.target.value))}>
          {(liveAutomation ? LIVE_INTERVALS : LINE_INTERVALS).map(interval => <option key={interval.value} value={interval.value}>{interval.label}</option>)}
        </select>
      </label>}
      {!liveAutomation && <><select className="stream-speed" aria-label="Playback speed" value={speed}
        onChange={(e) => onSpeedChange(Number(e.target.value))} disabled={disabled}>
        <option value={3600}>1h / s</option>
        <option value={14400}>4h / s</option>
        <option value={43200}>12h / s</option>
      </select>
      <button className="round-btn" aria-label={playing ? 'Pause' : 'Play'} onClick={onTogglePlay} disabled={disabled}>
        {playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
      </button></>}
      <span className="divider" />
      <div className="segmented">
        <button className={mode === 'line' ? 'on' : ''} aria-label="Line view" onClick={() => onModeChange('line')}>
          <LineChartIcon size={17} />
        </button>
        <button
          className={mode === 'bubble' ? 'on' : ''}
          aria-label="Bubble view"
          onClick={() => onModeChange('bubble')}
        >
          <BubblesIcon size={17} />
        </button>
      </div>
      </div>
    </header>
  )
}
