/**
 * Run: npx -y tsx web/tests/live/session.test.ts
 *
 * One whole live talk, without a microphone: the turn is heard and sent as a question, the answer
 * is spoken sentence by sentence with one fetch running ahead, talking over it stops everything at
 * once, Mute is silence, and every way out hands the microphone back.
 */
import { createSession, type LiveState, type Session } from '../../src/live/core'
import { check, report, same, settle } from './check'
import { FakeDevices, speak } from './fakes'

interface Talk {
  devices: FakeDevices
  session: Session
  sent: string[]
  states: LiveState[]
}

async function open(): Promise<Talk> {
  const devices = new FakeDevices()
  const sent: string[] = []
  const states: LiveState[] = []
  const session = createSession({
    devices,
    send: (text: string) => sent.push(text),
    onView: (view) => {
      if (states.at(-1) !== view.state) states.push(view.state)
    },
  })
  await session.start()
  await speak(devices, [[0.002, 1100]]) // the first second is the room being measured
  return { devices, session, sent, states }
}

/** A turn: a second of speech, then the quiet that ends it. */
async function aQuestion(talk: Talk) {
  await speak(talk.devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
}

async function turnChecks() {
  const talk = await open()
  check('1a the microphone is opened once and the strip says Listening',
    talk.devices.opens === 1 && talk.session.view().state === 'Listening', talk.session.view().state)

  await speak(talk.devices, [[0.002, 5000]]) // five seconds of an empty room before the question
  await aQuestion(talk)
  const clip = talk.devices.listens[0]
  const recorded = talk.devices.mic.pieces * 4096
  check('1b one turn goes to the server as one clip, holding that turn and not the quiet before it',
    talk.devices.listens.length === 1 && clip.size > 512 && clip.size < recorded / 2 && clip.type === 'audio/webm',
    `${clip?.size} bytes sent of ${recorded} bytes recorded`)
  same('1c and what it heard is asked exactly as a typed question is', talk.sent, ['How are people talking about the launch'])
  check('1d the strip says Heard you while it waits', talk.states.includes('Heard you'), talk.states.join(' '))

  talk.session.handle({ type: 'turn_start' })
  check('1e and Thinking once the assistant starts', talk.session.view().state === 'Thinking', talk.session.view().state)
  talk.session.stop()

  const cough = await open()
  await speak(cough.devices, [[0.3, 200], [0.002, 1000]])
  await settle()
  check('1f a cough is never sent anywhere', cough.devices.listens.length === 0, `${cough.devices.listens.length} clips`)
  cough.session.stop()

  const silent = await open()
  await speak(silent.devices, [[0.002, 2000]])
  check('1g and silence is never sent anywhere', silent.devices.listens.length === 0, `${silent.devices.listens.length} clips`)
  silent.session.stop()

  // The chat drops a question sent while it is still answering the last one, so it waits instead.
  const busy = await open()
  busy.session.handle({ type: 'turn_start' })
  await aQuestion(busy)
  check('1h a question asked mid answer waits rather than vanishing', busy.sent.length === 0, JSON.stringify(busy.sent))
  busy.session.handle({ type: 'turn_end' })
  await settle()
  same('1i and is asked as soon as the answer is over', busy.sent, ['How are people talking about the launch'])
  busy.session.stop()
}

async function speakingChecks() {
  const talk = await open()
  talk.devices.holdSay = true // every sentence waits for this test to answer it
  talk.session.handle({ type: 'turn_start' })
  talk.session.handle({ type: 'text', text: 'Sentiment is steady. Traction doubled overnight. The weekend was quiet. ' })
  await settle()
  same('2a the first two sentences are fetched, the current one and one ahead',
    talk.devices.spoken, ['Sentiment is steady.', 'Traction doubled overnight.'])

  talk.devices.says[0].answer()
  await settle()
  check('2b and the first one starts playing while the second is still coming',
    talk.devices.played.length === 1 && talk.devices.says.length === 2, `${talk.devices.played.length} playing`)
  check('2c the strip says Speaking', talk.session.view().state === 'Speaking', talk.session.view().state)

  talk.devices.says[1].answer()
  talk.devices.played[0].finish()
  await settle()
  same('2d the third is only fetched once the second is under way',
    talk.devices.spoken, ['Sentiment is steady.', 'Traction doubled overnight.', 'The weekend was quiet.'])

  talk.devices.says[2].answer()
  talk.devices.played[1].finish()
  await settle()
  talk.devices.played[2].finish()
  await settle()
  talk.session.handle({ type: 'turn_end' })
  await settle()
  check('2e when the answer is over it goes back to listening',
    talk.session.view().state === 'Listening', talk.session.view().state)
  talk.session.stop()

  // A step title is news while the person waits, and news nobody needs five seconds later.
  const steps = await open()
  steps.devices.holdSay = true
  steps.session.handle({ type: 'turn_start' })
  steps.session.handle({ type: 'step', phase: 'start', title: 'Looking at the last hour of posts' })
  await settle()
  same('2f a step title is read out while nothing else is waiting',
    steps.devices.spoken, ['Looking at the last hour of posts'])
  steps.session.handle({ type: 'step', phase: 'start', title: 'Counting the matches' })
  steps.session.handle({ type: 'step', phase: 'start', title: 'Reading the examples' })
  steps.devices.says[0].answer()
  await settle()
  steps.devices.played[0].finish()
  await settle()
  check('2g only the newest one is still waiting, never a queue of them',
    steps.devices.spoken.length === 2 && steps.devices.spoken[1] === 'Reading the examples', steps.devices.spoken.join(' | '))
  steps.session.stop()

  const stale = await open()
  stale.devices.holdSay = true
  stale.session.handle({ type: 'turn_start' })
  stale.session.handle({ type: 'text', text: 'Sentiment is steady. ' })
  await settle()
  stale.session.handle({ type: 'step', phase: 'start', title: 'Counting the matches' })
  stale.devices.clock.time += 6000 // the step it was about finished long ago
  stale.devices.says[0].answer()
  await settle()
  stale.devices.played[0].finish()
  await settle()
  check('2h a step title older than five seconds is dropped, not read out',
    stale.devices.spoken.length === 1, stale.devices.spoken.join(' | '))
  stale.session.stop()

  const whole = await open()
  whole.session.handle({ type: 'turn_start' })
  whole.session.handle({ type: 'text', text: 'All quiet' }) // no full stop anywhere
  whole.session.handle({ type: 'turn_end' })
  await settle()
  same('2i the last words are spoken even when the answer never ended a sentence', whole.devices.spoken, ['All quiet'])
  whole.session.stop()

  const refused = await open()
  refused.devices.sayStatus = 502
  refused.session.handle({ type: 'turn_start' })
  refused.session.handle({ type: 'text', text: 'Sentiment is steady. Traction doubled. ' })
  await settle()
  check('2j a voice service that says no is one plain sentence on the strip, and the talk goes on',
    refused.session.view().note.length > 0 && !refused.session.view().note.includes('http')
    && refused.session.view().active, JSON.stringify(refused.session.view().note))
  refused.session.stop()
}

async function bargeInChecks() {
  const talk = await open()
  talk.devices.holdSay = true
  talk.session.handle({ type: 'turn_start' })
  talk.session.handle({ type: 'text', text: 'Sentiment is steady. Traction doubled overnight. The weekend was quiet. ' })
  await settle()
  talk.devices.says[0].answer()
  await settle()
  const playing = talk.devices.played[0]
  const pending = talk.devices.says[1]
  check('3a it is speaking and one sentence is already on its way', !playing.stopped && talk.devices.says.length === 2)

  await speak(talk.devices, [[0.9, 300]]) // the person talks over it
  await settle()
  check('3b talking over it stops the sound at once', playing.stopped, 'the player was stopped')
  check('3c and drops the sentence that was already being fetched', pending.signal?.aborted === true,
    `aborted: ${pending.signal?.aborted}`)
  const spokenSoFar = talk.devices.spoken.length
  talk.session.handle({ type: 'text', text: 'And another thing. ' })
  await settle()
  check('3d nothing left in the queue is spoken after the interruption',
    talk.devices.spoken.length === spokenSoFar, talk.devices.spoken.join(' | '))

  await speak(talk.devices, [[0.9, 700], [0.002, 1000]])
  await settle()
  check('3e the microphone kept recording, so what they said over it became a question',
    talk.devices.listens.length === 1, `${talk.devices.listens.length} clips`)
  talk.session.handle({ type: 'turn_end' })
  await settle()
  same('3f and it is asked as soon as the answer they cut off is over', talk.sent, ['How are people talking about the launch'])
  check('3g nothing of the answer they cut off is spoken after it ends',
    talk.devices.spoken.length === spokenSoFar, talk.devices.spoken.join(' | '))
  talk.session.stop()
}

async function muteChecks() {
  const talk = await open()
  talk.session.setMuted(true)
  check('4a Mute says Muted', talk.session.view().state === 'Muted' && talk.session.view().muted, talk.session.view().state)
  await speak(talk.devices, [[0.9, 1500], [0.002, 1000]])
  await settle()
  check('4b and nothing said while muted is sent anywhere',
    talk.devices.listens.length === 0 && talk.sent.length === 0, `${talk.devices.listens.length} clips`)
  talk.session.setMuted(false)
  check('4c Unmute goes back to listening', talk.session.view().state === 'Listening', talk.session.view().state)
  await speak(talk.devices, [[0.3, 1000], [0.002, 1000]])
  await settle()
  check('4d and the microphone works again', talk.devices.listens.length === 1, `${talk.devices.listens.length} clips`)
  talk.session.stop()
}

async function exitChecks() {
  const ended = await open()
  ended.session.stop()
  check('5a End hands the microphone back and stops the sampler',
    ended.devices.mic.closed === 1 && ended.devices.clock.running === 0 && ended.session.view().state === 'Ended',
    `closed ${ended.devices.mic.closed} times, ${ended.devices.clock.running} tickers left`)
  ended.session.stop()
  check('5b ending twice is safe and does not close it twice', ended.devices.mic.closed === 1, `${ended.devices.mic.closed}`)

  const lost = await open()
  lost.devices.breakListen = true // the server went away mid talk
  await aQuestion(lost)
  check('5c losing the voice service ends the talk and hands the microphone back',
    lost.devices.mic.closed === 1 && lost.session.view().state === 'Ended' && lost.session.view().note.length > 0,
    JSON.stringify(lost.session.view().note))

  const refused = new FakeDevices()
  refused.refuseMic = true
  const noMic = createSession({ devices: refused, send: () => {} })
  const started = await noMic.start()
  check('5d a browser that refuses the microphone says so and leaves nothing running',
    started === false && noMic.view().active === false && refused.clock.running === 0
    && noMic.view().note.includes('microphone'), JSON.stringify(noMic.view().note))

  const quiet = await open()
  quiet.devices.holdSay = true
  quiet.session.handle({ type: 'turn_start' })
  quiet.session.handle({ type: 'text', text: 'Sentiment is steady. Traction doubled overnight. ' })
  await settle()
  const playing = quiet.devices.played
  quiet.devices.says[0].answer()
  await settle()
  quiet.session.stop()
  check('5e ending while it is talking stops the sound too',
    quiet.devices.played[0]?.stopped === true && quiet.devices.mic.closed === 1, `${playing.length} clips played`)

  const failed = await open()
  failed.session.handle({ type: 'turn_start' })
  failed.session.handle({ type: 'error', text: 'The assistant could not answer.' })
  await settle()
  check('5f an error is said in plain words and the talk stays open to try again',
    failed.session.view().note === 'The assistant could not answer.' && failed.session.view().active
    && failed.session.view().state === 'Listening', JSON.stringify(failed.session.view()))
  failed.session.stop()

  const broken = await open()
  broken.session.handle({ type: 'turn_start' })
  broken.session.handle({ type: 'text', text: 'Traction has doubled since' }) // no sentence end yet
  broken.session.handle({ type: 'error', text: 'The assistant could not answer.' })
  broken.session.handle({ type: 'turn_end' })
  await settle()
  check('5g and half a sentence from the answer that failed is not read out after it',
    broken.devices.says.length === 0 && broken.devices.played.length === 0, `${broken.devices.says.length} sentences asked for`)
  broken.session.stop()
}

async function readAloudChecks() {
  const devices = new FakeDevices()
  const session = createSession({ devices, send: () => {}, readAloud: false })
  await session.start()
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'step', phase: 'start', title: 'Checking the data' })
  session.handle({ type: 'text', text: 'This reply should stay silent.' })
  session.handle({ type: 'turn_end' })
  await settle()
  check('read aloud off makes no speech requests', devices.says.length === 0 && devices.played.length === 0)
  session.setReadAloud(true)
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'This reply can be read aloud.' })
  session.handle({ type: 'turn_end' })
  await settle()
  check('read aloud on permits playback', devices.played.length === 1)
  session.setReadAloud(false)
  check('turning read aloud off stops current playback', devices.played[0]?.stopped === true)
  const count = devices.says.length
  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Silent again.' })
  session.handle({ type: 'turn_end' })
  await settle()
  check('later replies remain silent after disabling', devices.says.length === count)
  session.stop()
}

async function main() {
  await readAloudChecks()
  await turnChecks()
  await speakingChecks()
  await bargeInChecks()
  await muteChecks()
  await exitChecks()
  process.exit(report('Talk live session'))
}

void main()
