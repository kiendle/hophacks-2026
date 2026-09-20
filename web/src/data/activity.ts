import type { Grade, ReplayEvent } from './replayTypes'
import type { Post } from './types'
import { engagementWeight, spread } from './sentiment'

export interface Activity {
  event: ReplayEvent
  grade: Grade
  /** Initial balance once, or a subsequent signed change; never a repeated snapshot. */
  likes: number
}

interface Contribution {
  published: boolean
  likes: number
  latest: Activity
}

/** Group by post before damping, so delivery batch sizes cannot change influence. */
export class ActivityAccumulator {
  private posts = new Map<string, Contribution>()
  private traction = 0

  add(activity: Activity) {
    const { event } = activity
    const previous = this.posts.get(event.postId)
    this.posts.set(event.postId, {
      published: previous?.published || (event.kind === 'post' && event.publication !== false),
      likes: (previous?.likes ?? 0) + activity.likes,
      latest: activity,
    })
    if (event.kind === 'like' && !event.opening) this.traction += activity.likes
  }

  result() {
    let volume = 0, activePosts = 0, scored = 0, weight = 0, sum = 0, sqSum = 0
    let top: Contribution | undefined
    for (const post of this.posts.values()) {
      if (post.published) volume++
      const w = Number(post.published) + engagementWeight(post.likes) - 1
      if (w <= 0) continue
      activePosts++
      const score = post.latest.grade.score
      const valid = score !== null && Number.isFinite(score)
      if (valid) {
        scored++
        weight += w
        sum += w * score
        sqSum += w * score ** 2
      }
      if (!top || post.likes > top.likes || (post.likes === top.likes && valid && top.latest.grade.score === null)) top = post
    }
    const sentiment = weight > 0 ? sum / weight : NaN
    let topPost: Post | undefined
    if (top) {
      const { event, grade } = top.latest
      topPost = { id: event.postId, handle: event.authorId ? `ID ${event.authorId}` : '', text: event.text,
        time: event.postTime ?? event.t, timeKnown: event.postTime !== null, sentiment: grade.score ?? NaN,
        periodLikes: top.likes, likes: 0, likesKnown: false, otherMetricsKnown: false,
        replies: 0, retweets: 0, quotes: 0 }
    }
    return { volume, activePosts, scored, weight, sum, sqSum, sentiment,
      spread: spread(weight, sentiment, sqSum), traction: this.traction, topPost }
  }
}
