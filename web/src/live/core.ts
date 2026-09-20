/**
 * Talk live, with no browser and no React in it: the ears, the mouth and the turn taking.
 *
 * The local assistant is the only brain. This file decides when the person has stopped speaking,
 * hands those seconds to the server to become words, and gives them to whatever `send` it was
 * built with, so a spoken question walks exactly the same path as a typed one. The answer is cut
 * into sentences as it streams and each one is fetched and played while the next is still arriving,
 * so the first words are heard long before the last one is written. Nothing here writes a word of
 * what is said.
 *
 * Everything it touches comes in through `Devices` (web/src/live/devices.ts builds the real one out
 * of getUserMedia, MediaRecorder, AudioContext, Audio, fetch and a clock), so every rule below can
 * be run under node with fakes and no microphone.
 */

/** One request to /api/live/say. The server refuses more, because sentences are short. */
export const SAY_LIMIT = 600
/** The first sentence is cut here if no full stop has arrived yet, so speech starts fast. */
export const FIRST_CUT = 60
/** How often the loudness is read, in milliseconds. */
export const SAMPLE_MS = 50
/** Milliseconds of sound in one recorder piece. */
export const TIMESLICE = 250

const MIN_PIECE = 4 // "A." is an initial, not a sentence: keep looking for the real end
const CALIBRATE_MS = 1000 // the first second of a session is the room, not the person
const START_MS = 150 // loud for this long and the person has started talking
const END_MS = 900 // quiet for this long and their turn is over
const MIN_TURN_MS = 400 // shorter than this was a cough
const MAX_TURN_MS = 20000 // a room that never goes quiet must not hold one turn open for ever
const MIN_FLOOR = 0.012 // however quiet the room is, silence never counts as speech
const MAX_FLOOR = 0.08 // and however loud the first second was, an ordinary voice is always heard
const ROOM_KEEP = 120 // readings kept to measure the room again: about six seconds of them
const ROOM_EVERY = 20 // the bar is worked out again this often, about once a second
const BARGE_BAR = 2 // while it is speaking, the person has to be this much louder to cut in
const STEP_STALE_MS = 5000 // a step title older than this is news nobody needs
const TITLE_WORDS = 8 // a step title is read out as its first clause, never a whole line of it
const PRE_ROLL = 2 // pieces of sound kept from just before speech was noticed
const MIN_CLIP = 512 // bytes: anything smaller than this is silence, and is never sent
const MAX_CLIP = 6 * 1024 * 1024 // and one turn never fills the tab's memory either
const ANSWER_WAIT_MS = 3000 // nothing came back from the chat after a question: say so
const LEVEL_STEP = 0.06 // the input meter is redrawn only when it has really moved
const LEVEL_MS = 200 // and at most five times a second, because it redraws the whole panel

const LISTEN_URL = '/api/live/listen'
const SAY_URL = '/api/live/say'

const NO_SERVICE = 'We could not reach the voice service from this laptop.'
const NO_WORDS = 'We could not turn that into words.'
const NO_SPEECH = 'That sentence could not be read out loud.'
const NO_MIC = 'This browser would not give us the microphone. Check the microphone permission for this page.'
const MUTED_NOTE = 'The microphone is off. Press Unmute to talk again.'
const HEARD_NOTE = 'Heard you. It answers the last question first.'
const NOT_TAKEN = 'That question did not reach the assistant. Please say it again.'
const SEND_FAILED = 'That question could not be sent to the assistant.'
const TOO_LONG = 'That was too long to hear in one go. Please say it again.'

// ------------------------------------------------------------------ plain words
// A person hears commas, full stops, "and" and "to". Nothing else. harness/steps.py holds the same
// rule for everything written on the server, and the server applies it again to whatever we send.
const ARROWS = /[^\S\n]*(?:[←→↔⇒⇨➡]+|-{1,2}>|=>)[^\S\n]*/g
const DASHES = /[^\S\n]*[‒–—―]+[^\S\n]*/g
const DOTS = /[^\S\n]*[·•‣▪・]+[^\S\n]*/g
const SEMICOLONS = /[^\S\n]*;+[^\S\n]*/g

/** Plain punctuation, for anything a person reads or hears. Never raises. */
export function plain(text: string): string {
  if (typeof text !== 'string') return ''
  return text.replace(ARROWS, ' to ').replace(DASHES, ', ').replace(DOTS, ', ').replace(SEMICOLONS, '. ')
}

