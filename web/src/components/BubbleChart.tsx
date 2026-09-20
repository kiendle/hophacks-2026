import { scaleLinear } from 'd3'
import { useMemo, useState } from 'react'
import { textOn } from '../color'
import { BUCKET_MS } from '../data/config'
import type { Series } from '../data/types'
import { windowStat, type WindowStat } from '../data/window'
import { formatCount } from '../format'
import { useEased } from '../hooks/useEased'
import { useSize } from '../hooks/useSize'
import { MARGIN as M } from '../layout'
import { SentimentGauge } from './HoverCard'
import { PostsIcon, SpreadIcon, TractionIcon } from './icons'

/** Past positions drawn behind each bubble, one per bucket. */
const TRAIL = 6
/** Bubble radius per sqrt(post), as a share of plot height. */
const RADIUS_K = 0.0025
const CARD_WIDTH = 190

interface Props {
  series: Series[]
  /** The current moment: bubbles show the window that ends here. */
  now: number
  windowMs: number
  selected: string[]
  onToggle: (id: string) => void
  onClearSubtopics: () => void
}

/** Sentiment runs 0 to 10, so its standard deviation cannot exceed 5. */
const MAX_SPREAD = 5

/** A window's values as [spread, sentiment, volume], carrying `fallback` when empty. */
function encode(stat: WindowStat, fallback: number[]): number[] {
  return stat.volume > 0 && Number.isFinite(stat.sentiment)
    ? [stat.spread, stat.sentiment, stat.volume] : [fallback[0], fallback[1], 0]
}

