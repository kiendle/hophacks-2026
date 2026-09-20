/**
 * A browser without a browser: a clock that is moved by hand, a microphone that is as loud as the
 * test says, a player that finishes when the test says, and a voice service that answers without a
 * network. Everything web/src/live/core.ts touches comes through here, so every rule in it can be
 * run under node.
 */
import type { Devices, Mic, Playing } from '../../src/live/core'
import { settle } from './check'

export class Clock {
  time = 1_000_000
  private tickers: { ms: number; tick: () => void; on: boolean }[] = []

  now = () => this.time

  every = (ms: number, tick: () => void) => {
    const entry = { ms, tick, on: true }
    this.tickers.push(entry)
    return () => {
      entry.on = false
    }
  }

  get running() {
    return this.tickers.filter((entry) => entry.on).length
  }

  /** Moves time forward in one-tick steps, letting the promises in between run. */
  async advance(ms: number) {
    const step = Math.min(...this.tickers.map((entry) => entry.ms), ms) || ms
    for (let left = ms; left > 0; left -= step) {
      this.time += step
      for (const entry of [...this.tickers]) if (entry.on) entry.tick()
      await settle()
    }
  }
}

export class FakeMic implements Mic {
  mime = 'audio/webm'
  loud = 0
  closed = 0
  /** How many pieces of sound the recorder has handed over in this session. */
  pieces = 0
  private onData: (piece: Blob) => void = () => {}

  level = () => this.loud
  onPiece = (fn: (piece: Blob) => void) => {
    this.onData = fn
  }
  close = () => {
    this.closed += 1
  }

  /** One recorder piece, as a real one would arrive every quarter second. */
  piece(bytes = 4096) {
    this.pieces += 1
    this.onData(new Blob([new Uint8Array(bytes)], { type: 'audio/webm' }))
  }
}

export class FakePlaying implements Playing {
  stopped = false
  done: Promise<void>
  private settle: () => void = () => {}

  constructor(public clip: Blob) {
    this.done = new Promise<void>((resolve) => {
      this.settle = resolve
    })
  }

  stop = () => {
    this.stopped = true
    this.settle()
  }

  /** The clip played all the way to its end. */
  finish() {
    this.settle()
  }
}

export interface SayCall {
  text: string
  signal: AbortSignal | null
  answer(): void
}

export class FakeDevices implements Devices {
  mic = new FakeMic()
  clock = new Clock()
  opens = 0
  refuseMic = false
  /** Every /api/live/say request, in the order it was made. */
  says: SayCall[] = []
  /** Every /api/live/listen request, in the order it was made. */
  listens: Blob[] = []
  played: FakePlaying[] = []
  /** What the server says the person said. */
  heard: string | null = 'How are people talking about the launch'
  listenStatus = 200
  sayStatus = 200
  /** Say requests wait for the test to answer them, instead of coming back at once. */
  holdSay = false
  breakListen = false

  now = () => this.clock.now()
  every = (ms: number, tick: () => void) => this.clock.every(ms, tick)

  open = async (): Promise<Mic> => {
    this.opens += 1
    if (this.refuseMic) throw new Error('the browser said no')
    return this.mic
  }

  play = (clip: Blob): Playing => {
    const playing = new FakePlaying(clip)
    this.played.push(playing)
    return playing
  }

  fetch = (url: string, init: RequestInit = {}): Promise<Response> => {
    if (url === '/api/live/listen') return this.listen(init)
    if (url === '/api/live/say') return this.say(init)
    return Promise.resolve(reply(404, { error: { message: 'No such route.' } }))
  }

  private listen(init: RequestInit): Promise<Response> {
    if (this.breakListen) return Promise.reject(new Error('the laptop is offline'))
    this.listens.push(init.body as Blob)
    if (this.listenStatus !== 200) {
      return Promise.resolve(reply(this.listenStatus, { error: { message: 'We could not turn that into words.' } }))
    }
    return Promise.resolve(reply(200, { text: this.heard ?? '', language: 'English', seconds: 1.4 }))
  }

  private say(init: RequestInit): Promise<Response> {
    const text = JSON.parse(String(init.body || '{}')).text as string
    const signal = (init.signal as AbortSignal) || null
    return new Promise<Response>((resolve, reject) => {
      const answer = () => {
        if (this.sayStatus !== 200) {
          resolve(reply(this.sayStatus, { error: { message: 'That sentence could not be read out loud.' } }))
          return
        }
        resolve(audio(new Blob([new Uint8Array(32)], { type: 'audio/mpeg' })))
      }
      const call: SayCall = { text, signal, answer }
      this.says.push(call)
      signal?.addEventListener('abort', () => reject(new Error('aborted')))
      if (!this.holdSay) answer()
    })
  }

  /** The text of every sentence that was fetched to be spoken, in order. */
  get spoken() {
    return this.says.map((call) => call.text)
  }
}

function reply(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    blob: async () => new Blob([JSON.stringify(body)]),
  } as unknown as Response
}

function audio(clip: Blob): Response {
  return { ok: true, status: 200, json: async () => ({}), blob: async () => clip } as unknown as Response
}

/** Loudness over time, as the sampler would read it: `pattern` is [level, milliseconds] pairs. */
export async function speak(devices: FakeDevices, pattern: [number, number][]) {
  for (const [level, ms] of pattern) {
    devices.mic.loud = level
    let left = ms
    while (left > 0) {
      const step = Math.min(250, left)
      await devices.clock.advance(step)
      devices.mic.piece() // the recorder hands over a piece of sound four times a second
      left -= step
    }
  }
}
