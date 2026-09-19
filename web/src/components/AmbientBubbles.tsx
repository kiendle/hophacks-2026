import { useEffect, useRef } from 'react'
import '../ambient.css'
import { SERIES_COLORS } from '../data/config'
import { useSize } from '../hooks/useSize'

/** Seconds for one full loop. Every motion is periodic in it, so the loop is seamless. */
const PERIOD = 78
/** Ghost positions trailing each bubble. */
const TRAIL = 5
/** Spacing between trail samples, as a share of the loop. */
const TRAIL_STEP = 0.022

interface Spec {
  title: string
  /** Resting position, 0 to 1 of the plot area: x reads as traction, y as sentiment. */
  x: number
  y: number
  /** Resting radius, as a share of the smaller viewport side. */
  r: number
  /** Drift amplitudes and whole-number frequencies, which keep the loop closed. */
  ax: number
  fx: number
  px: number
  ay: number
  fy: number
  py: number
  /** A burst of attention: traction climbs, sentiment usually dips. */
  surge?: { at: number; sharp: number; dx: number; dy: number; dr: number }
}

/** A dummy Movies topic: 2026 releases alongside a few classics. */
const MOVIES: Spec[] = [
  { title: 'The Odyssey', x: 0.62, y: 0.74, r: 0.085, ax: 0.05, fx: 1, px: 0.1, ay: 0.05, fy: 2, py: 0.3, surge: { at: 0.22, sharp: 26, dx: 0.22, dy: -0.12, dr: 0.5 } },
  { title: 'Avengers: Doomsday', x: 0.7, y: 0.42, r: 0.078, ax: 0.06, fx: 2, px: 0.55, ay: 0.04, fy: 1, py: 0.8, surge: { at: 0.62, sharp: 22, dx: 0.16, dy: -0.2, dr: 0.42 } },
  { title: 'Spider-Man: Brand New Day', x: 0.5, y: 0.6, r: 0.07, ax: 0.07, fx: 1, px: 0.7, ay: 0.05, fy: 2, py: 0.15, surge: { at: 0.86, sharp: 30, dx: 0.14, dy: 0.08, dr: 0.3 } },
  { title: 'Dune: Part Three', x: 0.44, y: 0.78, r: 0.062, ax: 0.05, fx: 2, px: 0.25, ay: 0.04, fy: 1, py: 0.5 },
  { title: 'Toy Story 5', x: 0.35, y: 0.5, r: 0.055, ax: 0.06, fx: 1, px: 0.4, ay: 0.06, fy: 3, py: 0.6 },
  { title: 'Supergirl', x: 0.8, y: 0.26, r: 0.045, ax: 0.05, fx: 3, px: 0.9, ay: 0.05, fy: 1, py: 0.2, surge: { at: 0.45, sharp: 34, dx: 0.1, dy: -0.14, dr: 0.28 } },
  { title: 'Inception', x: 0.22, y: 0.82, r: 0.05, ax: 0.04, fx: 1, px: 0.05, ay: 0.03, fy: 2, py: 0.45 },
  { title: 'Interstellar', x: 0.33, y: 0.88, r: 0.047, ax: 0.05, fx: 2, px: 0.6, ay: 0.03, fy: 1, py: 0.9 },
  { title: 'Parasite', x: 0.16, y: 0.66, r: 0.042, ax: 0.04, fx: 1, px: 0.35, ay: 0.04, fy: 2, py: 0.75 },
  { title: 'Spirited Away', x: 0.12, y: 0.86, r: 0.04, ax: 0.03, fx: 2, px: 0.8, ay: 0.03, fy: 3, py: 0.1 },
  { title: 'Mad Max: Fury Road', x: 0.88, y: 0.56, r: 0.044, ax: 0.06, fx: 1, px: 0.15, ay: 0.05, fy: 2, py: 0.65 },
  { title: 'The Dark Knight', x: 0.46, y: 0.16, r: 0.052, ax: 0.05, fx: 2, px: 0.45, ay: 0.04, fy: 1, py: 0.35 },
]

