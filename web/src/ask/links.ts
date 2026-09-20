/**
 * Addresses inside someone else's words.
 *
 * A post on a card is never reworded and never becomes markup. The only things in it that turn into
 * links are addresses that pass the checks here, and the label shown is built from the PARSED
 * address, so a look alike host reads as what it really is.
 */

/** A web address inside a post. It stops at whitespace and at characters that never belong to one. */
const URL_IN_TEXT = /https?:\/\/[^\s<>"'`]+/gi

/** "See https://t.co/abc." ends a sentence, not an address: closing punctuation goes back to the text. */
function trimAddress(raw: string): string {
  let address = raw
  for (;;) {
    const last = address.at(-1)
    if (!last) break
    const opens = ({ ')': '(', ']': '[', '}': '{' } as Record<string, string>)[last]
    if (opens ? address.split(opens).length >= address.split(last).length : !/[.,!?:;'"…»”’]/.test(last)) break
    address = address.slice(0, -1)
  }
  return address
}

export interface WebLink {
  href: string
  label: string
}

/** An address a person may click inside a post: http or https, a real host, nothing hidden before an "@". */
export function webLink(raw: unknown): WebLink | null {
  if (typeof raw !== 'string' || raw.length > 2000) return null
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null
  if (url.username || url.password || !url.hostname.includes('.')) return null
  const host = url.hostname.replace(/^www\./, '')
  const path = url.pathname.split('/').filter(Boolean)
  const first = (path[0] || '').slice(0, 24)
  const more = path.length > 1 || (path[0] || '').length > 24 || url.search !== '' || url.hash !== ''
  return { href: url.href, label: `${host}${first ? `/${first}` : ''}${more ? '...' : ''}` }
}

// The link to the post itself. Only https to the three hosts a post can live on is let through: a
// look alike host (x.com.evil.example), a port, a name before an "@", plain http, or any other
// scheme (javascript:, data:) gives no link at all.
const POST_HOSTS: Record<string, string> = { 'x.com': 'Open on X', 'twitter.com': 'Open on X', 'bsky.app': 'Open on Bluesky' }

export function postLink(value: unknown): WebLink | null {
  if (typeof value !== 'string' || value.length > 500) return null
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return null
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return null
  return Object.hasOwn(POST_HOSTS, url.hostname) ? { href: url.href, label: POST_HOSTS[url.hostname] } : null
}

export type TextPart = { text: string; href?: undefined; label?: undefined } | WebLink

/**
 * A post split into what the panel draws: plain text, and the addresses inside it. `cutTail` says
 * the text was cut short by someone else, so an address running to its very end may be half an
 * address and is left as text.
 */
export function textParts(text: unknown, cutTail = false): TextPart[] {
  const value = typeof text === 'string' ? text : ''
  const parts: TextPart[] = []
  let at = 0
  for (const match of value.matchAll(URL_IN_TEXT)) {
    const index = match.index ?? 0
    const address = trimAddress(match[0])
    const link = cutTail && index + match[0].length === value.length ? null : webLink(address)
    if (!link) continue
    if (index > at) parts.push({ text: value.slice(at, index) })
    parts.push(link)
    at = index + address.length
  }
  if (at < value.length) parts.push({ text: value.slice(at) })
  return parts
}

/**
 * The short form of a post: never cut inside an address, inside a word when a space is near, or
 * inside one emoji, and it ends in "..." whenever anything was left out. Nothing is ever hidden
 * without saying so.
 */
export const SHORT_POST = 240

export function excerpt(text: unknown, limit = SHORT_POST): string {
  const value = typeof text === 'string' ? text : ''
  if (value.length <= limit) return value
  let cut = limit
  for (const match of value.matchAll(URL_IN_TEXT)) {
    const index = match.index ?? 0
    if (index >= cut) break
    const end = index + match[0].length
    if (end > cut) {
      cut = index > 0 ? index : end
      break
    }
  }
  if (cut === limit && /\S/.test(value[cut] ?? ' ') && /\S/.test(value[cut - 1])) {
    const space = value.slice(0, cut).search(/\s\S*$/)
    if (space >= limit * 0.6) cut = space
  }
  if (/[\uD800-\uDBFF]/.test(value[cut - 1] ?? '')) cut -= 1
  const head = value.slice(0, cut).trimEnd()
  return head.length < value.trimEnd().length ? `${head}...` : value
}
