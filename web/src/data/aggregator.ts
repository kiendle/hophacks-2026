import { BUCKET_MS, SERIES_COLORS } from './config'
import { ActivityAccumulator, type Activity } from './activity'
import type { ReplayDataset, ReplayEvent } from './replayTypes'
import type { Bucket, LikeBalance, Post, Series, Snapshot } from './types'

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
  activity: Activity[]
  likes: Snapshot[]
  closed?: Bucket
}

interface Partial {
  start: number
  count: number
  through: number
  stats: ActivityAccumulator
  result?: ReturnType<ActivityAccumulator['result']>
  bucket?: Bucket
}

const emptyPost = (time: number): Post => ({ id: '', handle: '', text: '', time, sentiment: NaN,
  likes: 0, replies: 0, retweets: 0, quotes: 0, likesKnown: false, otherMetricsKnown: false })

/** Attribute influence to receipt time, never back to an older publication bucket. */
export class Aggregator {
  private companies: ReplayDataset['companies']
  private buckets = new Map<string, Map<number, Acc>>()
  private seen = new Set<string>()
  private balances = new Map<string, LikeBalance[]>()
  // Only the inspected partial bucket retains its per-post map. Completed
  // buckets keep their compact result and original activity for exact windows.
  private partial = new Map<string, Partial>()
  private gaps = new Map<string, Map<number, Bucket>>()
  posts = 0
  likes = 0

  constructor(companies: ReplayDataset['companies']) {
    this.companies = companies
    for (const company of companies) {
      this.buckets.set(company.id, new Map())
      this.gaps.set(company.id, new Map())
    }
  }

  addBatch(events: ReplayEvent[]) { for (const event of events) this.add(event) }

  add(event: ReplayEvent) {
    if (this.seen.has(event.id)) return
    this.seen.add(event.id)
    let likes = 0
    if (event.kind === 'post') this.posts++
    else {
      this.likes++
      const balances = this.balances.get(event.postId) ?? []
      const prev = balances.at(-1)
      // An opening initializes popularity once; a repeated opening isn't new activity.
      likes = event.opening && prev?.known ? 0 : event.delta!
      balances.push({ t: event.t, value: event.opening ? event.delta! : (prev?.value ?? 0) + event.delta!,
        known: event.opening || prev?.known || false })
      this.balances.set(event.postId, balances)
    }
    const start = Math.floor(event.t / BUCKET_MS) * BUCKET_MS
    for (const grade of event.grades) {
      const series = this.buckets.get(grade.company)
      if (!series) continue
      let bucket = series.get(start)
      if (!bucket) {
        bucket = { activity: [], likes: [] }
        series.set(start, bucket)
        this.gaps.get(grade.company)!.delete(start)
      }
      bucket.activity.push({ event, grade, likes })
      bucket.closed = undefined
      if (event.kind === 'like' && !event.opening) {
        bucket.likes.push({ t: event.t, traction: (bucket.likes.at(-1)?.traction ?? 0) + likes })
      }
    }
  }

  snapshot(now: number): Series[] {
    return this.companies.map((company, index) => {
      const buckets: Bucket[] = []
      let retained = this.partial.get(company.id)
      for (const [start, acc] of this.buckets.get(company.id)!) {
        if (start > now || acc.activity[0]?.event.t > now) continue
        const end = start + BUCKET_MS - 1
        if (acc.closed && now >= end) {
          if (retained?.start === start) retained = undefined
          buckets.push(acc.closed)
          continue
        }
        const cutoff = Math.min(now, end)
        let partial = retained
        // Rewinding within a bucket must start over if already-applied events
        // fall beyond the cutoff. Forward ticks only group newly visible rows.
        if (!partial || partial.start !== start || partial.through > cutoff) {
          partial = { start, count: 0, through: -Infinity, stats: new ActivityAccumulator() }
        }
        while (partial.count < acc.activity.length && acc.activity[partial.count].event.t <= cutoff) {
          const activity = acc.activity[partial.count++]
          partial.stats.add(activity)
          partial.through = Math.max(partial.through, activity.event.t)
          partial.result = undefined
          partial.bucket = undefined
        }
        // result() still sums in the original post insertion order, preserving
        // exact floating-point calculations and the existing popularity ties.
        const result = partial.result ??= partial.stats.result()
        const balance = atOrBefore(this.balances.get(result.topPost?.id ?? '') ?? [], cutoff)
        let bucket = partial.bucket
        if (!bucket || bucket.asOf !== cutoff || bucket.topPost.likes !== (balance?.value ?? 0) ||
          bucket.topPost.likesKnown !== (balance?.known ?? false)) {
          const topPost = { ...(result.topPost ?? emptyPost(start)),
            likes: balance?.value ?? 0, likesKnown: balance?.known ?? false }
          bucket = { start, sentiment: result.sentiment, weight: result.weight, sqSum: result.sqSum,
            volume: result.volume, activePosts: result.activePosts, scored: result.scored, traction: result.traction,
            activity: acc.activity, asOf: cutoff, snapshots: acc.likes, topPost }
          partial.bucket = bucket
        }
        if (now >= end) {
          acc.closed = bucket
          if (retained?.start === start) retained = undefined
        } else retained = partial
        buckets.push(bucket)
      }
      if (retained) this.partial.set(company.id, retained)
      else this.partial.delete(company.id)
      buckets.sort((a, b) => a.start - b.start)
      const filled: Bucket[] = []
      const gaps = this.gaps.get(company.id)!
      for (const bucket of buckets) {
        const previous = filled.at(-1)
        if (previous) for (let start = previous.start + BUCKET_MS; start < bucket.start; start += BUCKET_MS) {
          let gap = gaps.get(start)
          if (!gap) {
            gap = { start, sentiment: NaN, weight: 0, sqSum: 0, volume: 0, activePosts: 0, scored: 0,
              traction: 0, snapshots: [], activity: [], asOf: start + BUCKET_MS - 1, topPost: emptyPost(start) }
            gaps.set(start, gap)
          }
          filled.push(gap)
        }
        filled.push(bucket)
      }
      return { ...company, color: SERIES_COLORS[index % SERIES_COLORS.length], buckets: filled }
    })
  }
}
