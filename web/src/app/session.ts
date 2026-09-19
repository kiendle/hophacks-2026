import { useCallback, useState } from 'react'

/** home: the query box. setup: the blank graph with its filters. live: generating. */
export type Stage = 'home' | 'setup' | 'live'

export interface Session {
  id: string
  query: string
  /** Search terms a post must match; editable before and during a run. */
  terms: string[]
  /** Subtopics each post is sorted into. */
  subtopics: string[]
  startedAt: number
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

  return { recents, remember }
}
