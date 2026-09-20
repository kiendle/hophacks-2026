/**
 * The live stream: one row per event, bucketed on this side. A post event is
 * the post being created; a like event is engagement landing later, which
 * counts toward the bucket it arrives in, not the one the post was created in.
 */
export interface StreamEvent {
  kind: 'post' | 'like'
  /** Event time, epoch ms. This decides the bucket. */
  t: number
  postId: string
  /** Subtopic id. A post matching several subtopics arrives once per subtopic. */
  subtopic: string
  /** 0 to 10. Jev returns -1 to 1, so the backend sends (score + 1) * 5. */
  sentiment: number
  /** Weight of the event: one post, or the number of likes registered. */
  significance: number
  /** Carried on every event so a bucket can name its top post. */
  text: string
  handle: string
  /** When the post itself was written, which can be well before `t`. */
  postTime: number
  likes?: number
  replies?: number
  reposts?: number
  /**
   * Posts this row stands for. The backend sends one row per post and leaves
   * this unset; the dev fixture uses it to replay a month without emitting
   * millions of objects.
   */
  count?: number
}

/** One tick of the stream. */
export interface EventBatch {
  events: StreamEvent[]
  /** The moment the stream has reached. */
  now: number
  /** Events the backend scanned to produce this batch, before filtering. */
  read?: number
}
