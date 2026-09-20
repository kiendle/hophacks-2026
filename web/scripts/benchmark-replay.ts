/**
 * Reproducible replay CPU benchmark. Run variants sequentially, never in parallel:
 * node --expose-gc --import tsx scripts/benchmark-replay.ts --variant=before --mode=engine
 * node --expose-gc --import tsx scripts/benchmark-replay.ts --variant=after --mode=engine --compare=../harness/data/replay-performance/before-engine.json
 * Repeat with --mode=source to measure publication coalescing. --max-events=20000
 * is an explicitly labelled smoke test; omit it to load the complete real fixture.
 * The baseline directory is a copy of src/data made before implementation changes.
 */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createReadStream, readFileSync, statSync, writeFileSync, mkdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { performance } from 'node:perf_hooks'
import { createInterface } from 'node:readline'
import { Transform } from 'node:stream'
import { StringDecoder } from 'node:string_decoder'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createGunzip } from 'node:zlib'
import { area, line, scaleLinear, scaleUtc, stack } from 'd3'
import { loadReplayDataset } from '../server/loadReplayDataset.ts'
import { sentimentDomain } from '../src/sentimentDomain.ts'
import type { ReplayDataset, ReplayMessage } from '../src/data/replayTypes.ts'
import type { Bucket, Series } from '../src/data/types.ts'
import type { StreamSnapshot } from '../src/data/source.ts'

const args = new Map(process.argv.slice(2).map(value => {
  const [key, ...rest] = value.replace(/^--/, '').split('=')
  return [key, rest.join('=')]
}))
const web = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const resultsDir = resolve(web, '../harness/data/replay-performance')
const variant = args.get('variant') ?? 'after'
const mode = args.get('mode') ?? 'engine'
assert.ok(['before', 'after'].includes(variant), '--variant must be before or after')
assert.ok(['engine', 'source'].includes(mode), '--mode must be engine or source')
const maxEvents = Number(args.get('max-events') ?? Infinity)
const batchSize = Number(args.get('batch-size') ?? 1_000)
const samples = Number(args.get('samples') ?? 24)
const repeats = Number(args.get('repeats') ?? 8)
for (const [name, value] of Object.entries({ batchSize, samples, repeats })) {
  assert.ok(Number.isInteger(value) && value > 0, `${name} must be a positive integer`)
}
assert.ok(maxEvents === Infinity || (Number.isInteger(maxEvents) && maxEvents > 0))
const engineDir = variant === 'before' ? resolve(resultsDir, 'before/data') : resolve(web, 'src/data')
const fixture = resolve(web, 'data/demo.json.gz')
const output = resolve(args.get('output') ?? resolve(resultsDir, `${variant}-${mode}${maxEvents === Infinity ? '' : '-smoke'}.json`))
const importEngine = (name: string) => import(pathToFileURL(resolve(engineDir, `${name}.ts`)).href)
const { Aggregator } = await importEngine('aggregator') as typeof import('../src/data/aggregator.ts')
const { chartData } = await importEngine('chartData') as typeof import('../src/data/chartData.ts')
const { windowStat } = await importEngine('window') as typeof import('../src/data/window.ts')
const { BUCKET_MS, DEFAULT_LINE_INTERVAL } = await importEngine('config') as typeof import('../src/data/config.ts')