/**
 * The answer with everything nobody wants read out loud taken off it: web addresses, post
 * addresses, long ids, an id in brackets, code spans, tags, and the stars and backticks that are
 * markup on the screen and noise in the ear. The first pass matters most: a post can carry a right
 * to left override or a zero width space, which are invisible but reorder the line a person reads.
 */
export function speakable(text: string): string {
  if (typeof text !== 'string') return ''
  return plain(
    text
      .replace(
        // oxlint-disable-next-line no-control-regex -- Strip invisible control characters before speech.
        /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g,
        '',
      )
      .replace(/<\/?[A-Za-z][^<>]{0,200}>/g, ' ')
      .replace(/`[^`\n]{0,200}`/g, ' ')
      .replace(/\b(?:https?:\/\/|www\.)\S+/gi, ' ')
      .replace(/\bat:\/\/\S+/gi, ' ')
      // A slash between two letters is a slash a listener hears: X/Twitter is two words, not one.
      // Web addresses are already gone by here, and a date or a fraction has digits, so both survive.
      .replace(/(?<=[A-Za-z])\/(?=[A-Za-z])/g, ' ')
      // brackets holding one unbroken run with a digit, a colon or a slash in it: an id, never a word
      .replace(/[([]([A-Za-z0-9_:/.+-]{8,})[)\]]/g, (whole, inside: string) => (/[0-9_:/]/.test(inside) ? ' ' : whole))
      .replace(/\b(?:[0-9a-f]{16,}|\d{12,})\b/gi, ' ')
      .replace(/^[ \t]*[-*][ \t]+/gm, '')
      .replace(/[*_`#><]+/g, '')
      .replace(/[([]\s*[,.]?\s*[)\]]/g, ' ')
      .replace(/\s+/g, ' '),
  ).trim()
}

// ------------------------------------------------------------- cutting sentences
// Words that end in a full stop and do not end a sentence. Dates matter most here, because the
// assistant is told to say Sep 9 and a listener must not hear that as two sentences.
const ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'st', 'jr', 'sr', 'vs', 'etc', 'eg', 'ie', 'approx', 'no', 'fig',
  'inc', 'ltd', 'co', 'corp', 'dept', 'est', 'al', 'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug',
  'sep', 'sept', 'oct', 'nov', 'dec', 'mon', 'tue', 'tues', 'wed', 'thu', 'thur', 'thurs', 'fri',
  'sat', 'sun', 'am', 'pm',
])

/** Does the full stop at `at` really end a sentence, or is it inside a name, a date or an initial? */
function ends(text: string, at: number): boolean {
  const before = text.slice(0, at)
  if (/(?:^|[^A-Za-z])[A-Za-z]$/.test(before)) return false // an initial: "A." and the S of "U.S."
  const word = /([A-Za-z]+)$/.exec(before)
  return !word || !ABBREVIATIONS.has(word[1].toLowerCase())
}

/**
 * Where the sentence starting at `from` ends, or -1 while more text may still arrive. A full stop
 * only ends a sentence when whitespace follows it, so "3.5" and "example.com" are left alone, and a
 * stop at the very end of what we have so far waits: "1." may still become "1.5 million".
 */
function endOf(text: string, from = 0): number {
  for (let at = from; at < text.length; at += 1) {
    const mark = text[at]
    if (mark === '\n') return at + 1
    if (mark !== '.' && mark !== '!' && mark !== '?') continue
    let end = at + 1
    while (end < text.length && '"\')]’”'.includes(text[end])) end += 1
    if (end >= text.length) return -1
    if (!/\s/.test(text[end])) continue
    if (mark === '.' && !ends(text, at)) continue
    return end
  }
  return -1
}

/**
 * Is something still open at `at`: a quotation that has not been closed, or a bracket? The early
 * cut must never land in the middle of a quoted name, because the listener would hear the name in
 * two pieces with a dangling quote between them. A curly closing mark on its own is an apostrophe,
 * so only an unclosed opening one counts.
 */
