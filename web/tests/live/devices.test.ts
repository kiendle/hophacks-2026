/**
 * Run: npx -y tsx web/tests/live/devices.test.ts
 *
 * The only part of Talk live that touches the browser: opening the microphone, reading how loud
 * the room is, playing one clip, and handing all of it back. A microphone left open is the one
 * mistake a person notices, so every way out is checked here.
 */
import { createDevices, type AnalyserLike, type Browser, type ContextLike, type PlayerLike, type RecorderLike, type StreamLike } from '../../src/live/devices'
import { check, report, settle } from './check'

class FakeBrowser implements Browser {
  stopped = 0
  closed = 0
  started: number[] = []
  urls: string[] = []
  revoked: string[] = []
  made = 0
  players: FakePlayer[] = []
  recorder: FakeRecorder | null = null
  wave = 128 // the middle of the range is silence
  breakRecorder = false
  refuse = false
  sleepy: '' | 'wakes' | 'throws' | 'rejects' = '' // a context the browser made asleep
  woken = 0
  supported = ['audio/webm;codecs=opus', 'audio/webm']
  private stream: StreamLike

  constructor() {
    const track = { stop: () => { this.stopped += 1 } }
    this.stream = { getTracks: () => [track, track] }
  }

  getUserMedia = async (): Promise<StreamLike> => {
    if (this.refuse) throw new Error('the person said no')
    return this.stream
  }

  recorderFn = (stream: StreamLike, mime: string): RecorderLike => {
    void stream
    if (this.breakRecorder) throw new Error('this browser cannot record')
    const made = new FakeRecorder(mime, this.started)
    this.recorder = made
    return made
  }

  supports = (mime: string) => this.supported.includes(mime)

  context = (): ContextLike => {
    this.made += 1
    let shut = false
    const analyser: AnalyserLike = {
      fftSize: 0,
      getByteTimeDomainData: (buffer: Uint8Array) => {
        if (shut) throw new Error('this sound context is closed') // what a real one does
        for (let at = 0; at < buffer.length; at += 1) buffer[at] = this.wave
      },
    }
    const made: ContextLike = {
      createAnalyser: () => analyser,
      createMediaStreamSource: () => ({ connect: () => {} }),
      close: () => {
        this.closed += 1
        shut = true
      },
    }
    if (this.sleepy) {
      made.resume = () => {
        this.woken += 1
        if (this.sleepy === 'throws') throw new Error('this context cannot be woken')
        return this.sleepy === 'rejects' ? Promise.reject(new Error('not allowed to start')) : Promise.resolve()
      }
    }
    return made
  }

  player = (url: string): PlayerLike => {
    const made = new FakePlayer(url)
    this.players.push(made)
    return made
  }

  url = (clip: Blob) => {
    const made = `blob:live/${this.urls.length}/${clip.size}`
    this.urls.push(made)
    return made
  }

  revoke = (url: string) => {
    this.revoked.push(url)
  }

  fetch = async () => ({ ok: true }) as unknown as Response
  now = () => 1_000_000
  every = (ms: number, tick: () => void) => {
    void ms
    void tick
    return () => {}
  }
}

class FakeRecorder implements RecorderLike {
  state = 'inactive'
  ondataavailable: ((event: { data: Blob | null }) => void) | null = null
  mime: string
  private started: number[]

  constructor(mime: string, started: number[]) {
    this.mime = mime
    this.started = started
  }

  start(timeslice: number) {
    this.state = 'recording'
    this.started.push(timeslice)
  }

  stop() {
    this.state = 'inactive'
  }
}

class FakePlayer implements PlayerLike {
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  paused = 0
  refuse = false
  url: string

  constructor(url: string) {
    this.url = url
  }

  play() {
    return this.refuse ? Promise.reject(new Error('this browser wants a click first')) : Promise.resolve()
  }

  pause() {
    this.paused += 1
  }
}

/** The Browser the real code expects, built out of the fake above. */
function fake(): { env: Browser; spy: FakeBrowser } {
  const spy = new FakeBrowser()
  const env: Browser = {
    getUserMedia: spy.getUserMedia,
    recorder: (stream, mime) => spy.recorderFn(stream, mime),
    supports: spy.supports,
    context: spy.context,
    player: spy.player,
    url: spy.url,
    revoke: spy.revoke,
    fetch: spy.fetch,
    now: spy.now,
    every: spy.every,
  }
  return { env, spy }
}

