import { hsl } from 'd3'
import { useEffect, useRef } from 'react'
import '../ambient.css'
import { SERIES_COLORS } from '../data/config'
import { formatCount } from '../format'
import { useSize } from '../hooks/useSize'
import { sentimentColor } from '../sentimentColor'

/** Seconds for one full loop. Every motion is periodic in it, so the loop is seamless. */
const PERIOD = 45
/** Ghost positions trailing each bubble. */
const TRAIL = 6
/** Spacing between trail samples, as a share of the loop. */
const TRAIL_STEP = 0.005

interface Spec {
  title: string
  /** Posts per bucket when nobody is talking about it. */
  quiet: number
  /** Sentiment when the noise dies down, 0 to 10. */
  mood: number
  /** How far sentiment moves at the peak of a wave; negative means backlash. */
  sway: number
  /** Divisiveness when quiet: the spread of sentiment, 0 to 5. */
  split: number
  /** How far divisiveness moves at the peak of a wave. */
  splitSway: number
  /** Whole-number frequencies and offsets of the constant drift on each axis. */
  fm: number
  pm: number
  fd: number
  pd: number
  /** Waves of attention across the loop: where each peaks, and how sharp it is. */
  waves: { at: number; sharp: number; size: number }[]
}

/**
 * A dummy Movies topic: 2026 releases alongside classics and a few famous
 * flops, so the sentiment scale spans red to green.
 */
const MOVIES: Spec[] = [
  { title: 'Cats', quiet: 180, mood: 1.4, sway: -0.9, split: 0.6, splitSway: 0.4, fm: 1, pm: 0.0, fd: 3, pd: 0.0, waves: [{ at: 0.0, sharp: 10, size: 0.9 }] },
  { title: 'Morbius', quiet: 240, mood: 1.8, sway: -1.1, split: 2.4, splitSway: 0.6, fm: 2, pm: 0.37, fd: 1, pd: 0.73, waves: [{ at: 0.618, sharp: 10, size: 0.9 }] },
  { title: 'Madame Web', quiet: 210, mood: 2.1, sway: -1.3, split: 4.2, splitSway: 1.1, fm: 3, pm: 0.74, fd: 2, pd: 0.46, waves: [{ at: 0.236, sharp: 10, size: 0.9 }] },
  { title: 'Supergirl', quiet: 480, mood: 2.5, sway: -1.6, split: 1.5, splitSway: 0.9, fm: 1, pm: 0.11, fd: 3, pd: 0.19, waves: [{ at: 0.854, sharp: 10, size: 0.9 }] },
  { title: 'Minecraft', quiet: 590, mood: 2.8, sway: -1.4, split: 3.3, splitSway: 0.7, fm: 2, pm: 0.48, fd: 1, pd: 0.92, waves: [{ at: 0.472, sharp: 10, size: 0.9 }] },
  { title: 'Gladiator II', quiet: 440, mood: 3.2, sway: -1.2, split: 0.9, splitSway: 1.0, fm: 3, pm: 0.85, fd: 2, pd: 0.65, waves: [{ at: 0.09, sharp: 10, size: 0.9 }] },
  { title: 'Toy Story 5', quiet: 520, mood: 3.6, sway: -1.5, split: 2.7, splitSway: 1.2, fm: 1, pm: 0.22, fd: 3, pd: 0.38, waves: [{ at: 0.708, sharp: 10, size: 0.9 }] },
  { title: 'Avengers', quiet: 1100, mood: 3.9, sway: -1.8, split: 4.5, splitSway: 0.8, fm: 2, pm: 0.59, fd: 1, pd: 0.11, waves: [{ at: 0.326, sharp: 10, size: 0.9 }] },
  { title: 'Frozen 3', quiet: 500, mood: 4.3, sway: -1.0, split: 1.2, splitSway: 1.3, fm: 3, pm: 0.96, fd: 2, pd: 0.84, waves: [{ at: 0.944, sharp: 10, size: 0.9 }] },
  { title: 'Barbie', quiet: 700, mood: 4.7, sway: -1.5, split: 3.6, splitSway: 0.6, fm: 1, pm: 0.33, fd: 3, pd: 0.57, waves: [{ at: 0.562, sharp: 10, size: 0.9 }] },
  { title: 'Spider-Man', quiet: 850, mood: 5.0, sway: 1.2, split: 2.1, splitSway: 1.1, fm: 2, pm: 0.7, fd: 1, pd: 0.3, waves: [{ at: 0.18, sharp: 10, size: 0.9 }] },
  { title: 'Wicked', quiet: 640, mood: 5.4, sway: -1.3, split: 4.0, splitSway: 0.9, fm: 3, pm: 0.07, fd: 2, pd: 0.03, waves: [{ at: 0.798, sharp: 10, size: 0.9 }] },
  { title: 'Deadpool', quiet: 780, mood: 5.8, sway: 1.1, split: 0.7, splitSway: 1.2, fm: 1, pm: 0.44, fd: 3, pd: 0.76, waves: [{ at: 0.416, sharp: 10, size: 0.9 }] },
  { title: 'The Odyssey', quiet: 900, mood: 6.1, sway: -1.7, split: 2.9, splitSway: 0.7, fm: 2, pm: 0.81, fd: 1, pd: 0.49, waves: [{ at: 0.034, sharp: 10, size: 0.9 }] },
  { title: 'Mad Max', quiet: 360, mood: 6.5, sway: 0.9, split: 1.8, splitSway: 0.8, fm: 3, pm: 0.18, fd: 2, pd: 0.22, waves: [{ at: 0.652, sharp: 10, size: 0.9 }] },
  { title: 'Oppenheimer', quiet: 560, mood: 6.9, sway: 0.8, split: 3.8, splitSway: 1.0, fm: 1, pm: 0.55, fd: 3, pd: 0.95, waves: [{ at: 0.271, sharp: 10, size: 0.9 }] },
  { title: 'Shrek 5', quiet: 460, mood: 7.2, sway: -0.9, split: 1.0, splitSway: 1.1, fm: 2, pm: 0.92, fd: 1, pd: 0.68, waves: [{ at: 0.889, sharp: 10, size: 0.9 }] },
  { title: 'Interstellar', quiet: 380, mood: 7.6, sway: 0.7, split: 2.5, splitSway: 0.8, fm: 3, pm: 0.29, fd: 2, pd: 0.41, waves: [{ at: 0.507, sharp: 10, size: 0.9 }] },
  { title: 'Parasite', quiet: 300, mood: 8.0, sway: -1.0, split: 4.3, splitSway: 1.2, fm: 1, pm: 0.66, fd: 3, pd: 0.14, waves: [{ at: 0.125, sharp: 10, size: 0.9 }] },
  { title: 'Dune III', quiet: 600, mood: 8.3, sway: 0.6, split: 1.6, splitSway: 0.9, fm: 2, pm: 0.03, fd: 1, pd: 0.87, waves: [{ at: 0.743, sharp: 10, size: 0.9 }] },
  { title: 'Inception', quiet: 320, mood: 8.7, sway: 0.5, split: 3.1, splitSway: 0.7, fm: 3, pm: 0.4, fd: 2, pd: 0.6, waves: [{ at: 0.361, sharp: 10, size: 0.9 }] },
  { title: 'Dark Knight', quiet: 420, mood: 9.1, sway: 0.4, split: 0.8, splitSway: 0.6, fm: 1, pm: 0.77, fd: 3, pd: 0.33, waves: [{ at: 0.979, sharp: 10, size: 0.9 }] },
  { title: 'Spirited Away', quiet: 280, mood: 9.5, sway: 0.3, split: 2.2, splitSway: 0.5, fm: 2, pm: 0.14, fd: 1, pd: 0.06, waves: [{ at: 0.597, sharp: 10, size: 0.9 }] },
]

