import { scaleLinear } from 'd3'
import { useMemo, useState } from 'react'
import { textOn } from '../color'
import { BUCKET_MS } from '../data/config'
import type { Series } from '../data/types'
import { windowStat, type WindowStat } from '../data/window'
import { formatCount } from '../format'
import { useEased } from '../hooks/useEased'
import { useSize } from '../hooks/useSize'
import { useSmoothed } from '../hooks/useSmoothed'
import { MARGIN as M } from '../layout'
import { SentimentGauge } from './HoverCard'
import { InfoIcon, PostsIcon, SpreadIcon, TractionIcon } from './icons'

/** Past positions drawn behind each bubble, one per bucket. */
const TRAIL = 6
/** Bubble radius per sqrt(post), as a share of plot height. */
const RADIUS_K = 0.00065
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
  return stat.volume > 0 ? [stat.spread, stat.sentiment, stat.volume] : [fallback[0], fallback[1], 0]
}

export function BubbleChart({ series, now, windowMs, selected, onToggle, onClearSubtopics }: Props) {
  const [ref, { width, height }] = useSize<HTMLDivElement>()
  const iw = Math.max(0, width - M.left - M.right)
  const ih = Math.max(0, height - M.top - M.bottom)
  const [hover, setHover] = useState<string | null>(null)

  // Running axis bounds at every bucket boundary, from values known by then.
  const bounds = useMemo(() => {
    const start = Math.min(...series.map((s) => s.buckets[0]?.start ?? Infinity))
    const end = Math.max(...series.map((s) => (s.buckets.at(-1)?.start ?? -Infinity) + BUCKET_MS))
    const out: { t: number; y: [number, number] }[] = []
    let y: [number, number] = [Infinity, -Infinity]
    // Only full windows count; half-empty early ones would drag the minimum down.
    for (let t = start + windowMs; t <= end; t += BUCKET_MS) {
      for (const s of series) {
        const w = windowStat(s.buckets, t - windowMs, t)
        if (!(w.volume > 0)) continue
        y = [Math.min(y[0], w.sentiment), Math.max(y[1], w.sentiment)]
      }
      out.push({ t, y })
    }
    return out
  }, [series, windowMs])

  const known = bounds.findLast((b) => b.t <= now) ?? bounds[0]
  const yPad = known ? Math.max(0.3, (known.y[1] - known.y[0]) * 0.12) : 0
  const [yLo, yHi] = useSmoothed(
    known && isFinite(known.y[0]) ? Math.max(0, known.y[0] - yPad) : 4,
    known && isFinite(known.y[1]) ? Math.min(10, known.y[1] + yPad) : 8,
  )

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
  const y = scaleLinear().domain([yLo, yHi]).range([ih, 0])
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
  const hovered = bubbles.find((b) => b.series.id === hover)
  const hoveredStat = hover ? stats.get(hover) : undefined

  const xTicks = [0, 1, 2, 3, 4, 5]
  const windowHours = Math.round(windowMs / 3_600_000)

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
                  opacity={anySelected && !isSelected ? 0.3 : 0.92}
                  onPointerEnter={() => setHover(b.series.id)}
                  onPointerLeave={() => setHover(null)}
                  onClick={() => onToggle(b.series.id)}
                />
              )
            })}

            {bubbles.map((b) => {
              const inside = b.r * 2 - 8 >= b.series.name.length * 6.6
              return (
                <text
                  key={b.series.id}
                  x={b.cx}
                  y={inside ? b.cy : b.cy + b.r + 12}
                  dy={inside ? '0.34em' : 0}
                  textAnchor="middle"
                  className={inside ? 'bubble-label' : 'bubble-label bubble-label-out'}
                  fill={inside ? textOn(b.series.color) : '#333'}
                  opacity={anySelected && !selected.includes(b.series.id) ? 0.4 : 1}
                >
                  {b.series.name}
                </text>
              )
            })}
          </g>
        </svg>
      )}

      {width > 0 && (
        <div className="bubble-x-label" style={{ left: M.left + iw / 2, top: M.top + ih + 26 }}>
          Divisiveness, past {windowHours}h
          <span className="info" tabIndex={0} aria-label="How divisiveness is measured">
            <InfoIcon size={14} />
            <span className="info-tip" role="tooltip">
              Spread of sentiment within the subtopic, 0 to 5. Low and right is a fight, low and left is
              agreement that it is bad, high and left is quiet approval.
            </span>
          </span>
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
