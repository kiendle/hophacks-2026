import { utcFormat } from 'd3'
import type { TimeRange } from './data/types'

const fmtDay = utcFormat('%b %-d')
const fmtHour = utcFormat('%H:%M')
const fmtDayHour = utcFormat('%b %-d, %H:%M')

const isMidnight = (d: Date) => d.getUTCHours() === 0 && d.getUTCMinutes() === 0

export const formatTick = (d: Date) => (isMidnight(d) ? fmtDay(d) : fmtHour(d))

export const formatTime = (t: number) => fmtDayHour(new Date(t))

/** Drops the clock time when both ends fall on midnight. */
export function formatRange({ start, end }: TimeRange) {
  const a = new Date(start)
  const b = new Date(end)
  const f = isMidnight(a) && isMidnight(b) ? fmtDay : fmtDayHour
  return `${f(a)} – ${f(b)}`
}

/** 890, 3.1k, 12.4k, 125k, 1.2M */
export function formatCount(n: number) {
  for (const [v, unit] of [[1e6, 'M'], [1e3, 'k']] as const) {
    if (n >= v) {
      const k = n / v
      return `${k < 100 ? +k.toFixed(1) : Math.round(k)}${unit}`
    }
  }
  return String(n)
}