async function micChecks() {
  const { env, spy } = fake()
  const devices = createDevices(env)
  const mic = await devices.open()
  check('1a the best sound format this browser has is the one it records in',
    spy.recorder?.mime === 'audio/webm;codecs=opus' && mic.mime === 'audio/webm',
    `${spy.recorder?.mime} recorded, sent as ${mic.mime}`)
  check('1b the recorder hands over a piece of sound four times a second',
    spy.started.length === 1 && spy.started[0] === 250, JSON.stringify(spy.started))

  const pieces: Blob[] = []
  mic.onPiece((piece) => pieces.push(piece))
  spy.recorder?.ondataavailable?.({ data: new Blob([new Uint8Array(16)]) })
  spy.recorder?.ondataavailable?.({ data: null })
  check('1c and every piece reaches whoever asked for them, with nothing in place of a missing one',
    pieces.length === 1, `${pieces.length} pieces`)

  check('1d a silent room reads as no level at all', mic.level() === 0, String(mic.level()))
  spy.wave = 200
  check('1e and a loud one reads as a level worth acting on', mic.level() > 0.4, String(mic.level().toFixed(3)))

  mic.close()
  check('1f closing stops the recorder, drops every track and closes the sound context',
    spy.recorder?.state === 'inactive' && spy.stopped === 2 && spy.closed === 1,
    `recorder ${spy.recorder?.state}, ${spy.stopped} tracks stopped, context closed ${spy.closed} times`)
  mic.close()
  check('1g and closing twice is safe', spy.closed === 1, `${spy.closed}`)
}

async function micFailureChecks() {
  const { env, spy } = fake()
  spy.breakRecorder = true
  let refused = ''
  try {
    await createDevices(env).open()
  } catch (error) {
    refused = error instanceof Error ? error.message : String(error)
  }
  check('2a a browser that cannot record leaves no microphone open behind it',
    refused.includes('cannot record') && spy.stopped === 2 && spy.closed === 1,
    `${refused}, ${spy.stopped} tracks stopped, context closed ${spy.closed} times`)

  const said = fake()
  said.spy.refuse = true
  let told = ''
  try {
    await createDevices(said.env).open()
  } catch (error) {
    told = error instanceof Error ? error.message : String(error)
  }
  check('2b a person who says no is passed straight back, with nothing opened',
    told.includes('said no') && said.spy.made === 0, `${told}, ${said.spy.made} sound contexts`)

  const gone = fake()
  const devices = createDevices(gone.env)
  const mic = await devices.open()
  gone.spy.wave = 220 // a room that was loud a moment ago
  mic.close()
  check('2c reading the level of a microphone that has gone is silence, not a crash', mic.level() === 0, String(mic.level()))

  const asleep = fake()
  asleep.spy.sleepy = 'wakes'
  const woken = await createDevices(asleep.env).open()
  check('2d a sound context the browser made asleep is woken, or the meter would stay flat for ever',
    asleep.spy.woken === 1, `${asleep.spy.woken} wake ups`)
  woken.close()

  let opened = 0
  for (const kind of ['throws', 'rejects'] as const) {
    const stubborn = fake()
    stubborn.spy.sleepy = kind
    try {
      const still = await createDevices(stubborn.env).open()
      await settle()
      opened += 1
      still.close()
    } catch {
      /* counted below */
    }
  }
  check('2e and one that will not wake is still a microphone, never a crash', opened === 2, `${opened} of 2 opened`)
}

async function playChecks() {
  const { env, spy } = fake()
  const devices = createDevices(env)
  const clip = new Blob([new Uint8Array(64)], { type: 'audio/mpeg' })
  const playing = devices.play(clip)
  let over = false
  void playing.done.then(() => {
    over = true
  })
  await settle()
  check('3a the clip is played from its own address', spy.players.length === 1 && spy.urls.length === 1 && !over,
    `${spy.players.length} players`)

  spy.players[0].onended?.()
  await settle()
  check('3b when it finishes, the address is given back and the wait is over',
    over && spy.revoked.length === 1 && spy.revoked[0] === spy.urls[0], `${spy.revoked.length} addresses given back`)

  const second = devices.play(clip)
  let stopped = false
  void second.done.then(() => {
    stopped = true
  })
  second.stop()
  await settle()
  check('3c stopping it pauses the sound, gives the address back and ends the wait',
    stopped && spy.players[1].paused === 1 && spy.revoked.length === 2, `${spy.revoked.length} addresses given back`)
  second.stop()
  check('3d and stopping twice gives nothing back twice', spy.revoked.length === 2, `${spy.revoked.length}`)

  const quiet = fake()
  quiet.spy.players.length = 0
  const player = createDevices(quiet.env)
  const third = player.play(clip)
  quiet.spy.players[0].refuse = true
  let done = false
  void third.done.then(() => {
    done = true
  })
  quiet.spy.players[0].onerror?.()
  await settle()
  check('3e a browser that wants a click first stays quiet and never leaves a wait hanging',
    done && quiet.spy.revoked.length === 1, `${quiet.spy.revoked.length} addresses given back`)
}

async function main() {
  await micChecks()
  await micFailureChecks()
  await playChecks()
  process.exit(report('Talk live device'))
}

void main()
