/** The Morning Brief service's JSON, as morning-brief/server.py sends it. */

export interface Interest {
  id: string
  name: string
  terms: string[]
  posts: number
  covered_from: number | null
}

export interface Status {
  paused: boolean
  /** Posts kept by the collector. */
  posts: number
  queued: number
  backfill: { fraction: number } | null
  live: { state: string; event_ms: number | null }
  source?: { network: string; stream: string; host: string }
  interests: Interest[]
  default_hours: number
  default_seconds: number
  min_seconds: number
  max_seconds: number
  elevenlabs: boolean
  engagement_host: string
  /** Id of a brief being made right now, or null. */
  working: string | null
}

export interface Story {
  title: string
  summary: string
  why_it_matters: string
  mood: string
}

export interface Segment {
  topic: string
  headline: string
  script: string
  stories: Story[]
}

export interface Brief {
  id: string
  created: number
  hours: number
  status: 'working' | 'failed' | 'ready'
  /** While it is being made, what is happening. When it failed, why. */
  step: string
  title?: string
  notes: string[]
  coverage_note?: string
  audio: { full: string; voice: string | null } | null
  segments?: Segment[]
}

export interface BriefSummary {
  id: string
  created: number
  hours: number
  status: string
  title: string | null
  audio: boolean
}
