/**
 * The real microphone, the real player and the real clock, behind the small `Devices` port that
 * web/src/live/core.ts talks to.
 *
 * Everything the browser gives us is taken in through `Browser`, so the same code runs under node
 * with fakes: that is how the tests can prove the microphone is handed back on every exit path
 * without a microphone. Nothing here decides anything about the conversation.
 */
import { TIMESLICE, type Devices, type Mic, type Playing } from './core'

const RECORD_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
  'audio/wav',
]

export interface TrackLike {
  stop(): void
}

export interface StreamLike {
  getTracks(): TrackLike[]
}

export interface RecorderLike {
  state: string
  start(timeslice: number): void
  stop(): void
  ondataavailable: ((event: { data: Blob | null }) => void) | null
}

export interface AnalyserLike {
  fftSize: number
  getByteTimeDomainData(buffer: Uint8Array): void
}

export interface ContextLike {
  createAnalyser(): AnalyserLike
  createMediaStreamSource(stream: StreamLike): { connect(node: AnalyserLike): void }
  /** Wakes a context the browser made asleep. Not every browser has one to wake. */
  resume?(): unknown
  close(): unknown
}

export interface PlayerLike {
  onended: (() => void) | null
  onerror: (() => void) | null
  play(): unknown
  pause(): void
}

/** Everything web/src/live takes from the browser, in one place a fake can stand in for. */
export interface Browser {
  getUserMedia(): Promise<StreamLike>
  recorder(stream: StreamLike, mime: string): RecorderLike
  supports(mime: string): boolean
  context(): ContextLike
  player(url: string): PlayerLike
  url(clip: Blob): string
  revoke(url: string): void
  fetch(url: string, init?: RequestInit): Promise<Response>
  now(): number
  every(ms: number, tick: () => void): () => void
}

/** The browser we are actually in, or null when it cannot record or play sound at all. */
export function browser(): Browser | null {
  const window = globalThis as unknown as {
    MediaRecorder?: {
      new (stream: unknown, options?: { mimeType?: string }): RecorderLike
      isTypeSupported?(mime: string): boolean
    }
    AudioContext?: new () => ContextLike
    webkitAudioContext?: new () => ContextLike
    Audio?: new (src: string) => PlayerLike
    URL?: { createObjectURL(clip: Blob): string; revokeObjectURL(url: string): void }
    navigator?: { mediaDevices?: { getUserMedia(constraints: unknown): Promise<StreamLike> } }
  }
  const Recorder = window.MediaRecorder
  const Context = window.AudioContext || window.webkitAudioContext
  const Player = window.Audio
  const media = window.navigator?.mediaDevices
  if (!Recorder || !Context || !Player || !media || !window.URL) return null
  return {
    getUserMedia: () =>
      media.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }),
    recorder: (stream, mime) => new Recorder(stream, mime ? { mimeType: mime } : undefined),
    supports: (mime) => Boolean(Recorder.isTypeSupported?.(mime)),
    context: () => new Context(),
    player: (url) => new Player(url),
    url: (clip) => window.URL!.createObjectURL(clip),
    revoke: (url) => window.URL!.revokeObjectURL(url),
    fetch: (url, init) => fetch(url, init),
    now: () => Date.now(),
    every: (ms, tick) => {
      const id = setInterval(tick, ms)
      return () => clearInterval(id)
    },
  }
}

/** One loudness reading, 0 to about 1: how far the sound is from silence on average. */
function loudness(analyser: AnalyserLike, buffer: Uint8Array): number {
  analyser.getByteTimeDomainData(buffer)
  let sum = 0
  for (let at = 0; at < buffer.length; at += 1) {
    const away = (buffer[at] - 128) / 128
    sum += away * away
  }
  return Math.sqrt(sum / buffer.length)
}

async function open(env: Browser): Promise<Mic> {
  const stream = await env.getUserMedia()
  let context: ContextLike | null = null
  let recorder: RecorderLike | null = null
  try {
    context = env.context()
    // A context made a moment after the click can start asleep, and a sleeping one hears only
    // silence: the meter stays flat and no turn ever begins. Waking one that is awake does nothing.
    try {
      const waking = context.resume?.()
      if (waking && typeof (waking as Promise<void>).catch === 'function') (waking as Promise<void>).catch(() => {})
    } catch {
      /* it stays as the browser made it */
    }
    const analyser = context.createAnalyser()
    analyser.fftSize = 1024
    context.createMediaStreamSource(stream).connect(analyser)
    const buffer = new Uint8Array(analyser.fftSize)
    const mime = RECORD_TYPES.find((type) => env.supports(type)) || ''
    recorder = env.recorder(stream, mime)
    let onPiece: (piece: Blob) => void = () => {}
    recorder.ondataavailable = (event) => {
      if (event && event.data) onPiece(event.data)
    }
    recorder.start(TIMESLICE)
    const made = recorder
    let released = false
    return {
      mime: mime.split(';')[0] || 'audio/webm',
      level: () => {
        try {
          return loudness(analyser, buffer)
        } catch {
          return 0 // a context that has gone away is silence, not a crash
        }
      },
      onPiece: (fn) => {
        onPiece = fn
      },
      close: () => {
        if (released) return // a session can end more than one way at once, and often does
        released = true
        release(stream, made, context)
      },
    }
  } catch (error) {
    release(stream, recorder, context) // the microphone is never left open behind a failed start
    throw error
  }
}

/** The one place a microphone is handed back: the recorder, then the tracks, then the sound context. */
function release(stream: StreamLike | null, recorder: RecorderLike | null, context: ContextLike | null) {
  try {
    if (recorder && recorder.state !== 'inactive') recorder.stop()
  } catch {
    /* already stopped */
  }
  try {
    for (const track of stream?.getTracks() || []) track.stop()
  } catch {
    /* already gone */
  }
  try {
    const closing = context?.close()
    if (closing && typeof (closing as Promise<void>).catch === 'function') (closing as Promise<void>).catch(() => {})
  } catch {
    /* already closed */
  }
}

/** Plays one clip from a blob address, and gives the address back when it is done with it. */
function play(env: Browser, clip: Blob): Playing {
  let settle: () => void = () => {}
  const done = new Promise<void>((resolve) => {
    settle = resolve
  })
  const url = env.url(clip)
  const player = env.player(url)
  let over = false
  const finish = () => {
    if (over) return
    over = true
    player.onended = null
    player.onerror = null
    try {
      player.pause()
    } catch {
      /* already stopped */
    }
    try {
      env.revoke(url)
    } catch {
      /* already gone */
    }
    settle()
  }
  player.onended = finish
  player.onerror = finish
  try {
    const started = player.play()
    // A browser that wants a click before it makes a sound: stay quiet, and never throw.
    if (started && typeof (started as Promise<void>).catch === 'function') (started as Promise<void>).catch(finish)
  } catch {
    finish()
  }
  return { stop: finish, done }
}

/** The `Devices` core.ts needs, made out of one browser. */
export function createDevices(env: Browser): Devices {
  return {
    open: () => open(env),
    play: (clip: Blob) => play(env, clip),
    fetch: (url, init) => env.fetch(url, init),
    now: () => env.now(),
    every: (ms, tick) => env.every(ms, tick),
  }
}
