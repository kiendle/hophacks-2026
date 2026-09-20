import { useState } from 'react'
import { FiltersMenu } from '../app/FiltersMenu'
import { textOn } from '../color'
import { formatCount } from '../format'
import type { Series } from '../data/types'
import { BubblesIcon, ChevronLeftIcon, LineChartIcon, PauseIcon, PlayIcon } from './icons'

export type ViewMode = 'line' | 'bubble'

/** Pills shown before the rest collapse into "+N". */
const COLLAPSED = 2

interface Props {
  topic: string
  series: Series[]
  hidden: Set<string>
  onToggleSeries: (id: string) => void
  /** Cmd or Ctrl click: show only this subtopic, or everything again if it already is. */
  onIsolateSeries: (id: string) => void
  playing: boolean
  onTogglePlay: () => void
  /** Search terms a post must match, editable mid-run. */
  terms: string[]
  onTermsChange: (terms: string[]) => void
  /** Firehose events seen and kept so far. */
  events: { read: number; kept: number }
  mode: ViewMode
  onModeChange: (mode: ViewMode) => void
}

export function TopBar({
  topic,
  series,
  hidden,
  onToggleSeries,
  onIsolateSeries,
  playing,
  onTogglePlay,
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
      <span className="topic-sub">Subtopics</span>
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
        read {formatCount(events.read)} · maintained {formatCount(events.kept)}
      </span>
      <FiltersMenu terms={terms} onChange={onTermsChange} />
      <span className="divider" />

      <button className="round-btn" aria-label={playing ? 'Pause' : 'Play'} onClick={onTogglePlay}>
        {playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
      </button>
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
    </header>
  )
}
