/**
 * Development fixture only: synthetic posts and buckets so the UI runs
 * without a backend. Replace with a real DataSource; see data/source.ts.
 */
import { BUCKET_MS, SERIES_COLORS } from './config'
import { sentimentStats, traction } from './sentiment'
import type { Bucket, Post, Series } from './types'

const DAY = 24 * 60 * 60 * 1000
const START = Date.UTC(2026, 7, 17)
const DAYS = 31
const EVENT = START + 14 * DAY + 16 * 60 * 60 * 1000
const SAMPLE_POSTS = 120
/** Engagement snapshots are taken at every bucket boundary for this long. */
const SNAPSHOTS = 12
/** How fast a post's engagement saturates. */
const ENGAGEMENT_TAU = 10 * 60 * 60 * 1000

/** How the mid-month event hits a subtopic. */
type Role = 'shock' | 'drift' | 'flat'

interface Profile {
  id: string
  name: string
  baseSentiment: number
  baseVolume: number
  role: Role
  /** Signed sentiment change at the event's peak. */
  shift: number
  /** Extra volume multiple at the event's peak. */
  spike: number
  /** Engagement per post relative to the others. */
  engagement: number
}

const PROFILES: Profile[] = [
  { id: 'anthropic', name: 'Anthropic', baseSentiment: 6.4, baseVolume: 800, role: 'drift', shift: 0.9, spike: 1.3, engagement: 1 },
  { id: 'openai', name: 'OpenAI', baseSentiment: 6.1, baseVolume: 1400, role: 'shock', shift: -3.1, spike: 5.5, engagement: 1.3 },
  { id: 'deepmind', name: 'Google DeepMind', baseSentiment: 6.2, baseVolume: 700, role: 'drift', shift: 0.5, spike: 0.5, engagement: 0.8 },
  { id: 'nvidia', name: 'Nvidia', baseSentiment: 5.9, baseVolume: 900, role: 'flat', shift: 0, spike: 0.2, engagement: 1.1 },
  { id: 'meta', name: 'Meta AI', baseSentiment: 5.3, baseVolume: 600, role: 'flat', shift: 0, spike: 0.1, engagement: 0.9 },
  { id: 'microsoft', name: 'Microsoft', baseSentiment: 5.6, baseVolume: 750, role: 'flat', shift: -0.2, spike: 0.4, engagement: 0.7 },
  { id: 'xai', name: 'xAI', baseSentiment: 4.8, baseVolume: 650, role: 'flat', shift: 0, spike: 0.1, engagement: 1.8 },
  { id: 'mistral', name: 'Mistral', baseSentiment: 6.6, baseVolume: 250, role: 'flat', shift: 0, spike: 0.1, engagement: 0.5 },
  { id: 'perplexity', name: 'Perplexity', baseSentiment: 6.0, baseVolume: 350, role: 'flat', shift: 0, spike: 0.1, engagement: 0.7 },
]

type Pool = 'pos' | 'neu' | 'neg' | 'eventNeg' | 'eventPos' | 'recovery' | 'spillover'

