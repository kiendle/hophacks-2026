import { area, interpolateRgb, line, max, scaleLinear, scaleSqrt, scaleUtc, stack } from 'd3'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from 'react'
import { BUCKET_MS } from '../data/config'
import type { Bucket, Series, TimeRange } from '../data/types'
import { formatCount, formatTick } from '../format'
import { useSize } from '../hooks/useSize'
import { useSmoothed } from '../hooks/useSmoothed'
import { MARGIN as M } from '../layout'
import { clampView } from '../view'
import { HoverCard } from './HoverCard'

/** Height of the volume band under the main plot, and the gap above it. */
const VOL_H = 104
const VOL_GAP = 16

interface Point {
  /** Bucket midpoint, epoch ms. */
  t: number
  s: number
  v: number
  bucket: Bucket
  /** 0 to 1 while a point is appearing during playback. */
  grow: number
}

interface Layer {
  series: Series
  points: Point[]
  /** Interpolated end of the line at the playhead. */
  tail: { t: number; s: number; v: number } | null
}

interface Props {
  series: Series[]
  view: TimeRange
  /** The data known so far; pans and selections stay inside it. */
  extent: TimeRange
  playhead: number | null
  selection: TimeRange | null
  onSelect: (r: TimeRange | null) => void
  onViewChange: (r: TimeRange) => void
  /** Subtopics picked for the Ask panel; the rest fade while any are picked. */
  selectedSubtopics?: string[]
  /** Cmd or Ctrl click on a line picks or unpicks its subtopic. */
  onToggleSubtopic?: (id: string) => void
}

const HALF = BUCKET_MS / 2
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))
const lerp = (a: number, b: number, k: number) => a + (b - a) * k

function buildLayer(series: Series, view: TimeRange, cutoff: number, animating: boolean): Layer {
  const points: Point[] = []
  let tail: Layer['tail'] = null
  const bs = series.buckets
  for (let i = 0; i < bs.length; i++) {
    const t = bs[i].start + HALF
    if (t > cutoff) {
      const prev = bs[i - 1]
      if (prev && prev.start + HALF <= cutoff) {
        const k = (cutoff - (prev.start + HALF)) / BUCKET_MS
        tail = {
          t: cutoff,
          s: lerp(prev.sentiment, bs[i].sentiment, k),
          v: lerp(prev.volume, bs[i].volume, k),
        }
      }
      break
    }
    // One bucket of slack on each side keeps the line running off the edges.
    if (t < view.start - BUCKET_MS || t > view.end + BUCKET_MS) continue
    const grow = animating ? clamp((cutoff - t) / (BUCKET_MS * 0.8), 0, 1) : 1
    points.push({ t, s: bs[i].sentiment, v: bs[i].volume, bucket: bs[i], grow })
  }
  return { series, points, tail }
}

function sentimentTarget(layers: Layer[], view: TimeRange): [number, number] {
  let lo = Infinity
  let hi = -Infinity
  for (const { points, tail } of layers) {
    for (const p of points) {
      if (p.t < view.start || p.t > view.end) continue
      lo = Math.min(lo, p.s)
      hi = Math.max(hi, p.s)
    }
    if (tail) {
      lo = Math.min(lo, tail.s)
      hi = Math.max(hi, tail.s)
    }
  }
  if (!isFinite(lo)) return [4, 8]
  const pad = Math.max(0.3, (hi - lo) * 0.15)
  return [Math.max(0, lo - pad), Math.min(10, hi + pad)]
}

type VolumeRow = { t: number; total: number } & Record<string, number>

/** Posts per bucket for each shown subtopic, keyed by series id, plus the total. */
function volumeRows(layers: Layer[]): VolumeRow[] {
  const byT = new Map<number, VolumeRow>()
  const add = (t: number, id: string, v: number) => {
    const row = byT.get(t) ?? ({ t, total: 0 } as VolumeRow)
    row[id] = v
    row.total += v
    byT.set(t, row)
  }
  for (const { series, points, tail } of layers) {
    for (const p of points) add(p.t, series.id, p.v)
    if (tail) add(tail.t, series.id, tail.v)
  }
  return [...byT.values()].sort((a, b) => a.t - b.t)
}

