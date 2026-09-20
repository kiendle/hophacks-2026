export interface Post {
  id: string
  handle: string
  text: string
  /** Epoch ms. */
  time: number
  likes: number
  replies: number
  retweets: number
  quotes: number
  /** 0 to 10. */
  sentiment: number
  likesKnown?: boolean
  otherMetricsKnown?: boolean
}

export interface Bucket {
  /** Epoch ms, aligned to BUCKET_MS. */
  start: number
  /** Traction-weighted mean sentiment, 0 to 10. */
  sentiment: number
  /** Sum of post weights behind `sentiment`, so buckets can be combined. */
  weight: number
  /** Sum of weight * sentiment^2, for the weighted spread across a window. */
  sqSum: number
  /** Posts in the bucket. */
  volume: number
  /** Latest known traction of the bucket's posts. */
  traction: number
  /**
   * Cumulative traction of the bucket's posts at each engagement snapshot,
   * sorted by time. Replays read these, never `traction`, so no future leaks in.
   */
  snapshots: Snapshot[]
  topPost: Post
  /** Exact cumulative statistics on the event clock. */
  history?: Moment[]
  likeHistory?: Snapshot[]
  /** Streamed opinions retain both publication time and engagement observation time. */
  opinions?: readonly Opinion[]
  /** Latest timestamp this snapshot may use, including when inspected later. */
  asOf?: number
  /** Number of scored posts, independent of engagement weight. */
  scored?: number
}

export interface LikeBalance {
  t: number
  value: number
  known: boolean
}

export interface Opinion {
  t: number
  score: number | null
  balances: readonly LikeBalance[]
}

export interface Moment {
  t: number
  volume: number
  weight: number
  sum: number
  sqSum: number
}

export interface Snapshot {
  /** Epoch ms the engagement was observed. */
  t: number
  traction: number
}

export interface Series {
  id: string
  name: string
  color: string
  /** Sorted by start, contiguous. */
  buckets: Bucket[]
}

export interface TimeRange {
  start: number
  end: number
}

/** What the Ask panel is about: chosen subtopics within a time range. Either part can be empty. */
export interface Selection {
  range: TimeRange | null
  subtopics: string[]
}