function dangling(text: string): boolean {
  const count = (mark: RegExp) => (text.match(mark) || []).length
  return count(/"/g) % 2 === 1 || count(/[“‘]/g) > count(/[”’]/g) || count(/[([]/g) > count(/[)\]]/g)
}

export interface Cutter {
  /** Every sentence that has become whole, in order. */
  feed(text: string): string[]
  /** Whatever is left when the answer ends. */
  flush(): string[]
  /**
   * The same, for a hard boundary in the middle of an answer: a tool call. Whatever has been
   * written so far is said now, while the wait is still ahead of the person, instead of sitting in
   * the buffer until the tool comes back and being welded onto the first words of what follows.
   */
  close(): string[]
}

/**
 * The answer arrives a few characters at a time. feed() gives back every sentence that has become
 * whole; the very first one is cut early at about `firstCut` characters if no full stop has come
 * yet, because the wait before the first word is the whole difference between live and not.
 */
export function createCutter(options: { firstCut?: number; limit?: number } = {}): Cutter {
  const first = Math.max(8, Number(options.firstCut) || FIRST_CUT)
  const limit = Math.max(first, Number(options.limit) || SAY_LIMIT)
  let buffer = ''
  let opened = false // the first sentence is out, so the early cut is done with

  const take = (at: number) => {
    const piece = buffer.slice(0, at).trim()
    buffer = buffer.slice(at).replace(/^\s+/, '')
    opened = true
    return piece
  }
  const wordCut = (width: number) => {
    const space = buffer.lastIndexOf(' ', width)
    return space > width / 2 ? space : width
  }
  // Where the first sentence may be cut early, or -1 for "not yet": a quotation or a bracket that is
  // still open is carried on to the next space that closes it, and if nothing closes it inside the
  // limit the cut waits for more text rather than breaking a name in half.
  const earlyCut = (width: number) => {
    const at = wordCut(width)
    if (!dangling(buffer.slice(0, at))) return at
    for (let next = at + 1; next <= limit && next < buffer.length; next += 1) {
      if (buffer[next] === ' ' && !dangling(buffer.slice(0, next))) return next
    }
    return -1
  }
  const nextEnd = () => {
    for (let at = 0; ;) {
      const end = endOf(buffer, at)
      if (end < 0) return -1
      if (end >= MIN_PIECE) return end
      at = end
    }
  }

  const drain = () => {
    const out: string[] = []
    for (;;) {
      const end = nextEnd()
      if (end > 0 && end <= limit) {
        out.push(take(end))
        continue
      }
      if (!opened && buffer.length >= first) {
        // A name is still open: better a little later than cut in half. If nothing ever closes it,
        // the limit below cuts it in the end, because speech cannot wait for ever.
        const early = earlyCut(first)
        if (early > 0) {
          out.push(take(early))
          continue
        }
      }
      if (buffer.length > limit) {
        out.push(take(wordCut(limit)))
        continue
      }
      break
    }
    return out
  }
  const rest = () => {
    const out = drain()
    while (buffer.length > limit) out.push(take(wordCut(limit)))
    const last = buffer.trim()
    buffer = ''
    opened = true
    if (last) out.push(last)
    return out.filter(Boolean)
  }

  return {
    feed(text: string) {
      buffer += typeof text === 'string' ? text : ''
      return drain().filter(Boolean)
    },
    flush: rest,
    close: rest,
  }
}

/** Words a clause must never be left hanging on when a title is cut short. */
const TRAILING = new Set(['about', 'of', 'for', 'in', 'on', 'to', 'the', 'a', 'an', 'with', 'from', 'and', 'at'])

/**
 * A step title as it is read out: the first clause of it, and never more than a breath. Titles are
 * written for the eye and can name three keywords and a component nobody in the room has heard of.
 */
export function spokenTitle(text: string): string {
  const clause = String(text || '').split(/[,:("]/)[0].trim() || String(text || '').trim()
  const words = clause.split(/\s+/).filter(Boolean).slice(0, TITLE_WORDS)
  while (words.length > 2 && TRAILING.has(words[words.length - 1].toLowerCase())) words.pop()
  return words.join(' ').slice(0, 120)
}

// ------------------------------------------------------------ hearing a turn
export type Move = '' | 'start' | 'end' | 'drop'

/**
 * Is the person talking? feed(level, at, measure) takes one loudness reading and the clock, and
 * answers "start" when a turn begins, "end" when it is over, "drop" when it was too short to be a
 * question and "" the rest of the time.
 *
 * The bar is the room's own noise. It is measured in the first second and then measured again and
 * again from the quiet between turns, because the first second is often not the room at all: a
 * presenter starts talking as they click, or the hall claps. It is also capped, so whatever that
 * first second held, an ordinary speaking voice is always above it. `measure` is false for a
 * reading that is not the room, such as the answer coming out of the speakers.
 */
export function createDetector(
  options: { startMs?: number; endMs?: number; minMs?: number; calibrateMs?: number; maxMs?: number } = {},
) {
  const startMs = Number(options.startMs) || START_MS
  const endMs = Number(options.endMs) || END_MS
  const minMs = Number(options.minMs) || MIN_TURN_MS
  const calibrateMs = Number(options.calibrateMs) || CALIBRATE_MS
  const maxMs = Number(options.maxMs) || MAX_TURN_MS
  const room: number[] = []
  let floor = 0
  let since = 0 // readings taken since the bar was last worked out
  let began: number | null = null
  let last: number | null = null
  let loud = 0
  let quiet = 0
  let talking = false
  let startedAt = 0

  const barFrom = () => {
    const sorted = room.slice().sort((one, two) => one - two)
    const middle = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0
    return Math.min(MAX_FLOOR, Math.max(MIN_FLOOR, middle * 3 + 0.004))
  }

  return (level: number, at: number, measure = true): Move => {
    const now = Number(at)
    const value = Number(level)
    if (!Number.isFinite(now) || !Number.isFinite(value)) return ''
    if (began === null) began = now
    const step = last === null ? 0 : Math.max(0, Math.min(400, now - last)) // a stalled tab is not speech
    last = now
    if (now - began < calibrateMs) {
      room.push(Math.max(0, value))
      return ''
    }
    if (!floor) floor = barFrom()
    if (!talking && measure) {
      room.push(Math.max(0, value))
      if (room.length > ROOM_KEEP) room.splice(0, room.length - ROOM_KEEP)
      since += 1
      if (since >= ROOM_EVERY) {
        since = 0
        floor = barFrom() // a bad first second is forgotten within a few seconds of quiet
      }
    }
    if (value >= floor) {
      quiet = 0
      loud += step
      if (!talking && loud >= startMs) {
        talking = true
        startedAt = now - loud
        return 'start'
      }
      if (talking && now - startedAt >= maxMs) {
        // Twenty seconds with no quiet in them is a room, not a question. The turn is closed so the
        // recording cannot grow without end, and because we plainly do not understand this room any
        // more, the next second is spent measuring it again, with nothing counting as speech.
        talking = false
        loud = 0
        quiet = 0
        began = now
        room.length = 0
        since = 0
        floor = 0
        return 'end'
      }
    } else if (talking) {
      quiet += step
      if (quiet >= endMs) {
        const spoke = now - quiet - startedAt
        talking = false
        loud = 0
        quiet = 0
        return spoke >= minMs ? 'end' : 'drop'
      }
    } else {
      loud = 0
    }
    return ''
  }
}

// ----------------------------------------------------------------- the devices
/** One open microphone. Closing it stops the recorder, drops the tracks and closes the sound context. */
export interface Mic {
  /** The sound format the recorder makes, sent as the content type of a turn. */
  mime: string
  /** How loud the room is right now, 0 to about 1. */
  level(): number
  /** Called with every piece of sound the recorder finishes. */
  onPiece(fn: (piece: Blob) => void): void
  close(): void
}

/** One clip being played. `done` resolves when it has finished or been stopped, and never rejects. */
export interface Playing {
  stop(): void
  done: Promise<void>
}

export interface Devices {
  /** Opens the microphone. Rejects when the person says no, or the browser cannot. */
  open(): Promise<Mic>
  play(clip: Blob): Playing
  fetch(url: string, init?: RequestInit): Promise<Response>
  now(): number
  /** Calls `tick` every `ms`, and gives back the way to stop it. */
  every(ms: number, tick: () => void): () => void
}

// ------------------------------------------------------------------ the session
/** What the strip shows, in words a person reads. */
export type LiveState = 'Listening' | 'Heard you' | 'Thinking' | 'Speaking' | 'Muted' | 'Ended'

export interface LiveView {
  state: LiveState
  /** True between a successful start and the end of the session. */
  active: boolean
  /** The browser is still asking the person for the microphone: the click has been taken. */
  opening: boolean
  muted: boolean
  /** 0 to 1, for the input level meter. */
  level: number
  /** One plain sentence about something that went wrong, or ''. */
  note: string
}

/** What the session is told about the assistant's turn. See web/src/live/feed.ts. */
export interface LiveEvent {
  type: 'turn_start' | 'text' | 'step' | 'turn_end' | 'error'
  /** 'text': a piece of the answer. 'error': one plain sentence. */
  text?: string
  /** 'step': what the assistant is doing, in plain words. */
  title?: string
  /** 'step': only a start is spoken. */
  phase?: 'start' | 'end'
}

export interface SessionOptions {
  devices: Devices
  readAloud?: boolean
  /**
   * Submits a question exactly as typing it in the Ask box would. Answering false says the chat
   * would not take it, and the person is told so at once instead of waiting in silence.
   */
  send(text: string): void | boolean
  /**
   * Stops the answer in progress, if the chat can. It is called only when the person talks over an
   * answer and really does ask something, so their question is taken now rather than after the
   * answer they interrupted has finished streaming. Answering false says there was nothing to stop,
   * and the question waits for the end of that answer instead, as it did before.
   */
  stop?(): void | boolean
  onView?(view: LiveView): void
}

export interface Session {
  /** Opens the microphone and starts listening. Answers false when the browser said no. */
  start(): Promise<boolean>
  /** Ends the session and hands the microphone back. Safe to call twice. */
  stop(reason?: string): void
  setMuted(muted: boolean): void
  setReadAloud(enabled: boolean): void
  /** One event of the assistant's turn. */
  handle(event: LiveEvent): void
  watch(fn: (view: LiveView) => void): () => void
  view(): LiveView
}

interface Said {
  clip?: Blob
  problem?: string
}

export function createSession(options: SessionOptions): Session {
  const devices = options.devices
  const watchers = new Set<(view: LiveView) => void>()

  let state: LiveState = 'Ended'
  let active = false
  let opening = false // the browser is asking the person for the microphone
  // Bumped by every start and every stop. Anything slow that comes back holding an old number
  // belongs to a talk that is over, and is thrown away instead of bringing it back to life.
  let generation = 0
  let muted = false
  let readAloud = options.readAloud ?? true
  let level = 0
  let note = ''
  let shown: LiveView | null = null
  let paintedAt = 0

  let mic: Mic | null = null
  let ticker: (() => void) | null = null
  let detect = createDetector()
  let pieces: Blob[] = []
  let header: Blob | null = null
  let turnAt = -1
  let turnBytes = 0

  let running = false // the assistant is answering
  let seenTurn = false // a turn has begun since this talk did: before that, the answer is not ours
  let interrupted = false // the person talked over this answer: the rest of it is not read out
  let hearing = false // the person is speaking right now: nothing at all is read over them
  let held: string[] = [] // the answer written while they were speaking, spoken only if it was a cough
  let cutIn = false // something was being said when this turn of theirs began
  let cutOff = false // we stopped the assistant ourselves, so its error is not news
  let cutter: Cutter | null = null
  let waiting = '' // a question heard while the last one was still being answered
  let askedAt = 0

  const talk = {
    token: 0, // bumped on every interruption, so an answer already on its way is thrown away
    queue: [] as string[],
    step: null as { text: string; at: number; of: string } | null,
    next: null as Promise<Said> | null,
    audio: null as Playing | null,
    busy: false,
    aborts: new Set<AbortController>(),
  }

  const view = (): LiveView => ({ state, active, opening, muted, level, note })

  /**
   * Tells the screen, but only when there is something to tell. Every reading of the input meter
   * passes through here and each one redraws the whole Ask panel, so the meter alone has to move a
   * real amount, and never more than five times a second. Words on the strip are never held back.
   */
  const paint = (force = false) => {
    const now = view()
    const words = !shown || shown.state !== now.state || shown.active !== now.active
      || shown.opening !== now.opening || shown.muted !== now.muted || shown.note !== now.note
    const moved = !shown || Math.abs(shown.level - now.level) >= LEVEL_STEP
    const at = devices.now()
    if (!force && !words && !(moved && at - paintedAt >= LEVEL_MS)) return
    shown = now
    paintedAt = at
    options.onView?.(now)
    for (const watcher of [...watchers]) watcher(now)
  }

  const setState = (words: LiveState) => {
    state = words
    paint()
  }

  const say = (words: string) => {
    note = plain(String(words || '')).slice(0, 200)
    paint()
  }

  const resting = (): LiveState => (muted ? 'Muted' : running ? 'Thinking' : 'Listening')

  // ------------------------------------------------------------------ speaking
  const problemOf = async (response: Response): Promise<string> => {
    try {
      const body = (await response.json()) as { error?: { message?: string } }
      const message = body?.error?.message
      if (typeof message === 'string' && message.trim()) return message
    } catch {
      /* not JSON: the plain sentence below says enough */
    }
    return NO_SPEECH
  }

  /**
   * One sentence of sound. The request is only let go of once the last byte of the clip is in: an
   * mp3 spends nearly all of its life arriving, so a controller dropped at the headers would leave
   * the service reading out a sentence nobody will hear, and being paid for it. Held to the end,
   * the interruption reaches the server, which stops reading from ElevenLabs the moment we hang up.
   */
  const fetchSay = async (text: string, mine: number): Promise<Said> => {
    const controller = new AbortController()
    talk.aborts.add(controller)
    try {
      const response = await devices.fetch(SAY_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      })
      if (mine !== talk.token) return {}
      if (!response.ok) return { problem: await problemOf(response) }
      const clip = await response.blob()
      return mine === talk.token && clip ? { clip } : {}
    } catch {
      // An abort is the person talking over it, and is never a failure worth a sentence on screen.
      return mine === talk.token ? { problem: NO_SERVICE } : {}
    } finally {
      talk.aborts.delete(controller)
    }
  }

  const playClip = async (clip: Blob, mine: number) => {
    if (mine !== talk.token) return
    const playing = devices.play(clip)
    talk.audio = playing
    try {
      await playing.done
    } catch {
      /* a player that would not start is silence, never a broken session */
    } finally {
      if (talk.audio === playing) talk.audio = null
    }
  }

  const pump = async () => {
    if (talk.busy) return
    talk.busy = true
    const mine = talk.token
    try {
      while (mine === talk.token && active) {
        let text = ''
        if (talk.queue.length) text = talk.queue.shift() as string
        else if (talk.step && devices.now() - talk.step.at <= STEP_STALE_MS) {
          text = talk.step.text
          talk.step = null
        } else {
          talk.step = null // older than five seconds: the step it was about is long finished
          break
        }
        setState('Speaking')
        const ready = talk.next ?? fetchSay(text, mine)
        talk.next = talk.queue.length ? fetchSay(talk.queue[0], mine) : null // one ahead, never more
        const { clip, problem } = await ready
        if (problem) {
          say(problem)
          talk.queue = []
          talk.next = null
          break
        }
        if (clip) await playClip(clip, mine)
      }
    } finally {
      talk.busy = false
      if (mine === talk.token && active) setState(resting())
    }
    if (active && (talk.queue.length || talk.step)) void pump() // work that arrived while this finished
  }

  const stopSpeaking = () => {
    talk.token += 1
    talk.queue = []
    talk.step = null
    talk.next = null
    for (const controller of talk.aborts) {
      try {
        controller.abort()
      } catch {
        /* already done */
      }
    }
    talk.aborts.clear()
    if (talk.audio) {
      try {
        talk.audio.stop()
      } catch {
        /* already gone */
      }
    }
    talk.audio = null
  }

  const enqueue = (sentences: string[]) => {
    if (!readAloud) return
    let queued = false
    for (const sentence of sentences) {
      const words = speakable(sentence)
      if (!words) continue
      // While the person is speaking, nothing is read over them. What the assistant writes in those
      // seconds waits here: if it turns out to have been a cough it is read out after all, and if
      // it was really a question it is read on the screen only.
      if (hearing) held.push(words.slice(0, SAY_LIMIT))
      else talk.queue.push(words.slice(0, SAY_LIMIT))
      queued = true
    }
    // The answer itself has arrived, so the step title that was covering the wait is no longer
    // news: it must never be read out after the thing it was about.
    if (queued) talk.step = null
    if (talk.queue.length) void pump()
  }

  // ----------------------------------------------------------------- listening
  const trim = () => {
    pieces = header ? [header] : []
    turnAt = -1
    turnBytes = 0
  }

  const beginTurn = () => {
    // Barge in: whatever is playing stops at once and the queue goes with it, because talking over
    // the answer has to work on any loud noise. Whether there was really a question in it is only
    // known when the turn ends, so nothing else is thrown away here: a cough must not cost the
    // person the rest of their answer.
    cutIn = running || talk.busy
    hearing = true
    held = []
    stopSpeaking()
    turnAt = Math.max(0, pieces.length - PRE_ROLL)
    turnBytes = 0
    for (let at = turnAt; at < pieces.length; at += 1) turnBytes += pieces[at].size
    setState('Listening')
  }

  // Only the sound of this turn is sent. The first piece the recorder ever made carries the file's
  // header, so it goes in front of every turn but the first, or there is nothing to decode.
  const turnClip = (): Blob | null => {
    if (turnAt < 0) return null
    const parts = pieces.slice(turnAt)
    const whole = parts[0] === header ? parts : [header, ...parts]
    return new Blob(whole.filter(Boolean) as Blob[], { type: mic?.mime || 'audio/webm' })
  }

  const ask = (text: string) => {
    if (!active) return // the talk ended while these words were on their way back
    if (running) {
      // A message sent mid turn is dropped by the chat, so it waits here instead of vanishing.
      waiting = text
      if (!cutOff) say(HEARD_NOTE)
      return
    }
    askedAt = devices.now()
    setState('Heard you')
    let taken: void | boolean
    try {
      taken = options.send(text)
    } catch {
      say(SEND_FAILED)
      setState(resting())
      return
    }
    if (taken === false) {
      askedAt = 0
      say(NOT_TAKEN)
      setState(resting())
    }
  }

  const endTurn = async (keep: boolean) => {
    const mine = generation
    const clip = turnClip()
    trim()
    hearing = false
    if (!keep || muted || !clip || clip.size < MIN_CLIP) {
      // A cough is not a question, so the answer it stopped goes on being read out from where the
      // assistant has got to.
      cutIn = false
      const waited = held
      held = []
      if (!muted && waited.length) enqueue(waited)
      return // never send silence
    }
    held = []
    if (cutIn) {
      // There really was a question in it. From here the rest of that answer is read on the screen
      // only, and if the chat can be stopped it is stopped now, so this question is taken at once
      // instead of waiting for an answer nobody is listening to any more.
      cutIn = false
      interrupted = true
      if (running && options.stop) {
        try {
          cutOff = options.stop() !== false
        } catch {
          cutOff = false // the chat could not be stopped: their question waits for the end of it
        }
      }
    }
    setState('Heard you')
    let response: Response
    try {
      response = await devices.fetch(LISTEN_URL, {
        method: 'POST',
        headers: { 'Content-Type': clip.type || 'audio/webm' },
        body: clip,
      })
    } catch {
      if (mine === generation && active) stop('We lost the voice service, so the live talk has ended.')
      return
    }
    if (mine !== generation || !active) return // ended while the words were being made
    if (!response.ok) {
      let problem = NO_WORDS
      try {
        const body = (await response.json()) as { error?: { message?: string } }
        if (typeof body?.error?.message === 'string') problem = body.error.message
      } catch {
        /* not JSON */
      }
      if (mine !== generation || !active) return
      say(problem)
      setState(resting())
      return
    }
    const body = (await response.json().catch(() => null)) as { text?: string } | null
    if (mine !== generation || !active) return
    const text = typeof body?.text === 'string' ? plain(body.text).trim() : ''
    if (!text) {
      setState(resting())
      return
    }
    ask(text)
  }

  const sample = () => {
    if (!active || !mic) return
    const raw = muted ? 0 : mic.level()
    level = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0
    paint()
    // While it is talking, the microphone also hears the answer coming out of the speakers, so the
    // person has to be clearly louder than that before we treat it as an interruption.
    // The room is only measured while nothing is coming out of the speakers and the microphone is
    // on, so neither the answer nor a muted stretch of zeroes can move the bar for speech.
    const move = detect(talk.busy ? level / BARGE_BAR : level, devices.now(), !talk.busy && !muted)
    if (move === 'start') beginTurn()
    else if (move === 'end' || move === 'drop') void endTurn(move === 'end')
    // The chat can refuse a question, for instance while it is still answering the last one. The
    // person heard nothing happen, so they are told, rather than left waiting on a silent room.
    if (state === 'Heard you' && !running && !talk.busy && askedAt && devices.now() - askedAt > ANSWER_WAIT_MS) {
      askedAt = 0
      say(NOT_TAKEN)
      setState(resting())
    }
  }

  // ------------------------------------------------------------ start and stop
  const stop = (reason?: string) => {
    const was = active
    generation += 1 // a microphone still on its way is no longer wanted
    opening = false
    active = false
    stopSpeaking()
    if (ticker) {
      ticker()
      ticker = null
    }
    if (mic) {
      try {
        mic.close()
      } catch {
        /* already closed */
      }
    }
    mic = null
    pieces = []
    header = null
    turnAt = -1
    turnBytes = 0
    level = 0
    running = false
    seenTurn = false
    interrupted = false
    hearing = false
    held = []
    cutIn = false
    cutOff = false
    cutter = null
    waiting = ''
    askedAt = 0
    if (was || reason) {
      state = 'Ended'
      // The reason for this ending, or nothing at all: a sentence from the talk that just ended
      // must not sit under the input row of a chat with no talk in it.
      note = reason ? plain(reason).slice(0, 200) : ''
    }
    paint(true)
  }

  const start = async (): Promise<boolean> => {
    if (active || opening) return true // one microphone per talk, however often the button is pressed
    opening = true
    const mine = ++generation
    paint()
    let opened: Mic
    try {
      opened = await devices.open()
    } catch {
      opening = false
      if (mine !== generation) return false // it was ended while the browser was asking
      stop(NO_MIC)
      return false
    }
    opening = false
    if (mine !== generation) {
      // Ended, or started again, while the browser was still asking. This microphone belongs to a
      // talk that is over: it is handed straight back rather than left open with nothing holding it.
      try {
        opened.close()
      } catch {
        /* already closed */
      }
      return false
    }
    mic = opened
    active = true
    muted = false
    note = ''
    level = 0
    running = false
    seenTurn = false
    interrupted = false
    hearing = false
    held = []
    cutIn = false
    cutOff = false
    waiting = ''
    askedAt = 0
    detect = createDetector()
    pieces = []
    header = null
    turnAt = -1
    turnBytes = 0
    opened.onPiece((piece: Blob) => {
      if (!piece || !piece.size) return
      if (!header) header = piece
      pieces.push(piece)
      // Idle: keep the header and a little pre-roll, and let the rest go, so a session left open
      // for an hour never grows.
      if (turnAt < 0 && pieces.length > PRE_ROLL + 1) {
        pieces.splice(1, 1)
        return
      }
      if (turnAt < 0) return
      turnBytes += piece.size
      if (turnBytes > MAX_CLIP) {
        // Whatever is going on in that room, it is not one question. The turn is let go of before
        // it can fill the tab, and the person is told in one sentence.
        trim()
        say(TOO_LONG)
      }
    })
    ticker = devices.every(SAMPLE_MS, () => {
      try {
        sample()
      } catch {
        /* one bad reading must never end the session */
      }
    })
    setState('Listening')
    paint(true)
    return true
  }

  return {
    start,
    stop,
    setMuted(next: boolean) {
      if (!active || muted === next) return
      muted = next
      if (muted) {
        turnAt = -1
        trim()
        hearing = false // whatever they were saying is dropped, so the voice is not held back by it
        held = []
        stopSpeaking()
        note = MUTED_NOTE
      } else if (note === MUTED_NOTE) note = ''
      setState(muted ? 'Muted' : resting())
    },
    setReadAloud(enabled: boolean) {
      readAloud = enabled
      if (!enabled) {
        stopSpeaking()
        cutter = null
        if (active) setState(resting())
      }
    },
    handle(event: LiveEvent) {
      if (!active || !event || typeof event !== 'object') return
      if (event.type === 'turn_start') {
        running = true
        seenTurn = true
        interrupted = false
        cutIn = false
        askedAt = 0
        cutter = createCutter()
        talk.step = null
        setState('Thinking')
        return
      }
      // A talk started in the middle of a typed answer hears the tail of it and would read that
      // tail out from the middle of a sentence. Nothing is read out until a turn begins with us.
      if (!seenTurn) return
      if (event.type === 'turn_end') {
        running = false
        cutOff = false
        if (cutter && !interrupted && !cutIn) enqueue(cutter.flush())
        cutter = null
        talk.step = null // the answer is over: a title from it is not read out after the fact
        if (!talk.busy) setState(resting())
        const asked = waiting
        waiting = ''
        if (asked) ask(asked) // the question they interrupted with, now that the answer is over
        return
      }
      if (event.type === 'error') {
        stopSpeaking()
        running = false
        cutter = null // half a sentence from an answer that failed is not read out after the failure
        const text = typeof event.text === 'string' ? event.text : ''
        // The one error we asked for, by stopping the answer ourselves, is not worth a sentence.
        if (text && !cutOff && !/abort/i.test(text)) say(text)
        cutOff = false
        setState(resting())
        return
      }
      if (interrupted) return // the rest of an answer they talked over is read on the screen only
      if (event.type === 'text' && typeof event.text === 'string') {
        if (!cutter) cutter = createCutter()
        enqueue(cutter.feed(event.text))
      } else if (event.type === 'step') {
        if (event.phase === 'end') {
          // That step has finished, so its title is not news any more. A title still waiting for a
          // different step is left where it is.
          const of = String(event.title || '')
          if (talk.step && (!of || talk.step.of === of)) talk.step = null
          return
        }
        // A tool call is a hard end of sentence. What was written before it, such as the sentence
        // that says this will take about half a minute, is said now, while the wait is still ahead.
        if (cutter) enqueue(cutter.close())
        const title = spokenTitle(speakable(String(event.title || '')))
        // One title waits at a time, and it is only ever read out when the queue has run dry, which
        // is when the wait it describes is really being heard. It is dropped when the answer catches
        // up with it, when that step ends, when the turn ends, and when it goes stale.
        if (readAloud && title && !hearing) {
          talk.step = { text: title, at: devices.now(), of: String(event.title || '') }
          void pump()
        }
      }
    },
    watch(fn: (next: LiveView) => void) {
      watchers.add(fn)
      return () => watchers.delete(fn)
    },
    view,
  }
}