const TAU = Math.PI * 2
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** How loud a movie is at `u`, 0 to 1. Periodic, so waves repeat without a seam. */
function buzz(s: Spec, u: number): number {
  let loud = 0
  for (const w of s.waves) {
    const wide = w.sharp * 0.6
    loud = Math.max(loud, w.size * Math.exp(wide * (Math.cos(TAU * (u - w.at)) - 1)))
    // A second, smaller swell half a loop later, so several movies peak at once.
    loud = Math.max(loud, 0.6 * w.size * Math.exp(wide * 1.5 * (Math.cos(TAU * (u - w.at - 0.5)) - 1)))
  }
  // A floor keeps quiet movies big enough to carry their own labels.
  return 0.18 + 0.82 * loud
}

/**
 * One state for a movie at time `u`, on the same axes as the bubble view:
 * divisiveness across, sentiment up, posts as area. A wave of attention swells
 * the bubble and pulls both axes, and a slow drift keeps every bubble moving
 * even between waves.
 */
function stateAt(s: Spec, u: number) {
  const loud = buzz(s, u)
  return {
    loud,
    posts: s.quiet * (1 + 12 * loud),
    sentiment: clamp(s.mood + 0.55 * Math.sin(TAU * (s.fm * u + s.pm)) + s.sway * loud, 0.3, 9.9),
    spread: clamp(s.split + 0.45 * Math.sin(TAU * (s.fd * u + s.pd)) + s.splitSway * loud, 0.15, 4.85),
  }
}

/** Sentiment runs 0 to 10, so its spread cannot exceed 5. */
const MAX_SPREAD = 5

const MAX_POSTS = Math.max(...MOVIES.map((s) => s.quiet * 13))

