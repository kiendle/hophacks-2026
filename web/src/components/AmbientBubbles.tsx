import { useEffect, useRef } from 'react'
import '../ambient.css'
import { SERIES_COLORS } from '../data/config'
import { formatCount } from '../format'
import { useSize } from '../hooks/useSize'
import { sentimentColor } from '../sentimentColor'

/** Seconds for one full loop. Every motion is periodic in it, so the loop is seamless. */
const PERIOD = 17
/** Ghost positions trailing each bubble. */
const TRAIL = 6
/** Spacing between trail samples, as a share of the loop. */
const TRAIL_STEP = 0.005

interface Spec {
  title: string
  /** Posts per bucket when nobody is talking about it. */
  quiet: number
  /** Engagement each post earns, relative to the others. */
  pull: number
  /** Sentiment when the noise dies down, 0 to 10. */
  mood: number
  /** How far sentiment moves at the peak of a wave; negative means backlash. */
  sway: number
  /** Waves of attention across the loop: where each peaks, and how sharp it is. */
  waves: { at: number; sharp: number; size: number }[]
}

/**
 * A dummy Movies topic: 2026 releases alongside classics and a few famous
 * flops, so the sentiment scale spans red to green.
 */
const MOVIES: Spec[] = [
  { title: 'The Odyssey', quiet: 900, pull: 2.8, mood: 7.6, sway: -2.6, waves: [{ at: 0.02, sharp: 9, size: 1 }, { at: 0.55, sharp: 22, size: 0.6 }] },
  { title: 'Avengers', quiet: 1100, pull: 3.2, mood: 6.2, sway: -3.4, waves: [{ at: 0.3, sharp: 8, size: 1 }] },
  { title: 'Spider-Man', quiet: 850, pull: 2.4, mood: 6.8, sway: 2.1, waves: [{ at: 0.62, sharp: 9, size: 0.95 }] },
  { title: 'Dune III', quiet: 600, pull: 1.6, mood: 8.2, sway: 1.2, waves: [{ at: 0.16, sharp: 12, size: 0.9 }] },
  { title: 'Toy Story 5', quiet: 520, pull: 1.2, mood: 6, sway: -2.8, waves: [{ at: 0.46, sharp: 11, size: 0.9 }] },
  { title: 'Supergirl', quiet: 480, pull: 1.8, mood: 4.6, sway: -3.1, waves: [{ at: 0.78, sharp: 10, size: 0.9 }] },
  { title: 'Morbius', quiet: 240, pull: 3.6, mood: 2.6, sway: -1.8, waves: [{ at: 0.24, sharp: 14, size: 0.85 }] },
  { title: 'Cats', quiet: 180, pull: 3, mood: 2.2, sway: -1.4, waves: [{ at: 0.52, sharp: 14, size: 0.8 }] },
  { title: 'Madame Web', quiet: 210, pull: 2.6, mood: 3.2, sway: -2.2, waves: [{ at: 0.88, sharp: 13, size: 0.85 }] },
  { title: 'Inception', quiet: 320, pull: 1, mood: 8.8, sway: 0.9, waves: [{ at: 0.4, sharp: 13, size: 0.8 }] },
  { title: 'Spirited Away', quiet: 280, pull: 0.8, mood: 9.1, sway: 0.6, waves: [{ at: 0.7, sharp: 13, size: 0.8 }] },
  { title: 'Mad Max', quiet: 360, pull: 1.4, mood: 7.4, sway: 1.6, waves: [{ at: 0.08, sharp: 12, size: 0.85 }] },
  { title: 'Dark Knight', quiet: 420, pull: 1.5, mood: 9.2, sway: 0.5, waves: [{ at: 0.34, sharp: 12, size: 0.8 }] },
  { title: 'Interstellar', quiet: 380, pull: 1.3, mood: 8.4, sway: 1.1, waves: [{ at: 0.66, sharp: 12, size: 0.8 }] },
  { title: 'Parasite', quiet: 300, pull: 1.7, mood: 8.6, sway: -1.6, waves: [{ at: 0.94, sharp: 13, size: 0.75 }] },
  { title: 'Barbie', quiet: 700, pull: 2.2, mood: 7, sway: -2.4, waves: [{ at: 0.2, sharp: 10, size: 0.95 }] },
  { title: 'Oppenheimer', quiet: 560, pull: 1.9, mood: 8, sway: 1.4, waves: [{ at: 0.84, sharp: 11, size: 0.9 }] },
]

const TAU = Math.PI * 2
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** How loud a movie is at `u`, 0 to 1. Periodic, so waves repeat without a seam. */
function buzz(s: Spec, u: number): number {
  let loud = 0
  for (const w of s.waves) loud = Math.max(loud, w.size * Math.exp(w.sharp * (Math.cos(TAU * (u - w.at)) - 1)))
  // A floor keeps quiet movies big enough to carry their own labels.
  return 0.18 + 0.82 * loud
}

/**
 * One state for a movie at time `u`. Everything moves together: a wave of
 * attention lifts posts, engagement and size at once, and pulls sentiment
 * along with it, so the drift reads the way the real bubble view does.
 */
function stateAt(s: Spec, u: number) {
  const loud = buzz(s, u)
  const posts = s.quiet * (1 + 22 * loud)
  const engagement = posts * s.pull * (1 + 9 * loud)
  return {
    loud,
    posts,
    engagement,
    sentiment: clamp(s.mood + s.sway * loud, 0.3, 9.9),
  }
}

/**
 * Engagement spread across the whole loop, trimmed to the middle 90% so the
 * quiet crowd uses the width instead of stacking against the left edge. Peaks
 * beyond it simply pin to the right.
 */
