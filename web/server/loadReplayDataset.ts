import { createReadStream } from 'node:fs'
import { createInterface } from 'node:readline'
import { Transform } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { StringDecoder } from 'node:string_decoder'
import { createGunzip } from 'node:zlib'
import type { ReplayDataset, ReplayEvent } from '../src/data/replayTypes.ts'

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function metadata(value: unknown): asserts value is ReplayDataset {
  if (!record(value) || value.version !== 1 || !Number.isFinite(value.start) ||
      !Number.isFinite(value.end) || Number(value.start) >= Number(value.end) ||
      !Array.isArray(value.companies) || value.companies.some(company =>
        !record(company) || typeof company.id !== 'string' || typeof company.name !== 'string') ||
      !record(value.counts) || !Number.isSafeInteger(value.counts.posts) || Number(value.counts.posts) < 0 ||
      !Number.isSafeInteger(value.counts.likes) || Number(value.counts.likes) < 0 || !Array.isArray(value.events)) {
    throw new Error('Invalid replay dataset metadata.')
  }
}

function event(value: unknown): asserts value is ReplayEvent {
  if (!record(value) || typeof value.id !== 'string' || typeof value.postId !== 'string' ||
      (value.kind !== 'post' && value.kind !== 'like') || !Number.isFinite(value.t) ||
      typeof value.text !== 'string' || !Array.isArray(value.grades)) {
    throw new Error('Invalid replay event.')
  }
}

/** Read line-framed JSON without building a full decompressed JSON string.
 * Older, single-line packages remain supported. Events stay in memory for replay.
 */
export async function loadReplayDataset(path: string | URL): Promise<ReplayDataset> {
  const input = createReadStream(path)
  const gunzip = createGunzip()
  const utf8 = new StringDecoder('utf8')
  // These characters are legal inside JSON strings, but readline also treats
  // Unicode line separators as record boundaries. JSON escapes preserve them.
  const protectSeparators = (text: string) => text.replace(/[\u0085\u2028\u2029]/g,
    character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'))
  const decoded = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      callback(null, protectSeparators(utf8.write(chunk)))
    },
    flush(callback) { callback(null, protectSeparators(utf8.end())) },
  })
  const lines = createInterface({ input: decoded, crlfDelay: Infinity })
  let streamError: unknown
  // readline does not reliably forward input errors through its async iterator.
  // Closing it here also prevents a missing/corrupt file from leaving a pending read.
  const complete = pipeline(input, gunzip, decoded).catch(error => {
    streamError = error
    lines.close()
  })
  let dataset: ReplayDataset | undefined
  let closed = false
  let comma = false
  let posts = 0
  let likes = 0
  const count = (value: unknown) => {
    event(value)
    if (value.kind === 'post') posts++
    else likes++
    if (posts > dataset!.counts.posts || likes > dataset!.counts.likes) {
      throw new Error('Replay event counts do not match the dataset metadata.')
    }
  }
  try {
    for await (const raw of lines) {
      const line = raw.trim()
      if (!dataset) {
        const framed = /"events"\s*:\s*\[\s*$/.test(line)
        const value: unknown = JSON.parse(framed ? line + ']}' : line)
        metadata(value)
        dataset = value
        if (!framed) {
          for (const item of dataset.events) count(item)
          closed = true
        }
      } else if (closed) {
        if (line) throw new Error('Unexpected content after the replay dataset.')
      } else if (line === ']}') {
        if (comma) throw new Error('Trailing comma in the replay events array.')
        closed = true
      } else {
        if (!line || (dataset.events.length > 0 && !comma)) {
          throw new Error('Invalid replay event framing: expected a comma between events.')
        }
        comma = line.endsWith(',')
        const value: unknown = JSON.parse(comma ? line.slice(0, -1) : line)
        count(value)
        dataset.events.push(value as ReplayEvent)
      }
    }
    await complete
    if (streamError) throw streamError
    if (!dataset || !closed) throw new Error('Truncated replay dataset.')
    if (posts !== dataset.counts.posts || likes !== dataset.counts.likes) {
      throw new Error('Replay event counts do not match the dataset metadata.')
    }
    return dataset
  } finally {
    lines.close()
    input.destroy()
    gunzip.destroy()
    decoded.destroy()
  }
}