const TEXTS: Record<Pool, string[]> = {
  pos: [
    'ok {name} just saved me an entire afternoon of refactoring',
    'genuinely impressed by how {name} handled a 40 file codebase today',
    'the new {name} docs are so much better, whoever rewrote them thank you',
    'used {name} to explain my lab results in plain english. wild times',
    '{name} wrote the migration, tests pass, I am going to lunch',
    'hot take: {name} is the best dev tool I have paid for this year',
    'my mom asked {name} to plan her garden and now she will not stop talking about it',
  ],
  neu: [
    'anyone benchmarked {name} vs the others on long context retrieval?',
    'reading the {name} changelog so you do not have to. mostly small fixes',
    'curious how {name} prices the new tier once the promo ends',
    'thread: 6 prompts I use with {name} every single day',
    '{name} usage at our company doubled this quarter, still figuring out if that is good',
    'is {name} down for anyone else or just my wifi',
  ],
  neg: [
    '{name} rate limits are getting ridiculous for paid users',
    'third hallucinated citation from {name} this week. be careful out there',
    'why does {name} refuse the most harmless requests sometimes',
    '{name} context window feels smaller than advertised',
  ],
  eventNeg: [
    '{name} quietly changed the model and my entire pipeline broke overnight. no notice, no changelog',
    'cancelling my {name} subscription. you do not ship a downgrade and call it an upgrade',
    'the new {name} update is noticeably worse at code. side by side screenshots below',
    'so {name} can just swap the model under us whenever they want? how is anyone supposed to build on this',
    '{name} support told me it is "working as intended". it is not working at all',
    'every single dev in my timeline is complaining about {name} today. that is not a coincidence',
    'we moved our whole product to {name} last month. worst timing of my career',
    'hey {name}, can we get the old model back as an option at least',
  ],
  eventPos: [
    'unpopular opinion but the new {name} model is fine if you adjust your prompts',
    'people dunking on {name} today, meanwhile it just fixed my build',
  ],
  recovery: [
    '{name} is rolling back part of the update. good, but trust takes longer to rebuild',
    'credit where due, {name} posted a real postmortem this time',
    'the pinned model option from {name} is back. crisis mostly averted',
    'ok the patched {name} model is better than the broken one, still not what it was',
    '{name} apologized and extended everyone a free month. we will see',
  ],
  spillover: [
    'half my timeline is migrating to {name} today. hope their servers are ready',
    'tried the same prompts on {name} after the other update broke. honestly fine',
    'this week is a good reminder to not lock your product into a single provider like {name}',
    '{name} status page looking a little yellow with everyone switching over',
  ],
}

const HANDLE_A = ['kira', 'mo', 'jules', 'ravi', 'tess', 'omar', 'lena', 'nico', 'priya', 'sam', 'ada', 'felix', 'june', 'theo', 'maya', 'ivan']
const HANDLE_B = ['_builds', 'codes', '.dev', '_ml', 'writes', 'ships', '_data', 'hq', 'labs', '_eng']

