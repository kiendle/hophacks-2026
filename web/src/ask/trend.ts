import { BUCKET_MS } from '../data/config'
import type { Bucket, Series, TimeRange } from '../data/types'
import { tractionAt } from '../data/window'
import type { TrendStat } from './protocol'

const DAY = 24 * 60 * 60 * 1000
const round = (v: number, places = 2) => Math.round(v * 10 ** places) / 10 ** places

interface Sample {
  t: number
  sentiment: number
  volume: number
  traction: number
}

/** Buckets whose midpoint falls inside the range. */
function samples(buckets: Bucket[], range: TimeRange, asOf: number): Sample[] {
  const out: Sample[] = []
  for (const b of buckets) {
    const t = b.start + BUCKET_MS / 2
    if (t < range.start || t > range.end) continue
    out.push({ t, sentiment: b.sentiment, volume: b.volume, traction: tractionAt(b, asOf) })
  }
  return out
}

/**
 * Least-squares fit of sentiment against time, weighted by post volume so
 * busy buckets count for more. Returns the slope in points per day, the fitted
 * endpoints and r2, which says how much of the movement the line explains.
 */
function fit(rows: Sample[]) {
  const w = rows.map((r) => Math.max(r.volume, 1))
  const sw = w.reduce((a, b) => a + b, 0)
  const mt = rows.reduce((a, r, i) => a + w[i] * r.t, 0) / sw
  const ms = rows.reduce((a, r, i) => a + w[i] * r.sentiment, 0) / sw
  let cov = 0
  let varT = 0
  let varS = 0
  rows.forEach((r, i) => {
    cov += w[i] * (r.t - mt) * (r.sentiment - ms)
    varT += w[i] * (r.t - mt) ** 2
    varS += w[i] * (r.sentiment - ms) ** 2
  })
  const slope = varT > 0 ? cov / varT : 0
  const first = rows[0].t
  const last = rows[rows.length - 1].t
  return {
    mean: ms,
    slopePerDay: slope * DAY,
    start: ms + slope * (first - mt),
    end: ms + slope * (last - mt),
    change: slope * (last - first),
    r2: varT > 0 && varS > 0 ? cov ** 2 / (varT * varS) : 0,
  }
}

function statFrom(subtopic: string, rows: Sample[]): TrendStat | null {
  if (rows.length < 2) return null
  const f = fit(rows)
  let min = rows[0]
  let max = rows[0]
  for (const r of rows) {
    if (r.sentiment < min.sentiment) min = r
    if (r.sentiment > max.sentiment) max = r
  }
  const traction = rows.reduce((a, r) => a + r.traction, 0)
  const firstT = rows[0].traction
  const lastT = rows[rows.length - 1].traction
  return {
    subtopic,
    buckets: rows.length,
    volume: Math.round(rows.reduce((a, r) => a + r.volume, 0)),
    sentiment: {
      start: round(f.start),
      end: round(f.end),
      change: round(f.change),
      slopePerDay: round(f.slopePerDay),
      mean: round(f.mean),
      r2: round(f.r2),
      min: { value: round(min.sentiment), time: new Date(min.t).toISOString() },
      max: { value: round(max.sentiment), time: new Date(max.t).toISOString() },
    },
    traction: {
      total: Math.round(traction),
      first: Math.round(firstT),
      last: Math.round(lastT),
      changeRatio: firstT > 0 ? round(lastT / firstT) : null,
    },
  }
}

/** One trend per subtopic in scope, plus a combined "all" row when there are several. */
export function computeTrends(series: Series[], range: TimeRange, asOf: number): TrendStat[] {
  const perSeries = series.map((s) => ({ id: s.id, rows: samples(s.buckets, range, asOf) }))
  const stats = perSeries.map(({ id, rows }) => statFrom(id, rows)).filter((s) => s !== null)
  if (stats.length < 2) return stats

  // Combined: volume-weighted sentiment per bucket across the subtopics in scope.
  const byTime = new Map<number, Sample>()
  for (const { rows } of perSeries)
    for (const r of rows) {
      const acc = byTime.get(r.t)
      if (acc) {
        acc.sentiment += r.sentiment * r.volume
        acc.volume += r.volume
        acc.traction += r.traction
      } else byTime.set(r.t, { t: r.t, sentiment: r.sentiment * r.volume, volume: r.volume, traction: r.traction })
    }
  const combined = [...byTime.values()]
    .sort((a, b) => a.t - b.t)
    .map((r) => ({ ...r, sentiment: r.volume > 0 ? r.sentiment / r.volume : NaN }))
  const all = statFrom('all', combined)
  return all ? [all, ...stats] : stats
}
