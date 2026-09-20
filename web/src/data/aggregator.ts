import { BUCKET_MS, SERIES_COLORS } from './config'
import type { Grade, ReplayDataset, ReplayEvent } from './replayTypes'
import { engagementWeight } from './sentiment'
import type { Bucket, LikeBalance, Moment, Opinion, Post, Series, Snapshot } from './types'

export function atOrBefore<T extends { t: number }>(rows: readonly T[], t: number): T | undefined {
  let lo = 0
  let hi = rows.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (rows[mid].t <= t) lo = mid + 1
    else hi = mid
  }
  return rows[lo - 1]
}

interface Acc {
  history: Moment[]
  likes: Snapshot[]
  posts: { event: ReplayEvent; grade: Grade }[]
  opinions: Opinion[]
}

/** Stores delivered opinions and like balances on their separate event clocks. */
export class Aggregator {
  private companies: ReplayDataset['companies']
  private buckets = new Map<string, Map<number, Acc>>()
  private seen = new Set<string>()
  private balances = new Map<string, LikeBalance[]>()
  posts = 0
  likes = 0

  constructor(companies: ReplayDataset['companies']) {
    this.companies = companies
    for (const company of companies) this.buckets.set(company.id, new Map())
  }

  addBatch(events: ReplayEvent[]) { for (const event of events) this.add(event) }

  add(event: ReplayEvent) {
    if (this.seen.has(event.id)) return
    this.seen.add(event.id)
    const balances = this.balances.get(event.postId) ?? []
    this.balances.set(event.postId, balances)
    if (event.kind === 'post') this.posts++
    else {
      this.likes++
      const prev = balances.at(-1)
      balances.push({ t: event.t, value: event.opening ? event.delta! : (prev?.value ?? 0) + event.delta!, known: event.opening || prev?.known || false })
    }
    const start = Math.floor(event.t / BUCKET_MS) * BUCKET_MS
    for (const grade of event.grades) {
      const series = this.buckets.get(grade.company)
      if (!series) continue
      let bucket = series.get(start)
      if (!bucket) { bucket = { history: [], likes: [], posts: [], opinions: [] }; series.set(start, bucket) }
      if (event.kind === 'post') {
        const prev = bucket.history.at(-1)
        const valid = grade.score !== null && Number.isFinite(grade.score)
        bucket.history.push({ t: event.t, volume: (prev?.volume ?? 0) + 1,
          weight: (prev?.weight ?? 0) + Number(valid), sum: (prev?.sum ?? 0) + (valid ? grade.score! : 0),
          sqSum: (prev?.sqSum ?? 0) + (valid ? grade.score! ** 2 : 0) })
        bucket.posts.push({ event, grade })
        bucket.opinions.push({ t: event.t, score: valid ? grade.score : null, balances })
      } else if (!event.opening) {
        bucket.likes.push({ t: event.t, traction: (bucket.likes.at(-1)?.traction ?? 0) + event.delta! })
      }
    }
  }

  snapshot(now: number): Series[] {
    return this.companies.map((company, index) => {
      const buckets: Bucket[] = []
      for (const [start, acc] of this.buckets.get(company.id)!) {
        if (start > now) continue
        const m = atOrBefore(acc.history, now)
        const engagement = atOrBefore(acc.likes, now)?.traction ?? 0
        let top: Post = { id: '', handle: '', text: '', time: start, sentiment: NaN,
          likes: 0, replies: 0, retweets: 0, quotes: 0, likesKnown: false, otherMetricsKnown: false }
        let best = -Infinity
        let weight = 0
        let sum = 0
        let sqSum = 0
        for (const { event, grade } of acc.posts) {
          if (event.t > now) break
          const balance = atOrBefore(this.balances.get(event.postId) ?? [], now)
          if (grade.score !== null && Number.isFinite(grade.score)) {
            const w = engagementWeight(balance?.known ? balance.value : undefined)
            weight += w
            sum += w * grade.score
            sqSum += w * grade.score ** 2
          }
          const rank = (balance?.known ? balance.value : 0) + (grade.score !== null ? 0.1 : 0)
          if (rank <= best) continue
          best = rank
          top = { id: event.postId, handle: event.authorId ? `ID ${event.authorId}` : '', text: event.text,
            time: event.t, sentiment: grade.score ?? NaN, likes: balance?.value ?? 0,
            likesKnown: balance?.known ?? false, otherMetricsKnown: false, replies: 0, retweets: 0, quotes: 0 }
        }
        buckets.push({ start, sentiment: weight ? sum / weight : NaN,
          weight, sqSum, volume: m?.volume ?? 0, scored: m?.weight ?? 0,
          opinions: acc.opinions, asOf: now,
          traction: engagement, snapshots: acc.likes, likeHistory: acc.likes, history: acc.history, topPost: top })
      }
      buckets.sort((a, b) => a.start - b.start)
      // Explicit empty buckets break lines instead of implying continuous observations.
      const filled: Bucket[] = []
      for (const bucket of buckets) {
        const previous = filled.at(-1)
        if (previous) for (let start = previous.start + BUCKET_MS; start < bucket.start; start += BUCKET_MS) {
          filled.push({ start, sentiment: NaN, weight: 0, sqSum: 0, volume: 0, traction: 0,
            snapshots: [], history: [], likeHistory: [], topPost: { ...previous.topPost, id: '', text: '', time: start, sentiment: NaN } })
        }
        filled.push(bucket)
      }
      return { ...company, color: SERIES_COLORS[index % SERIES_COLORS.length], buckets: filled }
    })
  }
}
