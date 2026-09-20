import { chartData } from '../data/chartData'
import { TREND_WINDOW_MS } from '../data/config'
import type { Series, TimeRange } from '../data/types'

export interface ChartReading {
  value: number
  date_from: string
  date_to: string
}

export interface ChartSummary {
  company_id: string
  line: 'points' | '24h trend'
  samples: number
  first: ChartReading
  last: ChartReading
  low: ChartReading
  high: ChartReading
}

/** Describe the actual plotted values, not a regression over storage buckets. */
export function chartSummaries(series: Series[], range: TimeRange, cutoff: number,
  intervalMs: number, display: 'both' | 'points' | 'trend'): ChartSummary[] {
  const out: ChartSummary[] = []
  for (const company of series) {
    const data = chartData(company, range, cutoff, intervalMs)
    const add = (line: ChartSummary['line'], points: { t: number; s: number; start: number }[]) => {
      const rows = points.filter(p => p.t > range.start && p.t <= range.end && Number.isFinite(p.s))
      if (!rows.length) return
      const reading = (p: typeof rows[number]): ChartReading => ({ value: Math.round(p.s * 10000) / 10000,
        date_from: new Date(p.start).toISOString(), date_to: new Date(p.t).toISOString() })
      out.push({ company_id: company.id, line, samples: rows.length,
        first: reading(rows[0]), last: reading(rows.at(-1)!),
        low: reading(rows.reduce((a, b) => a.s <= b.s ? a : b)),
        high: reading(rows.reduce((a, b) => a.s >= b.s ? a : b)) })
    }
    if (display !== 'trend') add('points', data.points.map(p => ({ ...p, start: p.bucket.start })))
    if (display !== 'points') add('24h trend', data.trend.map(p => ({ ...p, start: p.t - TREND_WINDOW_MS })))
  }
  return out
}
