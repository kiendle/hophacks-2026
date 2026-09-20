import { useCallback, useState } from 'react'

/** home: the query box. setup: the blank graph with its filters. live: generating. */
export type Stage = 'home' | 'setup' | 'live' | 'automation'

export interface Session {
  id: string
  query: string
  /** Search terms a post must match; editable before and during a run. */
  terms: string[]
  /** Subtopics each post is sorted into. */
  subtopics: string[]
  startedAt: number
  title?: string
  pinned?: boolean
  archived?: boolean
}

const RECENTS_KEY = 'sentimeter.recents'
const RECENTS_MAX = 20

/** Past queries, newest first. Browser storage can be unavailable, so never throw. */
export function loadRecents(): Session[] {
  try {
    const raw = localStorage.getItem(RECENTS_KEY)
    return raw ? (JSON.parse(raw) as Session[]) : []
  } catch {
    return []
  }
}

function saveRecents(list: Session[]) {
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(list.slice(0, RECENTS_MAX)))
  } catch {
    // Nothing to do; recents are a convenience.
  }
}

export function useRecents() {
  const [recents, setRecents] = useState<Session[]>(loadRecents)

  const remember = useCallback((session: Session) => {
    setRecents((prev) => {
      const next = [session, ...prev.filter((s) => s.id !== session.id)]
      saveRecents(next)
      return next
    })
  }, [])

  const forget = useCallback((id: string) => {
    setRecents((prev) => {
      const next = prev.filter((s) => s.id !== id)
      saveRecents(next)
      return next
    })
  }, [])

  const edit = useCallback((id: string, changes: Partial<Pick<Session, 'title' | 'pinned' | 'archived'>>) => {
    setRecents(previous => {
      const next = previous.map(session => session.id === id ? { ...session, ...changes } : session)
      saveRecents(next)
      return next
    })
  }, [])

  return { recents, remember, forget, edit }
}

/** Share search settings only; conversations stay in this browser. */
export function shareSessionUrl(session: Session): string {
  const url = new URL(location.href)
  url.search = ''
  url.hash = `workspace=${encodeURIComponent(JSON.stringify({ query: session.query, title: session.title, terms: session.terms, subtopics: session.subtopics }))}`
  return url.href
}

export function readSharedSession(hash: string): Session | null {
  if (!hash.startsWith('#workspace=') || hash.length > 20000) return null
  try {
    const value = JSON.parse(decodeURIComponent(hash.slice(11)))
    if (!value || typeof value.query !== 'string' || !value.query.trim() || value.query.length > 500) return null
    const strings = (list: unknown): list is string[] => Array.isArray(list) && list.length <= 100 && list.every(item => typeof item === 'string' && item.length <= 500)
    if (!strings(value.terms) || !strings(value.subtopics)) return null
    return { id: crypto.randomUUID(), query: value.query, terms: value.terms, subtopics: value.subtopics,
      startedAt: Date.now(), title: typeof value.title === 'string' ? value.title.slice(0, 120) : undefined }
  } catch { return null }
}