export function BubbleChart({ series, now, windowMs, selected, onToggle, onClearSubtopics }: Props) {
  const [ref, { width, height }] = useSize<HTMLDivElement>()
  const iw = Math.max(0, width - M.left - M.right)
  const ih = Math.max(0, height - M.top - M.bottom)
  const [hover, setHover] = useState<string | null>(null)

  // Head plus trail values for every subtopic, flattened so they can be eased together.
  const { target, stats } = useMemo(() => {
    const target: number[] = []
    const stats = new Map<string, WindowStat>()
    for (const s of series) {
      const headStat = windowStat(s.buckets, now - windowMs, now)
      stats.set(s.id, headStat)
      const head = encode(headStat, [1, 5])
      target.push(...head)
      for (let k = 1; k <= TRAIL; k++) {
        const t = now - k * BUCKET_MS
        const p = encode(windowStat(s.buckets, t - windowMs, t), head)
        target.push(p[0], p[1])
      }
    }
    return { target, stats }
  }, [series, now, windowMs])
  const eased = useEased(target)

  const x = scaleLinear().domain([0, MAX_SPREAD]).range([0, iw])
  const y = scaleLinear().domain([0, 10]).range([ih, 0])
  const stride = 3 + TRAIL * 2

  const bubbles = series
    .map((s, i) => {
      const o = i * stride
      const trail = [[eased[o], eased[o + 1]]]
      for (let k = 0; k < TRAIL; k++) trail.push([eased[o + 3 + k * 2], eased[o + 4 + k * 2]])
      return {
        series: s,
        cx: x(eased[o]),
        cy: y(eased[o + 1]),
        r: Math.sqrt(Math.max(0, eased[o + 2])) * ih * RADIUS_K,
        trail: trail.map(([lx, sv]) => [x(lx), y(sv)]),
      }
    })
    .filter((b) => b.r > 0.5)
    .sort((a, b) => b.r - a.r)

  const anySelected = selected.length > 0
  // Keep labels readable when real companies occupy similar coordinates.
  const labels = bubbles.map((b) => ({ ...b, labelX: Math.min(iw - 95, b.cx + b.r + 10), labelY: b.cy }))
    .sort((a, b) => a.labelY - b.labelY)
  for (let i = 1; i < labels.length; i++) labels[i].labelY = Math.max(labels[i].labelY, labels[i - 1].labelY + 16)
  const overflow = Math.max(0, (labels.at(-1)?.labelY ?? 0) - ih + 5)
  for (const label of labels) label.labelY -= overflow
  const hovered = bubbles.find((b) => b.series.id === hover)
  const hoveredStat = hover ? stats.get(hover) : undefined

  const xTicks = [0, 1, 2, 3, 4, 5]

  return (
    <div ref={ref} className="chart bubble-chart">
      {width > 0 && (
        <svg width={width} height={height}>
          <g transform={`translate(${M.left},${M.top})`}>
            {y.ticks(5).map((v) => (
              <g key={v} transform={`translate(0,${y(v)})`}>
                <line x2={iw} className="grid" />
                <text x={-10} dy="0.32em" textAnchor="end" className="tick">
                  {y.tickFormat(5)(v)}
                </text>
              </g>
            ))}
            {xTicks.map((v) => (
              <g key={v} transform={`translate(${x(v)},0)`}>
                <line y2={ih} className="grid" />
                <text y={ih + 20} textAnchor="middle" className="tick">
                  {v}
                </text>
              </g>
            ))}
            <text className="axis-label" transform={`translate(${-M.left + 12},${ih / 2}) rotate(-90)`}>
              Sentiment (0 to 10)
            </text>

            <rect className="bubble-bg" width={iw} height={ih} onClick={onClearSubtopics} />

            {bubbles.map((b) => (
              <g
                key={b.series.id}
                className="bubble-trail"
                opacity={anySelected && !selected.includes(b.series.id) ? 0.25 : 1}
              >
                {b.trail.slice(0, -1).map(([x1, y1], k) => (
                  <line
                    key={k}
                    x1={x1}
                    y1={y1}
                    x2={b.trail[k + 1][0]}
                    y2={b.trail[k + 1][1]}
                    stroke={b.series.color}
                    strokeOpacity={0.75 * (1 - k / TRAIL)}
                  />
                ))}
              </g>
            ))}

            {bubbles.map((b) => {
              const isSelected = selected.includes(b.series.id)
              return (
                <circle
                  key={b.series.id}
                  className={isSelected ? 'bubble bubble-selected' : 'bubble'}
                  cx={b.cx}
                  cy={b.cy}
                  r={b.r}
                  fill={b.series.color}
                  opacity={anySelected && !isSelected ? 0.3 : (stats.get(b.series.id)?.scored ?? 0) < 30 ? 0.45 : 0.85}
                  onPointerEnter={() => setHover(b.series.id)}
                  onPointerLeave={() => setHover(null)}
                  onClick={() => onToggle(b.series.id)}
                  aria-label={b.series.name}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(b.series.id) } }}
                />
              )
            })}

            {labels.map((b) => (
              <g key={b.series.id} pointerEvents="none">
                <line x1={b.cx + b.r} y1={b.cy} x2={b.labelX - 3} y2={b.labelY}
                  stroke={b.series.color} strokeOpacity={0.5} />
                <text
                  x={b.labelX}
                  y={b.labelY}
                  dy="0.34em"
                  textAnchor="start"
                  className="bubble-label bubble-label-out"
                  fill="#333"
                  opacity={anySelected && !selected.includes(b.series.id) ? 0.4 : 1}
                >
                  {b.series.name}
                </text>
              </g>
            ))}
          </g>
        </svg>
      )}

      {width > 0 && (
        <div className="bubble-x-label" style={{ left: M.left + iw / 2, top: M.top + ih + 26 }}>
          Sentiment spread
        </div>
      )}

      {hovered && hoveredStat && (
        <div
          className="card bubble-card"
          style={{
            width: CARD_WIDTH,
            top: Math.min(Math.max(M.top + hovered.cy, 70), height - 70),
            left:
              M.left + hovered.cx + hovered.r + 14 + CARD_WIDTH > width
                ? M.left + hovered.cx - hovered.r - 14 - CARD_WIDTH
                : M.left + hovered.cx + hovered.r + 14,
          }}
        >
          <span
            className="subtopic-tag"
            style={{ background: hovered.series.color, color: textOn(hovered.series.color) }}
          >
            {hovered.series.name}
          </span>
          <SentimentGauge value={hoveredStat.sentiment} />
          <div className="card-stats muted">
            <span>
              <SpreadIcon size={13} />
              {hoveredStat.spread.toFixed(1)}
            </span>
            <span>
              <TractionIcon size={13} />
              {formatCount(Math.round(hoveredStat.traction))}
            </span>
            <span>
              <PostsIcon size={13} />
              {formatCount(Math.round(hoveredStat.volume))}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