/** Screen position of a value on each axis, matching where the bubbles sit. */
const plotX = (spread: number, w: number) => (0.05 + 0.8 * clamp(spread / MAX_SPREAD, 0, 1)) * w
const plotY = (sentiment: number, h: number) => (1 - (0.08 + 0.84 * (sentiment / 10))) * h

function place(s: Spec, u: number, w: number, h: number) {
  const now = stateAt(s, u)
  return {
    ...now,
    // Divisiveness runs left to right, sentiment bottom to top, area is posts.
    cx: plotX(now.spread, w),
    cy: plotY(now.sentiment, h),
    r: Math.sqrt(now.posts / MAX_POSTS) * 0.231 * Math.min(w, h),
  }
}

/** How close the pointer must get to an edge before its axis shows. */
const REVEAL_PX = 180

/** Tiny glyphs, matching the icons used in the real views. */
const POSTS_PATH = 'M6 5.5h12M6 10h12M6 14.5h12M6 19h7'
const SPREAD_PATH = 'M5.5 19 18.5 6M3 16.5 7.5 21M18.5 19 5.5 6M21 16.5 16.5 21'

interface Props {
  /** Overall strength of the backdrop. */
  opacity?: number
  /** Titles, sentiment and counts inside the bubbles. */
  labels?: boolean
  /** Multiplies the loop speed. */
  speed?: number
}

/**
 * Decorative backdrop for the landing page: movies ride their own waves of
 * attention across a sentiment and engagement field. Non-interactive, holds no
 * app state, reads no live data.
 */
