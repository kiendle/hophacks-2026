/**
 * Run: npx -y tsx web/tests/live/review/speaking.test.ts
 *
 * The mouth. A real /api/live/say answer arrives in two moments, not one: the server says "here
 * comes an mp3" and then the sound arrives byte by byte for as long as the sentence is. These tests
 * stop it in the second moment, which is where a person who talks over the answer really stops it,
 * and they watch what a cough costs, and when a tool title may be read out.
 */
import { createSession, type Session } from '../../../src/live/core'
import { check, report, same, settle } from '../check'
import { ReviewDevices, room, sound } from './fakes'

interface Talk {
  devices: ReviewDevices
  session: Session
  stopped: number
}

async function open(canStop = false): Promise<Talk> {
  const devices = new ReviewDevices()
  const talk = { devices, session: null as unknown as Session, stopped: 0 }
  talk.session = createSession({
    devices,
    send: () => {},
    stop: canStop ? () => { talk.stopped += 1 } : undefined,
  })
  await talk.session.start()
  await room(devices)
  return talk
}

/** Talking over a sentence whose sound is already coming down the wire. */
async function bargeInWhileTheSoundArrives() {
  const { devices, session } = await open()
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Sentiment is steady tonight. Traction has doubled since Friday. ' })
  await settle()
  same('1a the sentence being spoken and the one after it are both asked for',
    devices.spoken, ['Sentiment is steady tonight.', 'Traction has doubled since Friday.'])

  devices.says[0].headers() // the server is now streaming the sound of the first sentence
  await settle()
  check('1b the first one is being streamed and no sound has played yet',
    devices.says[0].phase === 'body' && devices.played.length === 0, devices.says[0].phase)

  await sound(devices, [[0.9, 300]]) // the person talks over it
  await settle()
  check('1c talking over it drops the sentence that had not started yet',
    devices.says[1].aborted === true, `aborted in the ${devices.says[1].abortedIn} step`)
  check('1d and the one whose sound is arriving, so the service stops reading and stops billing',
    devices.says[0].aborted === true && devices.says[0].abortedIn === 'body',
    `aborted ${devices.says[0].aborted} in the ${devices.says[0].abortedIn} step`)

  // Nothing is read out over them while they are still speaking.
  session.handle({ type: 'turn_end' })
  await settle()
  const duringTheirTurn = devices.spoken.length
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'The last hour is calm. Nothing has changed since. ' })
  await settle()
  check('1e the answer to the next question is not read over the person who is still talking',
    devices.spoken.length === duringTheirTurn, `${devices.spoken.length} sentences asked for`)

  await sound(devices, [[0.002, 1000]]) // they stop, and their turn goes off to the server
  await settle()
  check('1f and the moment they stop, the voice is free and the answer is read out',
    devices.spoken.length > duringTheirTurn && devices.played.length <= 1,
    `${devices.spoken.length} sentences asked for, ${devices.played.length} played`)
  session.stop()
}

/** A step title stored while the answer was still being read out. */
async function stepAfterTheAnswer() {
  const { devices, session } = await open()
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Sentiment is steady tonight. ' })
  await settle()
  devices.says[0].all()
  await settle()
  check('2a the first sentence is playing', devices.played.length === 1, `${devices.played.length}`)

  session.handle({ type: 'step', phase: 'start', title: 'Reading the last hour of posts' })
  session.handle({ type: 'text', text: 'Traction has doubled since Friday.' })
  session.handle({ type: 'turn_end' })
  await settle()
  devices.played[0].finish()
  await settle()
  devices.says[1]?.all()
  await settle()
  devices.played[1]?.finish()
  await settle()
  devices.says[2]?.all()
  await settle()
  same('2b the answer is read out and the tool title is not read out after it',
    devices.spoken, ['Sentiment is steady tonight.', 'Traction has doubled since Friday.'])
  check('2c and the strip is back to listening with nothing left to say',
    session.view().state === 'Listening', session.view().state)
  session.stop()
}

