export type Domain = [number, number]
const MIN_SPAN = 4
export const AXIS_SHRINK_DELAY_MS = 2500

/** Whole-number limits, modest padding, and a minimum span avoid magnifying noise. */
export function sentimentDomain(lo: number, hi: number): Domain {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [3, 7]
  const padding = Math.max(0.25, (hi - lo) * 0.1)
  let lower = Math.max(0, Math.floor(lo - padding))
  let upper = Math.min(10, Math.ceil(hi + padding))
  if (upper - lower < MIN_SPAN) {
    lower = Math.max(0, Math.min(10 - MIN_SPAN, Math.floor((lo + hi - MIN_SPAN) / 2)))
    upper = lower + MIN_SPAN
  }
  return [lower, upper]
}

/** Expand immediately; require a stable smaller target before contracting. */
export class SentimentAxis {
  private domain: Domain | null = null
  private pending: { target: Domain; since: number } | null = null

  update(target: Domain, now: number): Domain {
    if (!this.domain) { this.domain = target; return target }
    const [lo, hi] = this.domain
    if (target[0] < lo || target[1] > hi) {
      this.domain = [Math.min(lo, target[0]), Math.max(hi, target[1])]
      this.pending = this.domain[0] === target[0] && this.domain[1] === target[1]
        ? null : { target, since: now }
    } else if (target[0] === lo && target[1] === hi) {
      this.pending = null
    } else {
      if (!this.pending || this.pending.target[0] !== target[0] || this.pending.target[1] !== target[1]) {
        this.pending = { target, since: now }
      } else if (now - this.pending.since >= AXIS_SHRINK_DELAY_MS) {
        this.domain = target
        this.pending = null
      }
    }
    return this.domain
  }
}
