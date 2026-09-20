/**
 * Run: npx -y tsx web/tests/live/review/lifecycle.test.ts
 *
 * The edges of a session, where the browser is slow and the person is not. Opening the microphone
 * is not instant, so every test here holds it open for exactly as long as a real permission prompt
 * does, and then does the ordinary thing a presenter does: presses the button again, changes their
 * mind, or ends the talk while the last thing they said is still on its way to the server.
 *
 * All of these once left a microphone open with nothing on screen able to close it, which Chrome
 * shows as a red dot on the tab for the rest of the page's life.
 */
import { createSession, type Session } from '../../../src/live/core'
import { check, report, same, settle } from '../check'
import { ReviewDevices, room, sound } from './fakes'

interface Talk {
  devices: ReviewDevices
  session: Session
  sent: string[]
}

function make(): Talk {
  const devices = new ReviewDevices()
  const sent: string[] = []
  const session = createSession({ devices, send: (text: string) => sent.push(text) })
  return { devices, session, sent }
}

/** Two presses of the button while the browser is still asking for the microphone. */
async function twoClicks() {
  const { devices, session } = make()
  devices.holdOpen = true
  const first = session.start()
  check('1a the button says the press was taken while the browser asks for the microphone',
    session.view().opening === true && session.view().active === false,
    `opening ${session.view().opening}, active ${session.view().active}`)

  const second = session.start()
  devices.waitingOpens.forEach((open) => open())
  await first
  await second
  await settle()
  check('1b a second press opens no second microphone', devices.opens === 1, `${devices.opens} opened`)
  check('1c and the talk is on, with one microphone in hand',
    session.view().active === true && session.view().opening === false && devices.mics.length === 1,
    `${devices.mics.length} microphones, state ${session.view().state}`)

  await room(devices)
  session.stop()
  check('1d End hands that one back and stops the sampler',
    devices.mics[0].closed === 1 && devices.clock.running === 0,
    `closed ${devices.mics[0].closed} times, ${devices.clock.running} tickers still running`)
}

/** One microphone, so one recording: a turn is never two sound files spliced together. */
async function oneStream() {
  const { devices, session } = make()
  devices.holdOpen = true
  void session.start()
  void session.start()
  devices.waitingOpens.forEach((open) => open())
  await settle()
  await room(devices, 0.002)
  await sound(devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
  const clip = devices.listens[0]
  check('2a the turn is sent once, fed by the one microphone the talk holds',
    devices.listens.length === 1 && devices.opens === 1 && clip.body.size > 512,
    `${devices.listens.length} turns from ${devices.opens} microphone, ${clip?.body.size} bytes`)
  session.stop()
  check('2b and nothing is left running afterwards',
    devices.mics.every((mic) => mic.closed === 1) && devices.clock.running === 0,
    `${devices.mics.filter((mic) => mic.closed === 0).length} left open`)
}

/** End, or the panel closing, while the browser is still asking for the microphone. */
async function endedWhileAsking() {
  const { devices, session } = make()
  devices.holdOpen = true
  const starting = session.start()
  session.stop() // the presenter changed their mind, or React unmounted the sidebar
  check('3a the session says it has ended', session.view().active === false && session.view().state === 'Ended',
    session.view().state)

  devices.waitingOpens[0]()
  const started = await starting
  await settle()
  check('3b the microphone that arrives late does not bring the session back',
    started === false && session.view().active === false && session.view().opening === false,
    `started ${started}, active ${session.view().active}, state ${session.view().state}`)
  check('3c it is handed straight back, and nothing is left sampling',
    devices.mics[0].closed === 1 && devices.clock.running === 0,
    `closed ${devices.mics[0].closed} times, ${devices.clock.running} tickers running`)

  await room(devices)
  await sound(devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
  check('3d and nothing is heard or sent after the talk was ended',
    devices.listens.length === 0, `${devices.listens.length} turns sent after End`)

  // And the ordinary thing afterwards still works: the button starts a fresh talk.
  devices.holdOpen = false
  const again = await session.start()
  check('3e a talk started after that one works as usual',
    again === true && session.view().active === true && devices.opens === 2, `${devices.opens} microphones in all`)
  session.stop()
}

/** End while the last thing the person said is still being turned into words. */
async function endedWhileListening() {
  const { devices, session, sent } = make()
  devices.holdListen = true
  await session.start()
  await room(devices)
  await sound(devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
  check('4a the turn is on its way to the server', devices.listens.length === 1, `${devices.listens.length}`)

  session.stop()
  devices.listens[0].answer('what are people saying in the last hour')
  await settle()
  same('4b words that come back after End are not asked as a question', sent, [])
  check('4c and the strip stays ended, with nothing under the input row',
    session.view().state === 'Ended' && session.view().active === false && session.view().note === '',
    `${session.view().state}, note ${JSON.stringify(session.view().note)}`)

  const late = make()
  late.devices.holdListen = true
  await late.session.start()
  await room(late.devices)
  await sound(late.devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
  late.session.stop()
  late.devices.listens[0].break()
  await settle()
  check('4d a connection lost after End writes nothing under the input row of an ended talk',
    late.session.view().note === '', JSON.stringify(late.session.view().note))
}

/** A note belongs to the talk it was said in. */
async function notesEndWithTheTalk() {
  const { devices, session } = make()
  await session.start()
  await room(devices)
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'error', text: 'The assistant could not answer.' })
  await settle()
  check('5a a failed turn is said in plain words while the talk is on',
    session.view().note === 'The assistant could not answer.' && session.view().active, session.view().note)
  session.stop()
  check('5b and that sentence goes when the talk goes', session.view().note === '',
    JSON.stringify(session.view().note))

  // The reason a talk ended is the one sentence that does belong under the input row afterwards.
  const lost = make()
  await lost.session.start()
  await room(lost.devices)
  lost.session.stop('We lost the voice service, so the live talk has ended.')
  check('5c while the reason this one ended is still there to read',
    lost.session.view().note.startsWith('We lost the voice service'), JSON.stringify(lost.session.view().note))
}

async function main() {
  await twoClicks()
  await oneStream()
  await endedWhileAsking()
  await endedWhileListening()
  await notesEndWithTheTalk()
  process.exit(report('Talk live review: lifecycle'))
}

void main()