/** A subtopic's color washed toward white, for the stacked volume bands. */
const muted = (color: string) => interpolateRgb(color, '#fff')(0.55)

function snapRange(a: number, b: number, extent: TimeRange): TimeRange {
  const start = Math.floor(Math.min(a, b) / BUCKET_MS) * BUCKET_MS
  const end = Math.ceil(Math.max(a, b) / BUCKET_MS) * BUCKET_MS
  return {
    start: Math.max(extent.start, start),
    end: Math.min(extent.end, Math.max(end, start + BUCKET_MS)),
  }
}

export function LineChart({
  series,
  view,
  extent,
  playhead,
  selection,
  onSelect,
  onViewChange,
  selectedSubtopics = [],
  onToggleSubtopic,
}: Props) {
  const [ref, { width, height }] = useSize<HTMLDivElement>()
  const iw = Math.max(0, width - M.left - M.right)
  const ih = Math.max(0, height - M.top - M.bottom - VOL_H - VOL_GAP)
  const volTop = ih + VOL_GAP
  const fullH = volTop + VOL_H
  const cutoff = playhead ?? Infinity

  const layers = useMemo(
    () => series.map((s) => buildLayer(s, view, cutoff, playhead !== null)),
    [series, view, cutoff, playhead],
  )
  const volume = useMemo(() => volumeRows(layers), [layers])
  const bands = useMemo(
    () =>
      stack<VolumeRow>()
        .keys(layers.map((l) => l.series.id))
        .value((row, key) => row[key] ?? 0)(volume),
    [layers, volume],
  )

  const [lo, hi] = sentimentTarget(layers, view)
  const yDomain = useSmoothed(lo, hi)
  const volTarget = (max(volume, (d) => (d.t >= view.start && d.t <= view.end ? d.total : 0)) ?? 0) * 1.1 || 1
  const [, volMax] = useSmoothed(0, volTarget)

  const x = scaleUtc().domain([view.start, view.end]).range([0, iw])
  const y = scaleLinear().domain(yDomain).range([ih, 0])
  const vy = scaleLinear().domain([0, volMax]).range([VOL_H, 0])

  const maxVolume = useMemo(
    () => max(series, (s) => max(s.buckets, (b) => b.volume)) ?? 1,
    [series],
  )
  const r = scaleSqrt().domain([0, maxVolume]).range([1.4, 4.6])

  const path = line<{ t: number; s: number }>()
    .x((p) => x(p.t))
    .y((p) => y(p.s))
  const volumeArea = area<{ 0: number; 1: number; data: VolumeRow }>()
    .x((d) => x(d.data.t))
    .y0((d) => vy(d[0]))
    .y1((d) => vy(d[1]))

  // Hover and drag selection.
  const [hover, setHover] = useState<{ layer: number; t: number } | null>(null)
  const drag = useRef<{ anchor: number; px: number; moved: boolean } | null>(null)

  const localX = (e: PointerEvent) => e.clientX - e.currentTarget.getBoundingClientRect().left
  const localY = (e: PointerEvent) => e.clientY - e.currentTarget.getBoundingClientRect().top

  const nearest = (px: number, py: number) => {
    let best = null
    let bestD = Infinity
    for (let li = 0; li < layers.length; li++) {
      for (const p of layers[li].points) {
        if (p.grow < 1 || p.t < view.start || p.t > view.end) continue
        const d = Math.hypot(x(p.t) - px, (y(p.s) - py) * 0.5)
        if (d < bestD) {
          bestD = d
          best = { layer: li, t: p.t }
        }
      }
    }
    return best
  }

  const onPointerDown = (e: PointerEvent<SVGRectElement>) => {
    if ((e.metaKey || e.ctrlKey) && onToggleSubtopic) {
      const hit = nearest(localX(e), localY(e))
      if (hit) onToggleSubtopic(layers[hit.layer].series.id)
      return
    }
    e.currentTarget.setPointerCapture(e.pointerId)
    const px = localX(e)
    drag.current = { anchor: x.invert(px).getTime(), px, moved: false }
  }

  const onPointerMove = (e: PointerEvent<SVGRectElement>) => {
    const px = localX(e)
    const d = drag.current
    if (d) {
      if (!d.moved && Math.abs(px - d.px) < 4) return
      d.moved = true
      setHover(null)
      onSelect(snapRange(d.anchor, x.invert(clamp(px, 0, iw)).getTime(), extent))
      return
    }
    setHover(nearest(px, localY(e)))
  }

  const onPointerUp = () => {
    // A click without a drag clears the selection.
    if (drag.current && !drag.current.moved) onSelect(null)
    drag.current = null
  }

  // Wheel: vertical zooms around the cursor, horizontal pans. When the view
  // sits at the live edge, zoom pins the right edge so it stays live.
  const onWheel = useRef<(e: WheelEvent) => void>(() => {})
  const wheel = (e: WheelEvent) => {
    e.preventDefault()
    const width = view.end - view.start
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const dt = (e.deltaX / iw) * width
      onViewChange(clampView({ start: view.start + dt, end: view.end + dt }, extent))
      return
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const px = clamp(e.clientX - rect.left - M.left, 0, iw)
    const live = playhead !== null && view.end >= extent.end - HALF
    const t0 = live ? view.end : x.invert(px).getTime()
    const k = Math.exp(e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))
    onViewChange(clampView({ start: t0 - (t0 - view.start) * k, end: t0 + (view.end - t0) * k }, extent))
  }
  useLayoutEffect(() => {
    onWheel.current = wheel
  })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const handler = (e: WheelEvent) => onWheel.current(e)
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [ref])

  const hovered = hover && layers[hover.layer]?.points.find((p) => p.t === hover.t && p.grow === 1)
  const faded = (id: string) => selectedSubtopics.length > 0 && !selectedSubtopics.includes(id)

  // Direct labels at each line's visible end, nudged apart vertically.
  const labels = layers
    .map((l) => {
      const inView = l.points.filter((p) => p.t <= view.end)
      const end = l.tail ?? inView[inView.length - 1]
      return end && { id: l.series.id, name: l.series.name, color: l.series.color, x: Math.min(x(end.t), iw), y: y(end.s) }
    })
    .filter((l) => !!l)
    .sort((a, b) => a.y - b.y)
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 14)

  return (
    <div ref={ref} className="chart">
      {width > 0 && ih > 0 && (
        <svg width={width} height={height}>
          <defs>
            <clipPath id="plot-clip">
              <rect x={-6} y={-8} width={iw + 12} height={ih + 16} />
            </clipPath>
            <clipPath id="vol-clip">
              <rect width={iw} height={VOL_H} />
            </clipPath>
          </defs>
          <g transform={`translate(${M.left},${M.top})`}>
            {/* Sentiment axis */}
            {y.ticks(5).map((v) => (
              <g key={v} transform={`translate(0,${y(v)})`}>
                <line x2={iw} className="grid" />
                <text x={-10} dy="0.32em" textAnchor="end" className="tick">
                  {y.tickFormat(5)(v)}
                </text>
              </g>
            ))}
            <text className="axis-label" transform={`translate(${-M.left + 12},${ih / 2}) rotate(-90)`}>
              Sentiment (0 to 10)
            </text>

            {/* Volume band, on its own small axis */}
            <g transform={`translate(0,${volTop})`}>
              {vy
                .ticks(3)
                .filter((v) => v > 0)
                .map((v) => (
                  <g key={v} transform={`translate(0,${vy(v)})`}>
                    <line x2={iw} className="grid" />
                    <text x={-10} dy="0.32em" textAnchor="end" className="tick">
                      {formatCount(v)}
                    </text>
                  </g>
                ))}
              <g clipPath="url(#vol-clip)">
                {bands.map((band, i) => (
                  <path
                    key={band.key}
                    d={volumeArea(band) ?? undefined}
                    className="volume-band"
                    fill={muted(layers[i].series.color)}
                    opacity={faded(layers[i].series.id) ? 0.35 : 1}
                  />
                ))}
              </g>
              <line x2={iw} y1={VOL_H} y2={VOL_H} className="baseline" />
              <text className="axis-label" transform={`translate(${-M.left + 12},${VOL_H / 2}) rotate(-90)`}>
                Posts / {BUCKET_MS / 3_600_000}h
              </text>
            </g>

            {/* Time axis */}
            {x.ticks(Math.max(2, Math.floor(iw / 110))).map((d) => (
              <text key={+d} x={x(d)} y={fullH + 20} textAnchor="middle" className="tick">
                {formatTick(d)}
              </text>
            ))}
            <text className="axis-label" x={iw / 2} y={fullH + 40}>
              Time (UTC)
            </text>

            {selection && <SelectionBand x0={x(selection.start)} x1={x(selection.end)} iw={iw} h={fullH} />}
            {hovered && <line className="hover-line" x1={x(hovered.t)} x2={x(hovered.t)} y2={fullH} />}

            <g clipPath="url(#plot-clip)">
              {layers.map((l) => (
                <path
                  key={l.series.id}
                  d={path(l.tail ? [...l.points, l.tail] : l.points) ?? undefined}
                  className={selectedSubtopics.includes(l.series.id) ? 'series-line series-picked' : 'series-line'}
                  stroke={l.series.color}
                  opacity={faded(l.series.id) ? 0.2 : 1}
                />
              ))}
              {layers.map((l) =>
                l.points.map((p) => (
                  <circle
                    key={`${l.series.id}-${p.t}`}
                    cx={x(p.t)}
                    cy={y(p.s)}
                    r={r(p.v) * p.grow}
                    fill={l.series.color}
                    className="series-dot"
                    opacity={faded(l.series.id) ? 0.2 : 1}
                  />
                )),
              )}
            </g>

            {labels.map((l) => (
              <text
                key={l.id}
                x={l.x + 10}
                y={l.y}
                dy="0.32em"
                fill={l.color}
                className="series-label"
                opacity={faded(l.id) ? 0.35 : 1}
              >
                {l.name}
              </text>
            ))}

            {hovered && (
              <circle
                cx={x(hovered.t)}
                cy={y(hovered.s)}
                r={r(hovered.v) + 3.5}
                className="hover-ring"
                stroke={layers[hover.layer].series.color}
              />
            )}

            <rect
              className="overlay"
              width={iw}
              height={fullH}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
              onPointerLeave={() => setHover(null)}
            />
          </g>
        </svg>
      )}
      {hovered && (
        <HoverCard
          post={hovered.bucket.topPost}
          anchor={{ x: M.left + x(hovered.t), y: M.top + y(hovered.s) }}
          bounds={{ width, height }}
        />
      )}
    </div>
  )
}

function SelectionBand({ x0, x1, iw, h }: { x0: number; x1: number; iw: number; h: number }) {
  const a = clamp(x0, 0, iw)
  const b = clamp(x1, 0, iw)
  if (b <= a) return null
  return (
    <g>
      <rect className="selection" x={a} width={b - a} height={h} />
      {x0 >= 0 && <line className="selection-edge" x1={a} x2={a} y2={h} />}
      {x1 <= iw && <line className="selection-edge" x1={b} x2={b} y2={h} />}
    </g>
  )
}
