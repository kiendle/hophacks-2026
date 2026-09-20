import type { Activity } from './activity'

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
  /** Likes attributed to this period, including initial popularity received there. */
  periodLikes?: number
  timeKnown?: boolean
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
  /** Signed non-opening like changes received in this interval. */
  traction: number
  /**
   * Cumulative traction of the bucket's posts at each engagement snapshot,
   * sorted by time. Replays read these, never `traction`, so no future leaks in.
   */
  snapshots: Snapshot[]
  topPost: Post
  /** Timestamped arrivals, including activity on posts published in older periods. */
  activity?: readonly Activity[]
  /** Latest timestamp this snapshot may use, including when inspected later. */
  asOf?: number
  /** Number of scored posts, independent of engagement weight. */
  scored?: number
  /** Distinct posts with positive influence, including resurfacing older posts. */
  activePosts?: number
}

export interface LikeBalance {
  t: number
  value: number
  known: boolean
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
