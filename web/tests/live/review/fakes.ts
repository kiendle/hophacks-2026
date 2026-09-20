/**
 * Fakes for the review tests.
 *
 * The same clock, microphone and player the other live tests use, plus two things they do not have:
 * a voice service that can be held at every step a real streamed answer passes through (the moment
 * the server answers, and the moment the last byte of sound arrives), and a microphone that can be
 * left half open, the way a real one is while the browser is still asking the person for it.
 */
import type { Devices, Mic, Playing } from '../../../src/live/core'
import { Clock, FakeMic, FakePlaying } from '../fakes'

function reply(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    blob: async () => new Blob([JSON.stringify(body)]),
  } as unknown as Response
}

/** One request to read a sentence out loud, stopped wherever the test wants it stopped. */
export class Say {
  text: string
  /** headers: the server has not answered yet. body: it is streaming the sound. done: all of it is here. */
  phase: 'headers' | 'body' | 'done' = 'headers'
  aborted = false
  abortedIn: '' | 'headers' | 'body' | 'done' = ''
  response: Promise<Response>
  private clip: Promise<Blob>
  private openHeaders: (response: Response) => void = () => {}
  private failHeaders: (error: unknown) => void = () => {}
  private openBody: (clip: Blob) => void = () => {}
  private failBody: (error: unknown) => void = () => {}

  constructor(text: string, signal: AbortSignal | null) {
    this.text = text
    this.response = new Promise<Response>((resolve, reject) => {
      this.openHeaders = resolve
      this.failHeaders = reject
    })
    this.clip = new Promise<Blob>((resolve, reject) => {
      this.openBody = resolve
      this.failBody = reject
    })
    this.response.catch(() => {})
    this.clip.catch(() => {})
    signal?.addEventListener('abort', () => {
      this.aborted = true
      this.abortedIn = this.phase
      if (this.phase === 'headers') this.failHeaders(new Error('aborted'))
      else if (this.phase === 'body') this.failBody(new Error('aborted'))
    })
  }

  /** The server has answered and the sound is on its way, byte by byte. */
  headers() {
    if (this.phase !== 'headers') return
    this.phase = 'body'
    this.openHeaders({
      ok: true,
      status: 200,
      json: async () => ({}),
      blob: () => this.clip,
    } as unknown as Response)
  }

  /** The last byte of the clip. */
  body(bytes = 32) {
    if (this.phase !== 'body') return
    this.phase = 'done'
    this.openBody(new Blob([new Uint8Array(bytes)], { type: 'audio/mpeg' }))
  }

  /** A short sentence that came back all at once. */
  all() {
    this.headers()
    this.body()
  }
}

/** One turn of speech on its way to becoming words. */
export class Listen {
  body: Blob
  answered = false
  private open: (response: Response) => void
  private fail: (error: unknown) => void

  constructor(body: Blob, open: (response: Response) => void, fail: (error: unknown) => void) {
    this.body = body
    this.open = open
    this.fail = fail
  }

  answer(text: string) {
    this.answered = true
    this.open(reply(200, { text, language: 'English', seconds: 1.4 }))
  }

  refuse(status = 422) {
    this.answered = true
    this.open(reply(status, { error: { message: 'We could not turn that into words.' } }))
  }

  break() {
    this.answered = true
    this.fail(new Error('the laptop is offline'))
  }
}

export class ReviewDevices implements Devices {
  clock = new Clock()
  /** One for every time the microphone was opened, in order. */
  mics: FakeMic[] = []
  opens = 0
  /** The browser is still asking the person for the microphone. */
  holdOpen = false
  waitingOpens: (() => void)[] = []
  played: FakePlaying[] = []
  says: Say[] = []
  listens: Listen[] = []
  /** Sentences wait for the test to answer them, as a real service makes the person wait. */
  holdSay = true
  holdListen = false
  heard = 'How are people talking about the launch'

  now = () => this.clock.now()
  every = (ms: number, tick: () => void) => this.clock.every(ms, tick)

  open = async (): Promise<Mic> => {
    this.opens += 1
    const mic = new FakeMic()
    this.mics.push(mic)
    if (!this.holdOpen) return mic
    return new Promise<Mic>((resolve) => {
      this.waitingOpens.push(() => resolve(mic))
    })
  }

  play = (clip: Blob): Playing => {
    const playing = new FakePlaying(clip)
    this.played.push(playing)
    return playing
  }

  fetch = (url: string, init: RequestInit = {}): Promise<Response> => {
    if (url === '/api/live/say') {
      const text = JSON.parse(String(init.body || '{}')).text as string
      const call = new Say(text, (init.signal as AbortSignal) ?? null)
      this.says.push(call)
      if (!this.holdSay) call.all()
      return call.response
    }
    if (url === '/api/live/listen') {
      let open: (response: Response) => void = () => {}
      let fail: (error: unknown) => void = () => {}
      const response = new Promise<Response>((resolve, reject) => {
        open = resolve
        fail = reject
      })
      const call = new Listen(init.body as Blob, open, fail)
      this.listens.push(call)
      if (!this.holdListen) call.answer(this.heard)
      return response
    }
    return Promise.resolve(reply(404, { error: { message: 'No such route.' } }))
  }

  /** The text of every sentence that was fetched to be spoken, in order. */
  get spoken() {
    return this.says.map((call) => call.text)
  }
}

/** Loudness over time on one microphone: `pattern` is [level, milliseconds] pairs. */
export async function sound(devices: ReviewDevices, pattern: [number, number][], which = 0) {
  const mic = devices.mics[which]
  for (const [level, ms] of pattern) {
    mic.loud = level
    let left = ms
    while (left > 0) {
      const step = Math.min(250, left)
      await devices.clock.advance(step)
      mic.piece()
      left -= step
    }
  }
}

/** The first second of a session: the room being measured. */
export const room = (devices: ReviewDevices, level = 0.002) => sound(devices, [[level, 1100]])
