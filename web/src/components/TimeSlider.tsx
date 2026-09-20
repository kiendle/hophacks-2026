import { max, scaleLinear, scaleUtc } from 'd3'
import { useMemo, useRef, useState, type PointerEvent } from 'react'
import { BUCKET_MS } from '../data/config'
import type { Series, TimeRange } from '../data/types'
import { formatTime } from '../format'
import { useSize } from '../hooks/useSize'
import { clampView, MIN_WINDOW } from '../view'

/** Histogram bar width, in buckets. */
const BIN = 3
const BARS_H = 44
const TRACK_Y = BARS_H + 14
const HEIGHT = TRACK_Y + 8
const EDGE = 8

type Mode = 'move' | 'left' | 'right'

interface Props {
  series: Series[]
  /** The navigable span. During replay it ends at the playhead, so bars fill in live. */
  extent: TimeRange
  view: TimeRange
  onChange: (r: TimeRange) => void
  /**
   * Fixed-length window ending at the current moment: dragging only scrubs
   * `view.end`, and a quiet label shows that moment by the handle.
   */
  trailing?: boolean
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

export function TimeSlider({ series, extent, view, onChange, trailing = false }: Props) {
  const [ref, { width }] = useSize<HTMLDivElement>()

  const bins = useMemo(() => {
    const byStart = new Map<number, number>()
    for (const s of series)
      for (const b of s.buckets) {
        if (b.start > extent.end) continue
        const start = extent.start + Math.floor((b.start - extent.start) / (BIN * BUCKET_MS)) * BIN * BUCKET_MS
        byStart.set(start, (byStart.get(start) ?? 0) + b.volume)
      }
    return [...byStart].sort((a, b) => a[0] - b[0])
  }, [series, extent.start, extent.end])

  const x = scaleUtc().domain([extent.start, extent.end]).range([0, width])
  const y = scaleLinear()
    .domain([0, max(bins, (d) => d[1]) ?? 1])
    .range([BARS_H, 4])
  const step = x(extent.start + BIN * BUCKET_MS) - x(extent.start)
  const barWidth = Math.max(1, step * 0.8)

  // During replay the view can start before the data does.
  const xl = clamp(x(view.start), 0, width)
  const xr = clamp(x(view.end), 0, width)
  const modeAt = (px: number): Mode | null => {
    if (trailing) return px > xl - EDGE && px < xr + EDGE ? 'move' : null
    if (Math.abs(px - xl) <= EDGE) return 'left'
    if (Math.abs(px - xr) <= EDGE) return 'right'
    if (px > xl && px < xr) return 'move'
    return null
  }

  const drag = useRef<{ mode: Mode; t0: number; view: TimeRange } | null>(null)
  const [cursor, setCursor] = useState('pointer')

  const localX = (e: PointerEvent) => e.clientX - e.currentTarget.getBoundingClientRect().left

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    const px = localX(e)
    let start = clampView(view, extent)
    let mode = modeAt(px)
    if (trailing) start = view
    if (!mode) {
      // Jump the window to the click (centered, or ending there when trailing), then keep dragging it.
      const w = start.end - start.start
      const t = x.invert(px).getTime()
      start = trailing ? trail(t, w) : clampView({ start: t - w / 2, end: t + w / 2 }, extent)
      onChange(start)
      mode = 'move'
    }
    drag.current = { mode, t0: x.invert(px).getTime(), view: start }
    setCursor(mode === 'move' ? 'grabbing' : 'ew-resize')
  }

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const px = localX(e)
    const d = drag.current
    if (!d) {
      const mode = modeAt(px)
      setCursor(mode === 'move' ? 'grab' : mode ? 'ew-resize' : 'pointer')
      return
    }
    const dt = x.invert(px).getTime() - d.t0
    const { start, end } = d.view
    if (trailing) {
      onChange(trail(end + dt, end - start))
    } else if (d.mode === 'move') {
      onChange(clampView({ start: start + dt, end: end + dt }, extent))
    } else if (d.mode === 'left') {
      onChange({ start: clamp(start + dt, extent.start, end - MIN_WINDOW), end })
    } else {
      onChange({ start, end: clamp(end + dt, start + MIN_WINDOW, extent.end) })
    }
  }

  /** A trailing window ending at `t`; it may begin before the data does. */
  function trail(t: number, w: number): TimeRange {
    const end = clamp(t, Math.min(extent.start + BUCKET_MS, extent.end), extent.end)
    return { start: end - w, end }
  }

  const onPointerUp = () => {
    drag.current = null
    setCursor('grab')
  }

  return (
    <div ref={ref} className="slider">
      {width > 0 && (
        <svg
          width={width}
          height={HEIGHT}
          style={{ cursor }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <rect className="slider-window" x={xl} width={xr - xl} height={BARS_H + 4} />
          {bins.map(([start, v]) => {
            const mid = start + (BIN * BUCKET_MS) / 2
            const inside = mid >= view.start && mid <= view.end
            return (
              <rect
                key={start}
                x={x(start) + (step - barWidth) / 2}
                y={y(v)}
                width={barWidth}
                height={BARS_H - y(v)}
                className={inside ? 'bar bar-in' : 'bar'}
              />
            )
          })}
          <line className="slider-track" x2={width} y1={TRACK_Y} y2={TRACK_Y} />
          {!trailing && <circle className="slider-handle" cx={xl} cy={TRACK_Y} r={6} />}
          <circle className="slider-handle" cx={xr} cy={TRACK_Y} r={6} />
          {trailing && <NowLabel x={xr} y={TRACK_Y} width={width} text={formatTime(view.end)} />}
        </svg>
      )}
    </div>
  )
}

/** The current moment beside the handle, on a white backing so the track does not show through. */
function NowLabel({ x, y, width, text }: { x: number; y: number; width: number; text: string }) {
  const w = text.length * 6.2 + 10
  const left = x + 12 + w > width ? x - 12 - w : x + 12
  return (
    <g>
      <rect x={left} y={y - 8} width={w} height={16} fill="#fff" />
      <text className="slider-now" x={left + 5} y={y} dy="0.34em">
        {text}
      </text>
    </g>
  )
}