export function AmbientBubbles({ opacity = 0.44, labels = true, speed = 1 }: Props) {
  const [box, size] = useSize<HTMLDivElement>()
  const groups = useRef<(SVGGElement | null)[]>([])
  const axes = useRef<{ x: SVGGElement | null; y: SVGGElement | null }>({ x: null, y: null })
  const { width, height } = size

  useEffect(() => {
    if (!width || !height) return

    const draw = (u: number, writeText: boolean) => {
      MOVIES.forEach((spec, i) => {
        const g = groups.current[i]
        if (!g) return
        const head = place(spec, u, width, height)

        const circle = g.querySelector('.ambient-head') as SVGCircleElement
        circle.setAttribute('cx', String(head.cx))
        circle.setAttribute('cy', String(head.cy))
        circle.setAttribute('r', String(head.r))

        const title = g.querySelector('.ambient-label') as SVGTextElement | null
        const score = g.querySelector('.ambient-score') as SVGTextElement | null
        const stats = g.querySelector('.ambient-stats') as SVGGElement | null
        if (title && score) {
          const fontSize = clamp(head.r * 0.16, 13, 34)
          // Small bubbles carry the title only; the numbers would be noise.
          const showScore = head.r > 58
          title.setAttribute('x', String(head.cx))
          title.setAttribute('y', String(head.cy - (showScore ? head.r * 0.34 : 0)))
          title.setAttribute('dy', showScore ? '0' : '0.35em')
          title.setAttribute('font-size', String(fontSize))
          score.setAttribute('opacity', showScore ? '1' : '0')
          score.setAttribute('x', String(head.cx))
          score.setAttribute('y', String(head.cy + head.r * 0.12))
          score.setAttribute('font-size', String(fontSize * 1.7))
          score.setAttribute('fill', sentimentColor(head.sentiment))
          if (writeText) score.textContent = head.sentiment.toFixed(1)
        }
        if (stats) {
          // Posts and engagement, shown once the bubble is big enough to hold them.
          const fontSize = clamp(head.r * 0.1, 12, 22)
          const room = head.r > 98
          stats.setAttribute('opacity', room ? '1' : '0')
          stats.setAttribute('font-size', String(fontSize))
          const [postText, pullText] = stats.querySelectorAll('text')
          const [postIcon, pullIcon] = stats.querySelectorAll('path')
          if (writeText) {
            postText.textContent = formatCount(Math.round(head.posts))
            pullText.textContent = head.spread.toFixed(1)
          }
          const glyph = fontSize * 1.45
          const gap = fontSize * 0.5
          const postW = glyph + gap + postText.getComputedTextLength()
          const pullW = glyph + gap + pullText.getComputedTextLength()
          const left = head.cx - (postW + pullW + fontSize * 1.4) / 2
          const baseline = head.cy + head.r * 0.52
          const put = (icon: Element, text: Element, x: number) => {
            icon.setAttribute('transform', `translate(${x},${baseline - glyph * 0.78}) scale(${glyph / 24})`)
            text.setAttribute('x', String(x + glyph + gap))
            text.setAttribute('y', String(baseline))
          }
          put(postIcon, postText, left)
          put(pullIcon, pullText, left + postW + fontSize * 1.4)
        }

        g.querySelectorAll('.ambient-trail').forEach((dot, k) => {
          const past = place(spec, u - (k + 1) * TRAIL_STEP, width, height)
          dot.setAttribute('cx', String(past.cx))
          dot.setAttribute('cy', String(past.cy))
          dot.setAttribute('r', String(Math.max(2, past.r * (0.16 - k * 0.022))))
        })
      })
    }

    // Reduced motion slows the drift instead of freezing it, so the page still moves.
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const rate = speed * (calm ? 0.4 : 1)

    let raf = 0
    let elapsed = 0
    let last = performance.now()
    let lastText = 0
    const frame = (now: number) => {
      elapsed += ((now - last) / 1000) * rate
      last = now
      // Digits settle instead of flickering every frame.
      const writeText = now - lastText > 600
      if (writeText) lastText = now
      draw((elapsed / PERIOD) % 1, writeText)
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

  // The axes surface as the pointer nears the bottom or right edge.
  useEffect(() => {
    const el = box.current
    if (!el || !width || !height) return
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let queued = false
    let point: { x: number; y: number } | null = null

    const apply = () => {
      queued = false
      const rect = el.getBoundingClientRect()
      const near = (distance: number) => (point ? clamp(1 - distance / REVEAL_PX, 0, 1) : 0)
      const show = {
        x: point ? near(rect.bottom - point.y) : 0,
        y: point ? near(rect.right - point.x) : 0,
      }
      for (const key of ['x', 'y'] as const) {
        const g = axes.current[key]
        if (!g) continue
        const now = Number(g.style.opacity || 0)
        // Quick to appear, slower to leave, with a beat so it does not flicker.
        g.style.transition = calm ? 'none' : show[key] > now ? 'opacity 160ms ease-out' : 'opacity 320ms ease-out 120ms'
        g.style.opacity = String(show[key])
      }
    }

    const onMove = (e: MouseEvent) => {
      point = { x: e.clientX, y: e.clientY }
      if (!queued) {
        queued = true
        requestAnimationFrame(apply)
      }
    }
    const onLeave = () => {
      point = null
      apply()
    }
    window.addEventListener('mousemove', onMove)
    document.addEventListener('mouseleave', onLeave)
    return () => {
      window.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseleave', onLeave)
    }
  }, [box, width, height])

  return (
    <div ref={box} className="ambient" style={{ opacity }} aria-hidden>
      {width > 0 && (
        <svg width={width} height={height}>
          <g className="ambient-axis" ref={(el) => void (axes.current.x = el)} style={{ opacity: 0 }}>
            {[0, 1, 2, 3, 4, 5].map((v) => (
              <text key={v} className="tick" x={plotX(v, width)} y={height - 34} textAnchor="middle">
                {v}
              </text>
            ))}
            <line x1={0} x2={width} y1={height - 24} y2={height - 24} className="ambient-axis-line" />
            <text className="axis-label" x={width / 2} y={height - 8} textAnchor="middle">
              Divisiveness (0 to 5)
            </text>
          </g>
          <g className="ambient-axis" ref={(el) => void (axes.current.y = el)} style={{ opacity: 0 }}>
            {[0, 2, 4, 6, 8, 10].map((v) => (
              <text key={v} className="tick" x={width - 46} y={plotY(v, height)} dy="0.32em" textAnchor="end">
                {v}
              </text>
            ))}
            <line x1={width - 36} x2={width - 36} y1={0} y2={height} className="ambient-axis-line" />
            <text
              className="axis-label"
              transform={`translate(${width - 14},${height / 2}) rotate(-90)`}
              textAnchor="middle"
            >
              Sentiment (0 to 10)
            </text>
          </g>

          {MOVIES.map((spec, i) => {
            // Muted well below the real views: this is a backdrop, not the chart.
            const base = hsl(SERIES_COLORS[i % SERIES_COLORS.length])
            base.s *= 0.72
            base.l = Math.min(0.78, base.l + 0.05)
            const fill = base.formatHex()
            return (
              <g key={spec.title} ref={(el) => void (groups.current[i] = el)}>
                {Array.from({ length: TRAIL }, (_, k) => (
                  <circle key={k} className="ambient-trail" fill={fill} opacity={0.4 - k * 0.055} />
                ))}
                <circle className="ambient-head" fill={fill} opacity={0.58} />
                {labels && (
                  <>
                    <text className="ambient-label" textAnchor="middle">
                      {spec.title}
                    </text>
                    <text className="ambient-score" textAnchor="middle" />
                    <g className="ambient-stats">
                      <path d={POSTS_PATH} />
                      <text />
                      <path d={SPREAD_PATH} />
                      <text />
                    </g>
                  </>
                )}
              </g>
            )
          })}
        </svg>
      )}
    </div>
  )
}
