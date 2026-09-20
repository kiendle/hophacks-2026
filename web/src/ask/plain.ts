/**
 * Plain words, for anything a person reads.
 *
 * `plainText` is the same rule the server enforces in harness/steps.py `plain()`: a reader gets
 * commas, full stops, "and" and "to", never a dash, a middle dot, an arrow or a semicolon. Post
 * text is someone else's words and never goes through it.
 */

// A semicolon, or a run of them, is ONE full stop, and the word after it gets a capital. The whole
// run is matched at once, which is what keeps the rule idempotent.
function unsemicolon(_whole: string, tail: string, at: number, all: string) {
  const before = all.slice(0, at)
  const opensLine = before === '' || before.endsWith('\n')
  const stop = opensLine || /[.!?]$/.test(before) ? '' : '.'
  if (!tail) return stop
  if ('.,!?'.includes(tail)) return stop ? tail : ''
  return (opensLine ? '' : `${stop} `) + tail.toUpperCase()
}

export function plainText(value: unknown): string {
  if (typeof value !== 'string') return ''
  return value
    .replace(/[^\S\n]{2,}/g, ' ')
    .replace(/[^\S\n]*(?:[←→↔⇒⇨➡]+|-{1,2}>|=>)[^\S\n]*/g, ' to ')
    .replace(/[^\S\n]*[‒–—―]+[^\S\n]*/g, ', ')
    .replace(/[^\S\n]*[·•‣▪・]+[^\S\n]*/g, ', ')
    .replace(/(?<=\S)[^\S\n]+(?:-{1,2}[^\S\n]+)+(?=\S)/g, ', ')
    .replace(/[^\S\n]*(?:;[^\S\n]*)+(\S?)/g, unsemicolon)
    .replace(/[^\S\n]{2,}/g, ' ')
    .replace(/[^\S\n]+([,.])/g, '$1')
    .replace(/(?:,[^\S\n]*){2,}/g, ', ')
    .replace(/,[^\S\n]*\./g, '.')
    .replace(/^[^\S\n]*,[^\S\n]*|[^\S\n]*,[^\S\n]*$/gm, '')
}

// A title or a caption can reach us from a model or from a post. A right to left override reverses
// every character after it and a zero width mark is invisible while it is still there, so both go
// before anything is measured or shown. Written as numbers on purpose: spelled out, this class would
// be a row of invisible marks inside the guard that removes them.
const HIDDEN_RANGES = [[0, 8], [11, 31], [127, 159], [0xad, 0xad], [0x200b, 0x200f], [0x202a, 0x202e], [0x2060, 0x2069], [0xfeff, 0xfeff]]
const HIDDEN = new RegExp(`[${HIDDEN_RANGES.map(([low, high]) => String.fromCharCode(low, 0x2d, high)).join('')}]`, 'g')

/** One line of our own words: hidden marks out, spaces collapsed, plain punctuation, cut to `limit`. */
export function readable(value: unknown, limit = 200): string {
  if (typeof value !== 'string' && typeof value !== 'number') return ''
  const clean = plainText(String(value).replace(HIDDEN, '').replace(/\s+/g, ' ')).trim()
  return clean.length > limit ? `${clean.slice(0, limit - 1).trimEnd()}...` : clean
}

/** The same, kept to one line but never shortened with words of our own. */
export const oneLine = (value: unknown, limit = 160) => readable(value, limit)

export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`

export const number = (value: unknown) => Number(value || 0).toLocaleString('en-US')

/** "9 seconds", "1 minute 5 seconds", "2 minutes". */
export function formatDuration(ms: unknown): string {
  const value = ms === null || ms === undefined || ms === '' ? NaN : Number(ms)
  if (!Number.isFinite(value) || value < 0) return ''
  const total = Math.round(value / 1000)
  if (total < 60) return plural(total, 'second')
  const seconds = total % 60
  return plural(Math.floor(total / 60), 'minute') + (seconds ? ` ${plural(seconds, 'second')}` : '')
}

/** A step's own time, one decimal under ten seconds: "3.5 seconds", "28 seconds". */
export function durationWords(ms: unknown): string {
  const value = ms === null || ms === undefined || ms === '' ? NaN : Number(ms)
  if (!Number.isFinite(value) || value < 0) return ''
  if (value >= 9950) return formatDuration(value)
  const seconds = Math.round(value / 100) / 10
  return `${seconds} ${seconds === 1 ? 'second' : 'seconds'}`
}

/** "4 minutes 12 seconds", "12 seconds": what a confirm button has left. */
export function countdownWords(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return plural(Math.round(seconds), 'second')
  const rest = Math.round(seconds) % 60
  return plural(Math.floor(seconds / 60), 'minute') + (rest ? ` ${plural(rest, 'second')}` : '')
}

/** The one line a finished activity list collapses into: "4 steps, 22 seconds". */
export function summarizeSteps(steps: { ok?: boolean | null }[] | unknown, totalMs?: unknown): string {
  const list = Array.isArray(steps) ? (steps as { ok?: boolean | null }[]) : []
  if (!list.length) return 'No steps'
  const failed = list.filter((step) => step && step.ok === false).length
  const parts = [plural(list.length, 'step')]
  if (failed) parts.push(`${failed} did not finish`)
  const duration = formatDuration(totalMs)
  if (duration) parts.push(duration)
  return parts.join(', ')
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const DAYS_IN = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

function dayParts(value: unknown, shift = 0) {
  const found = /^(\d{4})-(\d{2})-(\d{2})/.exec(typeof value === 'string' ? value.trim() : '')
  if (!found) return null
  let year = Number(found[1])
  let month = Number(found[2])
  let day = Number(found[3]) + shift
  while (day < 1) {
    month -= 1
    if (month < 1) {
      month = 12
      year -= 1
    }
    day += DAYS_IN[month - 1] + (month === 2 && year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 1 : 0)
  }
  if (month < 1 || month > 12 || day > 31) return null
  return { year, month, day }
}

/** "Sep 10" from a 2026-09-10 day, and nothing from anything else. */
export function dayWords(value: unknown, shift = 0): string {
  const parts = dayParts(value, shift)
  return parts ? `${MONTHS[parts.month - 1]} ${parts.day}` : ''
}

/** A bucket label: a date reads as Sep 10, a live clock time like 19:05 is left alone. */
export const dayLabel = (value: unknown) => dayWords(value) || oneLine(value, 24)

/** "Sep 6 to Sep 12, 2026". The end day is the first day NOT observed, so it is shown one day back. */
export function windowWords(from: unknown, to: unknown): string {
  const low = dayWords(from)
  const high = dayWords(to, -1)
  const span = low && high && low !== high ? `${low} to ${high}` : low || high
  if (!span) return ''
  const parts = dayParts(to, -1) || dayParts(from)
  return parts ? `${span}, ${parts.year}` : span
}

/** "Sep 19, 08:12 UTC" from an ISO time, for a moment a person reads. */
export function momentWords(iso: unknown): string {
  const at = new Date(typeof iso === 'string' || typeof iso === 'number' ? iso : NaN)
  if (Number.isNaN(at.getTime())) return ''
  const day = `${MONTHS[at.getUTCMonth()]} ${at.getUTCDate()}`
  const hour = String(at.getUTCHours()).padStart(2, '0')
  const minute = String(at.getUTCMinutes()).padStart(2, '0')
  return `${day}, ${hour}:${minute} UTC`
}
