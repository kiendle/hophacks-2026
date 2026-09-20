/**
 * Run: npx -y tsx web/tests/live/review/renders.test.ts
 *
 * How often the strip asks React to draw. Every reading of the input level meter goes through
 * useTalkLive's setView, and that redraws the whole Ask sidebar, every message in it included, so
 * the cost of the meter grows with the conversation. The meter now has to have really moved, and is
 * drawn at most five times a second, while anything with words in it is drawn at once.
 */
import { createSession, type LiveView } from '../../../src/live/core'
import { check, report, settle } from '../check'
import { ReviewDevices, room } from './fakes'

async function howOften() {
  const devices = new ReviewDevices()
  let paints = 0
  const session = createSession({ devices, send: () => {}, onView: () => { paints += 1 } })
  await session.start()
  await room(devices)
  const before = paints

  // Ten seconds of a person talking: a voice is never at one steady loudness.
  const mic = devices.mics[0]
  for (let tick = 0; tick < 200; tick += 1) {
    mic.loud = 0.12 + 0.10 * Math.abs(Math.sin(tick / 3))
    await devices.clock.advance(50)
  }
  const drawn = paints - before
  check('1a the level meter draws the sidebar a handful of times a second, not twenty',
    drawn <= 60, `${drawn} redraws in ten seconds`)
  check('1b and it does still move, so the meter is alive', drawn >= 5, `${drawn} redraws`)
  session.stop()
}

/** Words are never held back: a state or a note reaches the screen on the reading it happened. */
async function wordsAreNeverLate() {
  const devices = new ReviewDevices()
  const seen: LiveView[] = []
  const session = createSession({ devices, send: () => {}, onView: (view) => seen.push({ ...view }) })
  await session.start()
  await room(devices)

  seen.length = 0
  session.handle({ type: 'turn_start' })
  check('2a a new state is on the screen at once', seen.at(-1)?.state === 'Thinking', seen.at(-1)?.state)
  session.handle({ type: 'error', text: 'The assistant could not answer.' })
  await settle()
  check('2b and so is a sentence about something going wrong',
    seen.some((view) => view.note === 'The assistant could not answer.'), `${seen.length} draws`)
  session.stop()
  check('2c and the end of the talk', seen.at(-1)?.active === false && seen.at(-1)?.state === 'Ended',
    JSON.stringify(seen.at(-1)))
}

async function main() {
  await howOften()
  await wordsAreNeverLate()
  process.exit(report('Talk live review: redraws'))
}

void main()