function mulberry32(seed: number) {
  return () => {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function normal(rand: () => number): number {
  return Math.sqrt(-2 * Math.log(1 - rand())) * Math.cos(2 * Math.PI * rand())
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** 0 to 1 strength of the event's sentiment shift and volume spike, 0 before it. */
function eventShape(role: Role, t: number) {
  const d = (t - EVENT) / DAY
  if (d < 0) return { shift: 0, spike: 0 }
  const ramp = 1 - Math.exp(-d * 10)
  if (role === 'shock')
    // Partial recovery: settles at 35% of the peak drop.
    return { shift: ramp * (0.35 + 0.65 * Math.exp(-d / 1.8)), spike: ramp * Math.exp(-d / 0.9) }
  if (role === 'drift') return { shift: 1 - Math.exp(-d / 2), spike: ramp * Math.exp(-d / 1.5) }
  return { shift: ramp * Math.exp(-d / 1.5), spike: ramp * Math.exp(-d / 0.9) }
}

function pickPool(profile: Profile, sentiment: number, t: number): Pool {
  const days = (t - EVENT) / DAY
  if (profile.role === 'shock') {
    if (days >= 0 && days < 1.5) return sentiment < 5 ? 'eventNeg' : 'eventPos'
    if (days >= 1.5 && days < 5) return 'recovery'
  }
  if (profile.role === 'drift' && days >= 0 && days < 1.5) return 'spillover'
  if (sentiment >= 6.3) return 'pos'
  if (sentiment < 4.5) return 'neg'
  return 'neu'
}

function makeSeries(profile: Profile, index: number): Series {
  const rand = mulberry32(index * 7919 + 17)
  const pick = <T,>(xs: T[]) => xs[Math.floor(rand() * xs.length)]
  const buckets: Bucket[] = []

  for (let start = START; start < START + DAYS * DAY; start += BUCKET_MS) {
    const mid = start + BUCKET_MS / 2
    const hour = new Date(mid).getUTCHours()
    const ev = eventShape(profile.role, mid)

    const target =
      profile.baseSentiment + 0.35 * Math.sin((2 * Math.PI * (hour - 9)) / 24) + profile.shift * ev.shift
    const volume = Math.round(
      profile.baseVolume *
        (1 + 0.5 * Math.sin((2 * Math.PI * (hour - 10)) / 24)) *
        (1 + profile.spike * ev.spike) *
        (0.9 + 0.2 * rand()),
    )

    // A sample of posts stands in for the bucket; the aggregate uses the real formula.
    const posts: Post[] = []
    for (let j = 0; j < SAMPLE_POSTS; j++) {
      const sentiment = clamp(target + 1.4 * normal(rand), 0, 10)
      // Outrage travels further while the event is hot, for the subtopic it hits.
      const boost = profile.role === 'shock' && sentiment < target ? 1 + 4 * ev.spike : 1
      const likes = Math.round(Math.exp(Math.log((volume / 30) * profile.engagement) + 1.3 * normal(rand)) * boost)
      posts.push({
        id: `${profile.id}-${start}-${j}`,
        handle: '',
        text: '',
        time: start + Math.floor(rand() * BUCKET_MS),
        likes,
        replies: Math.round(likes * (0.06 + 0.12 * rand()) * (1 + 2 * ev.spike)),
        retweets: Math.round(likes * (0.08 + 0.15 * rand())),
        quotes: Math.round(likes * (0.01 + 0.04 * rand()) * (1 + 3 * ev.spike)),
        sentiment,
      })
    }

    const stats = sentimentStats(posts)
    let top = posts[0]
    for (const p of posts) if (traction(p) > traction(top)) top = p
    const pool = pickPool(profile, top.sentiment, mid)
    top.handle = `@${pick(HANDLE_A)}${pick(HANDLE_B)}`
    top.text = pick(TEXTS[pool]).replaceAll('{name}', profile.name)

    // Engagement accrues after posting, so each snapshot sees more of it.
    const final = (posts.reduce((s, p) => s + traction(p), 0) * volume) / SAMPLE_POSTS
    const snapshots = Array.from({ length: SNAPSHOTS }, (_, k) => {
      const age = (k + 1) * BUCKET_MS - BUCKET_MS / 2
      return { t: start + (k + 1) * BUCKET_MS, traction: Math.round(final * (1 - Math.exp(-age / ENGAGEMENT_TAU))) }
    })

    buckets.push({
      start,
      sentiment: stats.mean,
      weight: stats.weight,
      sqSum: stats.sqSum,
      volume,
      traction: snapshots[snapshots.length - 1].traction,
      snapshots,
      topPost: top,
    })
  }

  return { id: profile.id, name: profile.name, color: SERIES_COLORS[index % SERIES_COLORS.length], buckets }
}

/** Deterministic profile for a subtopic the fixture does not know. */
function inventProfile(name: string, index: number): Profile {
  let h = 0
  for (const ch of name) h = (Math.imul(h, 31) + ch.charCodeAt(0)) | 0
  const rand = mulberry32(Math.abs(h) + 1)
  const role: Role = index === 0 ? 'shock' : index === 1 ? 'drift' : 'flat'
  return {
    id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
    name,
    baseSentiment: 4.8 + rand() * 1.9,
    baseVolume: Math.round(250 + rand() * 1200),
    role,
    shift: role === 'shock' ? -(2.4 + rand()) : role === 'drift' ? 0.4 + rand() * 0.7 : 0,
    spike: role === 'shock' ? 4 + rand() * 2 : role === 'drift' ? 0.5 + rand() : 0.1 + rand() * 0.3,
    engagement: 0.6 + rand(),
  }
}

/**
 * Series for the given subtopics, in order. Known names keep their scripted
 * behaviour around the mid-month event; the rest are invented deterministically.
 */
export function generateDevSeries(subtopics?: string[]): Series[] {
  if (!subtopics) return PROFILES.map(makeSeries)
  return subtopics.map((name, i) => {
    const known = PROFILES.find((p) => p.name.toLowerCase() === name.toLowerCase())
    return makeSeries(known ? { ...known, role: i === 0 ? 'shock' : known.role } : inventProfile(name, i), i)
  })
}
