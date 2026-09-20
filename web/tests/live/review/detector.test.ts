/**
 * Run: npx -y tsx web/tests/live/review/detector.test.ts
 *
 * The ears, in a room that is not a laboratory. The bar for "someone is talking" used to be set
 * once, from the first second of the session, with nothing capping it: a presenter who clicked the
 * button and started talking set the bar to three times their own voice and the page was deaf for
 * the rest of the talk, with the strip still saying Listening.
 *
 * So the bar is capped at a level no ordinary speaking voice can hide under, and it is measured
 * again and again from the quiet between turns. A room that gets louder later, which is what a hall
 * full of people does, is handled by that and by a cap on how long one turn may last.
 */
import { createDetector, createSession, type Session } from '../../../src/live/core'
import { check, report, settle } from '../check'
import { ReviewDevices, room, sound } from './fakes'

interface Talk {
  devices: ReviewDevices
  session: Session
  sent: string[]
}

async function open(calibrationLevel = 0.002): Promise<Talk> {
  const devices = new ReviewDevices()
  const sent: string[] = []
  const session = createSession({ devices, send: (text: string) => sent.push(text) })
  await session.start()
  await room(devices, calibrationLevel)
  return { devices, session, sent }
}

/** A quiet room, so that everything below has something to be compared with. */
async function theWayItIsMeantToGo() {
  const talk = await open(0.002)
  await sound(talk.devices, [[0.30, 1000], [0.002, 1000]])
  await settle()
  check('1a in a quiet room a question at ordinary volume is heard',
    talk.devices.listens.length === 1, `${talk.devices.listens.length} turns sent`)
  talk.session.stop()
}

/** The presenter clicks the button and talks. Nobody waits a second in front of an audience. */
async function talkingThroughTheFirstSecond() {
  const talk = await open(0.20) // the first second is their own voice, not the room
  await sound(talk.devices, [[0.30, 1000], [0.002, 1000]])
  await settle()
  check('2a a question at ordinary volume is still heard after a first second full of talking',
    talk.devices.listens.length === 1, `${talk.devices.listens.length} turns sent`)
  check('2b and it is asked, not swallowed', talk.sent.length === 1, JSON.stringify(talk.sent))
  talk.session.stop()

  // The same thing in numbers, straight from the detector.
  const detect = createDetector()
  let at = 0
  for (; at < 1000; at += 50) detect(0.20, at) // the first second: their own voice
  let heard = ''
  for (; at < 2000; at += 50) heard = heard || detect(0.30, at)
  check('2c the bar can never end up above an ordinary speaking voice, whatever it was measured on',
    heard === 'start', `a voice at 0.30 after a first second at 0.20 reads as ${JSON.stringify(heard)}`)

  // A very loud room is still a room: it is not mistaken for someone talking.
  const loud = createDetector()
  let tick = 0
  const moves: string[] = []
  for (; tick < 1000; tick += 50) loud(0.05, tick)
  for (; tick < 4000; tick += 50) moves.push(loud(0.05, tick))
  check('2d and a steady loud room still makes no turns of its own',
    moves.every((move) => move === ''), moves.filter(Boolean).join(' '))
}

/** The room gets louder after the first second: applause, a crowd, a fan. */
async function aRoomThatGetsLouder() {
  const talk = await open(0.002)
  await sound(talk.devices, [[0.05, 30000]]) // half a minute of crowd noise, nobody talking
  await settle()
  const clips = talk.devices.listens.map((turn) => turn.body.size)
  check('3a the crowd never holds one turn open for ever: the longest is about twenty seconds',
    clips.every((size) => size < 90 * 4096), `${clips.length} turns, longest ${Math.round(Math.max(0, ...clips) / 1024)} kilobytes`)
  check('3b and the room is measured again, so the crowd stops counting as speech',
    clips.length <= 2, `${clips.length} turns sent in thirty seconds of crowd`)

  const before = talk.devices.listens.length
  await sound(talk.devices, [[0.30, 1000]]) // the presenter talks into it
  await sound(talk.devices, [[0.05, 1500]]) // and stops: the crowd is still there
  await settle()
  check('3c a question spoken into a loud room is still heard and sent',
    talk.devices.listens.length === before + 1, `${talk.devices.listens.length - before} turns sent`)
  check('3d the strip never says anything but Listening while it waits',
    ['Listening', 'Heard you'].includes(talk.session.view().state), talk.session.view().state)
  talk.session.stop()
}

/** Nothing may hold one turn open without end. */
async function oneTurnHasAnEnd() {
  const detect = createDetector()
  let at = 0
  for (; at < 1000; at += 50) detect(0.002, at)
  const moves: { move: string; at: number }[] = []
  for (; at < 40000; at += 50) {
    const move = detect(0.5, at) // somebody is holding a note, or the microphone is on the speaker
    if (move) moves.push({ move, at })
  }
  check('4a a turn that never goes quiet is ended after about twenty seconds',
    moves.length >= 2 && moves[0].move === 'start' && moves[1].move === 'end'
    && moves[1].at - moves[0].at <= 21000, JSON.stringify(moves.slice(0, 3)))
}

/** A thinking pause in the middle of a sentence. */
async function aPauseMidSentence() {
  const talk = await open(0.002)
  await sound(talk.devices, [[0.30, 700], [0.002, 850], [0.30, 700], [0.002, 1000]])
  await settle()
  check('5a a pause of under a second keeps one question in one piece',
    talk.devices.listens.length === 1, `${talk.devices.listens.length} turns sent`)
  talk.session.stop()
}

async function main() {
  await theWayItIsMeantToGo()
  await talkingThroughTheFirstSecond()
  await aRoomThatGetsLouder()
  await oneTurnHasAnEnd()
  await aPauseMidSentence()
  process.exit(report('Talk live review: detector'))
}

void main()