const RANGE = (() => {
  const all: number[] = []
  for (const s of MOVIES) for (let i = 0; i < 240; i++) all.push(Math.log10(stateAt(s, i / 240).engagement))
  all.sort((a, b) => a - b)
  return { lo: all[Math.floor(all.length * 0.05)], hi: all[Math.floor(all.length * 0.95)] }
})()

const MAX_POSTS = Math.max(...MOVIES.map((s) => s.quiet * 23))

/** Tick positions in log10 units, at 1, 2 and 5 times each power of ten. */
function logTicks(lo: number, hi: number): number[] {
  const out: number[] = []
  for (let p = Math.floor(lo); p <= Math.ceil(hi); p++)
    for (const m of [1, 2, 5]) {
      const v = Math.log10(m) + p
      if (v >= lo && v <= hi) out.push(v)
    }
  return out
}

/** Screen position of a value on each axis, matching where the bubbles sit. */
const plotX = (log: number, w: number) => (0.05 + 0.8 * clamp((log - RANGE.lo) / (RANGE.hi - RANGE.lo), 0, 1)) * w
const plotY = (sentiment: number, h: number) => (1 - (0.08 + 0.84 * (sentiment / 10))) * h

function place(s: Spec, u: number, w: number, h: number) {
  const now = stateAt(s, u)
  return {
    ...now,
    // Engagement runs left to right, sentiment bottom to top, area is posts.
    cx: plotX(Math.log10(now.engagement), w),
    cy: plotY(now.sentiment, h),
    r: Math.sqrt(now.posts / MAX_POSTS) * 0.42 * Math.min(w, h),
  }
}

/** How close the pointer must get to an edge before its axis shows. */
const REVEAL_PX = 180

/** Tiny glyphs, matching the icons used in the real views. */
const POSTS_PATH = 'M6 5.5h12M6 10h12M6 14.5h12M6 19h7'
const PULL_PATH = 'M4 17.5 10 11.5l3.5 3.5L20 8.5M14.5 8.5H20V14'

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
export function AmbientBubbles({ opacity = 0.55, labels = true, speed = 1 }: Props) {
  const [box, size] = useSize<HTMLDivElement>()
  const groups = useRef<(SVGGElement | null)[]>([])
  const axes = useRef<{ x: SVGGElement | null; y: SVGGElement | null }>({ x: null, y: null })
  const { width, height } = size

  useEffect(() => {
    if (!width || !height) return

    const draw = (u: number) => {
      MOVIES.forEach((spec, i) => {
        const g = groups.current[i]
        if (!g) return
        const head = place(spec, u, width, height)

        // Quiet movies fade back out rather than sitting parked at the left.
        g.setAttribute('opacity', String(clamp(0.42 + head.loud * 1.3, 0, 1)))

        const circle = g.querySelector('.ambient-head') as SVGCircleElement
        circle.setAttribute('cx', String(head.cx))
        circle.setAttribute('cy', String(head.cy))
        circle.setAttribute('r', String(head.r))

        const title = g.querySelector('.ambient-label') as SVGTextElement | null
        const score = g.querySelector('.ambient-score') as SVGTextElement | null
        const stats = g.querySelector('.ambient-stats') as SVGGElement | null
        if (title && score) {
          const fontSize = clamp(head.r * 0.16, 13, 34)
          title.setAttribute('x', String(head.cx))
          title.setAttribute('y', String(head.cy - head.r * 0.34))
          title.setAttribute('font-size', String(fontSize))
          score.setAttribute('x', String(head.cx))
          score.setAttribute('y', String(head.cy + head.r * 0.12))
          score.setAttribute('font-size', String(fontSize * 1.7))
          score.setAttribute('fill', sentimentColor(head.sentiment))
          score.textContent = head.sentiment.toFixed(1)
        }
        if (stats) {
          // Posts and engagement, shown once the bubble is big enough to hold them.
          const fontSize = clamp(head.r * 0.1, 12, 22)
          const room = head.r > 70
          stats.setAttribute('opacity', room ? '1' : '0')
          stats.setAttribute('font-size', String(fontSize))
          const [postText, pullText] = stats.querySelectorAll('text')
          const [postIcon, pullIcon] = stats.querySelectorAll('path')
          postText.textContent = formatCount(Math.round(head.posts))
          pullText.textContent = formatCount(Math.round(head.engagement))
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
    const frame = (now: number) => {
      elapsed += ((now - last) / 1000) * rate
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
            {logTicks(RANGE.lo, RANGE.hi).map((v) => (
              <text key={v} className="tick" x={plotX(v, width)} y={height - 34} textAnchor="middle">
                {formatCount(Math.round(10 ** v))}
              </text>
            ))}
            <line x1={0} x2={width} y1={height - 24} y2={height - 24} className="ambient-axis-line" />
            <text className="axis-label" x={width / 2} y={height - 8} textAnchor="middle">
              Traction, past 24h
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
            const fill = SERIES_COLORS[i % SERIES_COLORS.length]
            return (
              <g key={spec.title} ref={(el) => void (groups.current[i] = el)}>
                {Array.from({ length: TRAIL }, (_, k) => (
                  <circle key={k} className="ambient-trail" fill={fill} opacity={0.4 - k * 0.055} />
                ))}
                <circle className="ambient-head" fill={fill} opacity={0.42} />
                {labels && (
                  <>
                    <text className="ambient-label" textAnchor="middle">
                      {spec.title}
                    </text>
                    <text className="ambient-score" textAnchor="middle" />
                    <g className="ambient-stats">
                      <path d={POSTS_PATH} />
                      <text />
                      <path d={PULL_PATH} />
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
