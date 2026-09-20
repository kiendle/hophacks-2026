/**
 * Model written objects turned into labelled plain lines.
 *
 * Both functions here read something the model wrote (a tool call's input, a saved draft), so every
 * key may be spelled either way and every value is treated as text. Neither ever throws, and neither
 * ever puts JSON in front of a reader: the exact request is shown separately, inside an open details
 * panel. Same wording as harness/web/chat.js, which does this job on the other page.
 */
import { dayWords, oneLine, plural, durationWords, windowWords } from './plain'
import type { DraftLine, StepFact } from './protocol'

const asObject = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : [])

const LANGUAGE_NAMES: Record<string, string> = {
  en: 'English', ja: 'Japanese', es: 'Spanish', pt: 'Portuguese', ko: 'Korean', fr: 'French',
  de: 'German', tr: 'Turkish', ar: 'Arabic', it: 'Italian', zh: 'Chinese', ru: 'Russian', nl: 'Dutch', hi: 'Hindi',
}

const SOURCE_NAMES: Record<string, string> = {
  bluesky_live: 'live Bluesky',
  twitter_firehose: 'the X/Twitter archive',
  congress: 'US Congress posts',
}

export function languageWords(value: unknown): string {
  const codes = (Array.isArray(value) ? value : [value]).map((code) => oneLine(code, 16)).filter(Boolean)
  const names = codes.slice(0, 6).map((code) => LANGUAGE_NAMES[code.toLowerCase()] || code)
  return names.length ? names.join(', ') : 'Any language'
}

const words = (value: unknown, limit = 20) =>
  asArray(value)
    .map((word) => oneLine(word, 40))
    .filter(Boolean)
    .slice(0, limit)

const LIVE_DEFAULTS = { minutes: 15, seconds: 20 }
const span = (value: unknown, fallback: number) => (Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : fallback)

/**
 * The plain words part of what "details" opens: the same facts the step was built from. How long it
 * took is the last of them, once the step has ended. The row itself never shows a duration.
 */
export function stepFacts(request: { tool?: unknown; input?: unknown } | null | undefined, ms?: unknown): StepFact[] {
  const info = asObject(request)
  const input = asObject(info.input)
  const tool = oneLine(info.tool, 60).replace('mcp__harness__', '')
  const searching = tool === 'preview_keywords' || tool === 'bluesky_recent' || tool === 'bluesky_listen'
  const searched = words(input.keywords)
  const lines: StepFact[] = []
  if (searched.length) lines.push({ label: 'Words searched', value: searched.map((word) => `"${word}"`).join(', ') })
  if (tool === 'preview_keywords') {
    const low = dayWords(input.date_from)
    const high = dayWords(input.date_to, -1)
    const dates = low && high && low !== high ? `${low} to ${high}` : low || high
    if (dates) lines.push({ label: 'Dates', value: dates })
  }
  if (tool === 'bluesky_recent') lines.push({ label: 'Time covered', value: `the last ${plural(span(input.minutes, LIVE_DEFAULTS.minutes), 'minute')}` })
  if (tool === 'bluesky_listen') lines.push({ label: 'Time covered', value: `${plural(span(input.seconds, LIVE_DEFAULTS.seconds), 'second')} of live posts` })
  if (searching) lines.push({ label: 'Language', value: languageWords(input.language) })
  if (tool === 'save_draft') {
    let name = ''
    try {
      name = oneLine(asObject(JSON.parse(String(input.spec_json ?? ''))).name, 120)
    } catch {
      name = ''
    }
    if (name) lines.push({ label: 'Project name', value: name })
  }
  const took = durationWords(ms)
  if (took) lines.push({ label: '', value: `Took ${took}` })
  return lines.filter((line) => line.value)
}

function groupsOf(spec: Record<string, unknown>) {
  const raw = asArray(spec.categories).length ? asArray(spec.categories) : asArray(spec.classification)
  return raw
    .slice(0, 12)
    .map((group) => {
      const item = asObject(group)
      return {
        name: oneLine(item.name ?? (typeof group === 'string' ? group : ''), 60),
        description: oneLine(item.description ?? item.about ?? '', 160),
      }
    })
    .filter((group) => group.name)
}

export function liveWords(lookbackHours: unknown, runHours: unknown): string {
  const back = Number(lookbackHours)
  const run = Number(runHours)
  const said = ['From now']
  if (Number.isFinite(back) && back > 0) said.push(`also looking back ${plural(back, 'hour')}`)
  if (Number.isFinite(run) && run > 0) said.push(`and it keeps running for ${plural(run, 'hour')}`)
  return said.join(', ')
}

/** The draft card's own content: labelled plain lines, built from the saved draft and nothing else. */
export function projectLines(spec: unknown): DraftLine[] {
  const root = asObject(spec)
  const observation = asObject(root.observation)
  const window = asObject(observation.window)
  const filter = asObject(root.filter)
  const source = oneLine(observation.source ?? root.source, 40)
  const keywords = words(asArray(root.keywords).length ? root.keywords : filter.any_terms)
  const live = source === 'bluesky_live' || window.mode === 'live'
  const groups = groupsOf(root)
  const lines: DraftLine[] = [
    { label: 'Name', value: oneLine(root.name, 120) },
    { label: 'What we are watching', value: oneLine(observation.intent ?? root.intent, 240) },
    { label: 'Where', value: SOURCE_NAMES[source] || oneLine(source.replace(/_/g, ' '), 40) },
    {
      label: 'When',
      value: live ? liveWords(window.lookback_hours, window.run_hours) : windowWords(window.from ?? root.date_from, window.to ?? root.date_to),
    },
    { label: 'Language', value: languageWords(root.language ?? observation.language ?? filter.languages) },
    { label: 'Words we search for', value: keywords.map((word) => `"${word}"`).join(', ') },
  ]
  if (groups.length) {
    lines.push({
      label: 'Groups we sort posts into',
      groups,
      value: groups.map((group) => (group.description ? `${group.name}: ${group.description}` : group.name)).join('. '),
    })
  }
  lines.push({ label: 'Feeling question', value: oneLine(root.sentiment_question ?? asObject(root.sentiment).instructions, 240) })
  const said = lines.filter((line) => line.value)
  // "Language: Any language" on its own says nothing, so an empty draft returns no lines at all and
  // the card shows its own sentence instead of one meaningless row.
  return said.some((line) => line.label !== 'Language') ? said : []
}
