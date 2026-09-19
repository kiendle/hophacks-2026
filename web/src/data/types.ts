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
}

export interface Bucket {
  /** Epoch ms, aligned to BUCKET_MS. */
  start: number
  /** Traction-weighted mean sentiment, 0 to 10. */
  sentiment: number
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
