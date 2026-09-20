import type { EvidencePost } from './ask/protocol'

/** Keep real handles, but never expose internal numeric identifiers as names. */
export function displayHandle(handle: string): string {
  const value = handle.trim()
  return /^(?:ID\s+|(?:tweet|post|author)[\s:_-]*)?\d+$/i.test(value) ? '' : value
}

export function postExcerpt(text: string, limit = 400): string {
  const characters = Array.from(text)
  return characters.length > limit ? characters.slice(0, limit).join('').trimEnd() + '…' : text
}

export function scoredCitations(posts: EvidencePost[]): EvidencePost[] {
  return posts.filter(post => post.scored !== false && Number.isFinite(post.sentiment))
}

export function hoverCardLayout(anchor: { x: number; y: number }, bounds: { width: number; height: number }, measuredHeight: number) {
  const margin = 8
  const width = Math.min(280, Math.max(0, bounds.width - 2 * margin))
  const maxHeight = Math.min(280, Math.max(0, bounds.height - 2 * margin))
  const height = Math.min(measuredHeight || maxHeight, maxHeight)
  const left = anchor.x + 18 + width > bounds.width - margin ? anchor.x - 18 - width : anchor.x + 18
  return { width, maxHeight,
    left: Math.max(margin, Math.min(left, bounds.width - width - margin)),
    top: Math.max(margin, Math.min(anchor.y - height / 2, bounds.height - height - margin)) }
}