const TAU = Math.PI * 2
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** Periodic bump peaking at `at`, so surges repeat without a seam. */
const bump = (u: number, at: number, sharp: number) => Math.exp(sharp * (Math.cos(TAU * (u - at)) - 1))

function place(s: Spec, u: number, w: number, h: number) {
  const surge = s.surge ? bump(u, s.surge.at, s.surge.sharp) : 0
  const x = s.x + s.ax * Math.sin(TAU * (s.fx * u + s.px)) + surge * (s.surge?.dx ?? 0)
  const y = s.y + s.ay * Math.sin(TAU * (s.fy * u + s.py)) + surge * (s.surge?.dy ?? 0)
  const grow = 1 + 0.08 * Math.sin(TAU * (s.fx * u + s.py)) + surge * (s.surge?.dr ?? 0)
  return {
    cx: clamp(x, 0.05, 0.95) * w,
    // y is sentiment, so higher means further up the screen.
    cy: (1 - clamp(y, 0.08, 0.95)) * h,
    r: s.r * grow * Math.min(w, h),
  }
}

interface Props {
  /** Overall strength of the backdrop. */
  opacity?: number
  /** Faint titles under the larger bubbles. */
  labels?: boolean
  /** Multiplies the loop speed. */
  speed?: number
}

/**
 * Decorative backdrop for the landing page: the bubble view's motion, faded
 * out and non-interactive. It holds no app state and reads no live data.
 */
export function AmbientBubbles({ opacity = 0.5, labels = true, speed = 1 }: Props) {
  const [box, size] = useSize<HTMLDivElement>()
  const groups = useRef<(SVGGElement | null)[]>([])
  const { width, height } = size

  useEffect(() => {
    if (!width || !height) return
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    const draw = (u: number) => {
      MOVIES.forEach((spec, i) => {
        const g = groups.current[i]
        if (!g) return
        const head = place(spec, u, width, height)
        const circle = g.querySelector('.ambient-head') as SVGCircleElement
        circle.setAttribute('cx', String(head.cx))
        circle.setAttribute('cy', String(head.cy))
        circle.setAttribute('r', String(head.r))
        const text = g.querySelector('text')
        if (text) {
          text.setAttribute('x', String(head.cx))
          text.setAttribute('y', String(head.cy + head.r + 16))
        }
        g.querySelectorAll('.ambient-trail').forEach((dot, k) => {
          const past = place(spec, u - (k + 1) * TRAIL_STEP, width, height)
          dot.setAttribute('cx', String(past.cx))
          dot.setAttribute('cy', String(past.cy))
          dot.setAttribute('r', String(Math.max(2, past.r * (0.26 - k * 0.04))))
        })
      })
    }

    if (still) {
      draw(0.18)
      return
    }

    let raf = 0
    let elapsed = 0
    let last = performance.now()
    const frame = (now: number) => {
      elapsed += ((now - last) / 1000) * speed
      last = now
      draw((elapsed / PERIOD) % 1)
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)

    // Idle in a background tab, and pick up where it left off.
    const onVisibility = () => {
      cancelAnimationFrame(raf)
      if (document.visibilityState === 'visible') {
        last = performance.now()
        raf = requestAnimationFrame(frame)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelAnimationFrame(raf)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [width, height, speed])

  return (
    <div ref={box} className="ambient" style={{ opacity }} aria-hidden>
      {width > 0 && (
        <svg width={width} height={height}>
          {MOVIES.map((spec, i) => {
            const color = SERIES_COLORS[i % SERIES_COLORS.length]
            return (
              <g key={spec.title} ref={(el) => void (groups.current[i] = el)}>
                {Array.from({ length: TRAIL }, (_, k) => (
                  <circle key={k} className="ambient-trail" fill={color} opacity={0.42 - k * 0.07} />
                ))}
                <circle className="ambient-head" fill={color} opacity={0.78} />
                {labels && spec.r > 0.05 && (
                  <text className="ambient-label" textAnchor="middle">
                    {spec.title}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      )}
    </div>
  )
}