// Exploration alone may stop the line-framed stream early. Full runs always use
// the production streaming loader, including its metadata and count validation.
async function loadPrefix(limit: number): Promise<ReplayDataset> {
  const input = createReadStream(fixture)
  const gunzip = createGunzip()
  const decoder = new StringDecoder('utf8')
  const protect = (text: string) => text.replace(/[\u0085\u2028\u2029]/g,
    character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'))
  const decoded = new Transform({ transform(chunk: Buffer, _encoding, callback) {
    callback(null, protect(decoder.write(chunk)))
  }, flush(callback) { callback(null, protect(decoder.end())) } })
  input.pipe(gunzip).pipe(decoded)
  const lines = createInterface({ input: decoded, crlfDelay: Infinity })
  let data: ReplayDataset | undefined
  try {
    for await (const raw of lines) {
      const line = raw.trim()
      if (!data) {
        data = JSON.parse(line + ']}') as ReplayDataset
      } else if (line === ']}') break
      else {
        data.events.push(JSON.parse(line.endsWith(',') ? line.slice(0, -1) : line))
        if (data.events.length >= limit) break
      }
    }
  } finally { lines.close(); input.destroy(); gunzip.destroy(); decoded.destroy() }
  assert.ok(data)
  return data
}

interface Sample { elapsedMs: number; cpuMs: number }
const timings: Record<string, Sample[]> = {}
function measure<T>(name: string, action: () => T): T {
  const cpu = process.cpuUsage()
  const started = performance.now()
  const result = action()
  const elapsedMs = performance.now() - started
  const used = process.cpuUsage(cpu)
  ;(timings[name] ??= []).push({ elapsedMs, cpuMs: (used.user + used.system) / 1_000 })
  return result
}
function summarize(values: Sample[]) {
  const sorted = values.map(value => value.elapsedMs).sort((a, b) => a - b)
  const at = (percent: number) => sorted[Math.max(0, Math.ceil(sorted.length * percent) - 1)]
  return { samples: values.length, p50Ms: at(0.5), p95Ms: at(0.95), maxMs: sorted.at(-1),
    totalElapsedMs: sorted.reduce((a, b) => a + b, 0), totalCpuMs: values.reduce((a, b) => a + b.cpuMs, 0) }
}
const fingerprint = (name: string) => createHash('sha256').update(readFileSync(resolve(engineDir, `${name}.ts`))).digest('hex')
const loadStarted = performance.now()
console.log(JSON.stringify({ phase: 'loading', variant, mode, maxEvents: maxEvents === Infinity ? 'full' : maxEvents }))
const dataset = await (maxEvents === Infinity ? loadReplayDataset(fixture) : loadPrefix(maxEvents))
const loadedMs = performance.now() - loadStarted
// The engine benchmark covers the full dataset. The source scheduling benchmark
// uses its real tail, bounded to avoid quadratic baseline publishing during setup.
const events = mode === 'source' ? dataset.events.slice(-(32_000 + samples * batchSize)) : dataset.events
assert.ok(events.length > 0)
const actualCounts = { posts: 0, likes: 0 }
for (const event of events) actualCounts[event.kind === 'post' ? 'posts' : 'likes']++
if (maxEvents === Infinity && mode === 'engine') assert.deepEqual(actualCounts, dataset.counts)
const measuredEvents = Math.min(samples * batchSize,
  mode === 'source' ? events.length : Math.max(batchSize, Math.floor(events.length / 4)))
const prefixLength = Math.max(0, events.length - measuredEvents)
const visibleIds = ['openai', 'anthropic']
const visible = (series: Series[]) => visibleIds.map(id => series.find(row => row.id === id)!).filter(Boolean)
const cutoffFor = (length: number) => events[Math.max(0, length - 1)].t
const now = cutoffFor(events.length)
const view = { start: dataset.start, end: now + 1 }
let drawSink = 0
function project(series: Series[], cutoff: number, range = { start: dataset.start, end: cutoff + 1 }) {
  return visible(series).map(row => ({ id: row.id, ...chartData(row, range, cutoff, DEFAULT_LINE_INTERVAL) }))
}
function drawProjection(projected: ReturnType<typeof project>) {
  // The same D3 line and stacked-area operations as LineChart at a fixed viewport;
  // this excludes React, DOM/layout, CSS animation, and browser painting.
  const volume = new Map<number, Record<string, number>>()
  let lo = Infinity, hi = -Infinity
  for (const row of projected) {
    for (const point of [...row.points, ...row.trend]) if (Number.isFinite(point.s)) {
      lo = Math.min(lo, point.s); hi = Math.max(hi, point.s)
    }
    for (const point of row.points) {
      const entry = volume.get(point.t) ?? { t: point.t, total: 0 }
      entry[row.id] = point.v; entry.total += point.v; volume.set(point.t, entry)
    }
  }
  const volumeRows = [...volume.values()].sort((a, b) => a.t - b.t)
  const x = scaleUtc().domain([view.start, view.end]).range([0, 1_000])
  const y = scaleLinear().domain(sentimentDomain(lo, hi)).range([300, 0])
  const vy = scaleLinear().domain([0, Math.max(1, ...volumeRows.map(row => row.total)) * 1.1]).range([104, 0])
  const path = line<{ t: number; s: number }>().defined(point => Number.isFinite(point.s)).x(point => x(point.t)).y(point => y(point.s))
  for (const row of projected) { drawSink += (path(row.points)?.length ?? 0) + (path(row.trend)?.length ?? 0) }
  const bands = stack<Record<string, number>>().keys(projected.map(row => row.id)).value((row, key) => row[key] ?? 0)(volumeRows)
  const volumeArea = area<{ 0: number; 1: number; data: Record<string, number> }>()
    .x(value => x(value.data.t)).y0(value => vy(value[0])).y1(value => vy(value[1]))
  for (const band of bands) drawSink += volumeArea(band)?.length ?? 0
}
function bucketSummary(bucket: Bucket) {
  const { start, sentiment, weight, sqSum, volume, activePosts, scored, traction, asOf, topPost } = bucket
  return { start, sentiment, weight, sqSum, volume, activePosts, scored, traction, asOf, topPost: { ...topPost } }
}
function seriesSummary(series: Series[]) {
  return series.map(row => ({ id: row.id, buckets: row.buckets.map(bucketSummary) }))
}
function projectionSummary(projected: ReturnType<typeof project>) {
  return projected.map(row => ({ id: row.id, points: row.points.map(point => ({ t: point.t, s: point.s, v: point.v,
    bucket: bucketSummary(point.bucket) })), trend: row.trend }))
}
function activityCoverage(series: Series[]) {
  const expected = new Map(dataset.companies.map(company => [company.id, { hash: createHash('sha256'), count: 0 }]))
  for (const event of events) for (const grade of event.grades) {
    const value = expected.get(grade.company)
    if (value) { value.hash.update(event.id + '\n'); value.count++ }
  }
  return series.map(row => {
    const hash = createHash('sha256')
    let count = 0
    for (const bucket of row.buckets) for (const activity of bucket.activity ?? []) {
      hash.update(activity.event.id + '\n'); count++
    }
    const wanted = expected.get(row.id)!
    const digest = hash.digest('hex')
    assert.equal(count, wanted.count, `${row.id}: every company-matching activity remains present`)
    assert.equal(digest, wanted.hash.digest('hex'), `${row.id}: activity IDs and ordering remain exact`)
    return { id: row.id, count, sha256: digest }
  })
}

let correctness: unknown
let sourceResult: unknown
console.log(JSON.stringify({ phase: 'loaded', events: events.length, loadedMs, prefixLength, measuredEvents,
  heapMb: process.memoryUsage().heapUsed / 2 ** 20, rssMb: process.memoryUsage().rss / 2 ** 20 }))
if (mode === 'engine') {
  const aggregator = new Aggregator(dataset.companies)
  measure('preloadAdd', () => aggregator.addBatch(events.slice(0, prefixLength)))
  let series = measure('preloadSnapshot', () => aggregator.snapshot(cutoffFor(prefixLength)))
  measure('coldChart', () => project(series, cutoffFor(prefixLength)))
  measure('coldWindow', () => visible(series).map(row => windowStat(row.buckets, dataset.start, cutoffFor(prefixLength) + 1)))
  for (let start = prefixLength; start < events.length; start += batchSize) {
    const end = Math.min(start + batchSize, events.length)
    const cutoff = cutoffFor(end)
    measure('ongoingTick', () => {
      measure('addBatch', () => aggregator.addBatch(events.slice(start, end)))
      series = measure('snapshotDuringTick', () => aggregator.snapshot(cutoff))
      const projected = measure('chartDuringTick', () => project(series, cutoff))
      measure('drawProjectionDuringTick', () => drawProjection(projected))
      measure('windowDuringTick', () => visible(series).map(row => windowStat(row.buckets, dataset.start, cutoff + 1)))
    })
    console.log(JSON.stringify({ phase: 'tick', end, total: events.length }))
  }
  for (let i = 0; i < repeats; i++) {
    series = measure('snapshotOnly', () => aggregator.snapshot(now))
    measure('chartOnly', () => project(series, now))
    measure('windowOnly', () => visible(series).map(row => windowStat(row.buckets, view.start, view.end)))
  }
  assert.equal(aggregator.posts, actualCounts.posts)
  assert.equal(aggregator.likes, actualCounts.likes)
  const coverage = activityCoverage(series)
  const finalSeries = seriesSummary(series)
  const points = projectionSummary(project(series, now))
  const windows = visible(series).map(row => ({ id: row.id,
    full: windowStat(row.buckets, dataset.start, now + 1),
    last12h: windowStat(row.buckets, now - 12 * 3_600_000, now + 1),
    partial: windowStat(row.buckets, now - 7.125 * 3_600_000, now - 0.5 * 3_600_000) }))
  const history = [0.2, 0.6, 0.9].map(fraction => {
    const cutoff = cutoffFor(Math.max(1, Math.floor(events.length * fraction)))
    const historical = aggregator.snapshot(cutoff)
    return { cutoff, series: seriesSummary(historical), points: projectionSummary(project(historical, cutoff)),
      windows: visible(historical).map(row => ({ id: row.id, stats: windowStat(row.buckets, dataset.start, cutoff + 1) })) }
  })
  const resumed = seriesSummary(aggregator.snapshot(now))
  assert.deepEqual(resumed, finalSeries, 'historical inspection must not change the live snapshot')
  correctness = { counts: { posts: aggregator.posts, likes: aggregator.likes }, coverage, finalSeries, points, windows, history }
} else {
  // A deterministic transport burst: each actual frame contains 1,000 real
  // events. Timers fire after a group of six messages (100 ms of transport time).
  // Network and React/browser paint are deliberately excluded from these timings.
  const { createStreamSource } = await importEngine('streamSource') as typeof import('../src/data/streamSource.ts')
  const globals = globalThis as unknown as Record<string, unknown>
  const saved = new Map(['WebSocket', 'location', 'window', 'setTimeout', 'clearTimeout'].map(key => [key, globals[key]]))
  const pending = new Map<number, () => void>()
  let timerId = 0
  const sockets: MockSocket[] = []
  class MockSocket {
    static OPEN = 1
    static CONNECTING = 0
    readyState = 1
    onmessage?: (event: { data: string }) => void
    onclose?: () => void
    onopen?: () => void
    constructor(_url: string) { sockets.push(this) }
    send(_data: string) {}
    close() { this.readyState = 3; this.onclose?.() }
  }
  globals.WebSocket = MockSocket
  globals.location = { protocol: 'http:', host: 'replay-benchmark.local' }
  globals.window = globalThis
  globals.setTimeout = (callback: () => void) => { pending.set(++timerId, callback); return timerId }
  globals.clearTimeout = (id: number) => { pending.delete(id) }
  const flush = () => { const callbacks = [...pending.values()]; pending.clear(); for (const callback of callbacks) callback() }
  let latest: StreamSnapshot | undefined
  let publications = 0
  const source = createStreamSource()
  const unsubscribe = source.subscribe(snapshot => { latest = snapshot; publications++ })
  let sequence = 0
  const deliver = (message: ReplayMessage) => sockets.at(-1)!.onmessage!({ data: JSON.stringify(message) })
  try {
    deliver({ type: 'init', run: 'benchmark', start: dataset.start, end: dataset.end, companies: dataset.companies, speed: 14_400 })
    deliver({ type: 'batch', run: 'benchmark', sequence: sequence++, now: cutoffFor(prefixLength),
      events: events.slice(0, prefixLength), status: 'playing', speed: 14_400 })
    flush()
    publications = 0
    let frameCount = 0
    let framesInBurst = 0
    let burstElapsedMs = 0
    let burstCpuMs = 0
    for (let start = prefixLength; start < events.length; start += batchSize) {
      const end = Math.min(start + batchSize, events.length)
      const wire = JSON.stringify({ type: 'batch', run: 'benchmark', sequence: sequence++, now: cutoffFor(end),
        events: events.slice(start, end), status: 'playing', speed: 14_400 })
      measure('sourceMessage', () => sockets.at(-1)!.onmessage!({ data: wire }))
      const sample = timings.sourceMessage.at(-1)!
      burstElapsedMs += sample.elapsedMs; burstCpuMs += sample.cpuMs
      frameCount++; framesInBurst++
      if (framesInBurst === 6 || end === events.length) {
        measure('sourceTimer', flush)
        const timer = timings.sourceTimer.at(-1)!
        ;(timings.sourceBurst ??= []).push({ elapsedMs: burstElapsedMs + timer.elapsedMs, cpuMs: burstCpuMs + timer.cpuMs })
        burstElapsedMs = 0; burstCpuMs = 0; framesInBurst = 0
      }
    }
    assert.ok(latest)
    assert.notEqual(latest.status, 'error', latest.error)
    assert.equal(latest.read, actualCounts.posts)
    assert.equal(latest.kept, actualCounts.likes)
    correctness = { counts: { posts: latest.read, likes: latest.kept }, coverage: activityCoverage(latest.series),
      finalSeries: seriesSummary(latest.series), points: projectionSummary(project(latest.series, now)) }
    sourceResult = { deliveredFrames: frameCount, measuredPublications: publications, messagesPerTimerFlush: 6,
      timerIntervalModelMs: 100, deliveredEvents: measuredEvents, pendingTimersAfterFlush: pending.size }
  } finally {
    unsubscribe()
    for (const [key, value] of saved) { if (value === undefined) delete globals[key]; else globals[key] = value }
  }
}

if (mode === 'engine') {
  // Full-history analytics is measured separately: the default line chart does
  // not call that extra query. Sum measured phases to describe its actual work.
  const phases = ['addBatch', 'snapshotDuringTick', 'chartDuringTick', 'drawProjectionDuringTick']
  timings.lineChartTick = timings.addBatch.map((_value, index) => phases.reduce((total, name) => ({
    elapsedMs: total.elapsedMs + timings[name][index].elapsedMs,
    cpuMs: total.cpuMs + timings[name][index].cpuMs,
  }), { elapsedMs: 0, cpuMs: 0 }))
}
const report = {
  variant, mode, capturedAt: new Date().toISOString(), node: process.version, platform: process.platform,
  fixture: { path: fixture, compressedBytes: statSync(fixture).size, metadataCounts: dataset.counts,
    loadedEvents: dataset.events.length, testedEvents: events.length, fullDataset: maxEvents === Infinity && mode === 'engine', loadedMs },
  scenario: { prefixLength, measuredEvents, batchSize, requestedSamples: samples, actualSamples: Math.ceil(measuredEvents / batchSize),
    firstMeasuredTime: events[prefixLength].t, finalTime: now, visibleCompanies: visibleIds,
    chartIntervalMs: DEFAULT_LINE_INTERVAL, chartView: view, bucketMs: BUCKET_MS,
    notes: 'Synchronous Node elapsed/process CPU, excluding browser DOM/layout/paint. Prefix preloaded once; tail ticks measured individually.' },
  engineHashes: Object.fromEntries(['activity', 'aggregator', 'chartData', 'window', 'streamSource'].map(name => [name, fingerprint(name)])),
  metrics: Object.fromEntries(Object.entries(timings).map(([name, values]) => [name, summarize(values)])),
  samples: timings, sourceResult, memory: process.memoryUsage(), drawSink, correctness,
}
const encode = (value: unknown) => JSON.stringify(value, (_key, item) => typeof item === 'number' && !Number.isFinite(item) ? String(item) : item, 2)
mkdirSync(dirname(output), { recursive: true })
writeFileSync(output, encode(report) + '\n')

function compareExact(actual: unknown, expected: unknown, path = 'correctness'): void {
  if (typeof actual === 'number' && typeof expected === 'number') {
    assert.ok(Math.abs(actual - expected) <= 1e-10 * Math.max(1, Math.abs(actual), Math.abs(expected)), `${path}: ${actual} != ${expected}`)
  } else if (Array.isArray(expected)) {
    assert.ok(Array.isArray(actual), `${path}: expected array`)
    assert.equal(actual.length, expected.length, `${path}.length`)
    expected.forEach((value, index) => compareExact(actual[index], value, `${path}[${index}]`))
  } else if (expected !== null && typeof expected === 'object') {
    assert.ok(actual !== null && typeof actual === 'object', `${path}: expected object`)
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort(), `${path}: object keys`)
    for (const key of Object.keys(expected)) compareExact((actual as Record<string, unknown>)[key], (expected as Record<string, unknown>)[key], `${path}.${key}`)
  } else assert.equal(actual, expected, path)
}
let comparison: unknown
if (args.has('compare')) {
  const baseline = JSON.parse(readFileSync(resolve(args.get('compare')!), 'utf8')) as typeof report
  const comparable = JSON.parse(encode(report)) as typeof report
  assert.equal(comparable.mode, baseline.mode)
  assert.equal(comparable.fixture.loadedEvents, baseline.fixture.loadedEvents)
  assert.deepEqual(comparable.scenario, baseline.scenario)
  compareExact(comparable.correctness, baseline.correctness)
  comparison = { exactCountsActivityIdsPopupFieldsAndPointShapes: true, numericRelativeTolerance: 1e-10,
    metrics: Object.fromEntries(Object.entries(report.metrics).filter(([name]) => baseline.metrics[name]).map(([name, result]) => [name, {
      beforeP50Ms: baseline.metrics[name].p50Ms, afterP50Ms: result.p50Ms,
      beforeP95Ms: baseline.metrics[name].p95Ms, afterP95Ms: result.p95Ms,
      p50Speedup: baseline.metrics[name].p50Ms / result.p50Ms,
    }])) }
  writeFileSync(output.replace(/\.json$/, '-comparison.json'), encode(comparison) + '\n')
}
console.log(encode({ output, metrics: report.metrics, sourceResult, comparison }))
