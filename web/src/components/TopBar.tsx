import { useState } from 'react'
import { textOn } from '../color'
import type { Series } from '../data/types'
import { ChevronLeftIcon, PauseIcon, PlayIcon } from './icons'


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
}

export function TopBar({
  topic,
  series,
  hidden,
  onToggleSeries,
  onIsolateSeries,
  playing,
  onTogglePlay,
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
      </div>

      <div className="spacer" />

      <button className="round-btn" aria-label={playing ? 'Pause' : 'Play'} onClick={onTogglePlay}>
        {playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
      </button>
    </header>
  )
}