/** A step title read out while the tool is running, which is the whole point of one. */
async function stepWhileTheToolRuns() {
  const { devices, session } = await open()
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'That needs a live look, give me about half a minute' })
  session.handle({ type: 'step', phase: 'start', title: 'Asking Jev to read 150 Bluesky posts about "AI", "chatbot"' })
  await settle()
  same('3a the sentence written before the tool call is said first, while the wait is still ahead',
    devices.spoken[0], 'That needs a live look, give me about half a minute')

  devices.says[0].all()
  await settle()
  devices.played[0].finish()
  await settle()
  same('3b and the title covers the wait after it, short enough to be one breath',
    devices.spoken[1], 'Asking Jev to read 150 Bluesky posts')
  devices.says[1]?.all()
  await settle()
  devices.played[1]?.finish()
  await settle()
  // The tool comes back and the answer arrives: a second title is not read out over it.
  session.handle({ type: 'step', phase: 'end', title: 'Asking Jev to read 150 Bluesky posts about "AI", "chatbot"' })
  session.handle({ type: 'text', text: 'The mood on Bluesky right now leans negative. ' })
  session.handle({ type: 'turn_end' })
  await settle()
  devices.says[2]?.all()
  await settle()
  same('3c then the answer itself, with no title after the step it belonged to',
    devices.spoken.slice(2), ['The mood on Bluesky right now leans negative.'])
  session.stop()
}

/** A cough, a laugh from the audience, a door. */
async function aCoughIsNotAQuestion() {
  const { devices, session } = await open()
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Sentiment is steady tonight. Traction has doubled since Friday. ' })
  await settle()
  devices.says[0].all()
  await settle()
  check('4a the answer is being read out', devices.played.length === 1, `${devices.played.length} played`)

  await sound(devices, [[0.9, 200], [0.002, 1000]]) // a cough, not a question
  await settle()
  check('4b the cough stops the sound at once, because talking over it has to work',
    devices.played[0].stopped === true)
  check('4c and nothing is sent to the server, because it was too short to be a question',
    devices.listens.length === 0, `${devices.listens.length} turns sent`)

  const before = devices.spoken.length
  session.handle({ type: 'text', text: 'The weekend was quiet. ' })
  session.handle({ type: 'turn_end' })
  await settle()
  devices.says[before]?.all()
  await settle()
  check('4d the rest of the answer is read out: a cough does not cost the person their answer',
    devices.spoken.length > before && devices.spoken.at(-1) === 'The weekend was quiet.',
    `${devices.spoken.length} sentences asked for, last ${JSON.stringify(devices.spoken.at(-1))}`)
  check('4e and nothing on the strip says anything went wrong', session.view().note === '',
    JSON.stringify(session.view().note))
  session.stop()
}

/** A real question over the answer, with a chat that can be stopped. */
async function bargeInStopsTheTurn() {
  const talk = await open(true)
  const { devices, session } = talk
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Sentiment is steady tonight. Traction has doubled since Friday. ' })
  await settle()
  devices.says[0].all()
  await settle()

  await sound(devices, [[0.9, 900], [0.002, 1000]]) // they ask something else out loud
  await settle()
  check('5a a real question over the answer stops the turn, so they are not left in silence',
    talk.stopped === 1, `the chat was stopped ${talk.stopped} times`)

  // The panel ends the turn its own way, with the abort as its error. That is not news.
  session.handle({ type: 'error', text: 'The operation was aborted.' })
  session.handle({ type: 'turn_end' })
  await settle()
  check('5b and the abort we asked for is not written under the input row',
    session.view().note === '', JSON.stringify(session.view().note))
  check('5c the rest of the answer they talked over is never read out',
    devices.spoken.length === 2, devices.spoken.join(' | '))
  session.stop()
}

async function main() {
  await bargeInWhileTheSoundArrives()
  await stepAfterTheAnswer()
  await stepWhileTheToolRuns()
  await aCoughIsNotAQuestion()
  await bargeInStopsTheTurn()
  process.exit(report('Talk live review: speaking'))
}

void main()
