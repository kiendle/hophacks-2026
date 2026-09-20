/**
 * Run: npx -y tsx web/tests/live/review/plumbing.test.ts
 *
 * The joint between the spoken side and the Ask panel. Two turns reach the panel: the one that
 * answers a question, and the one that follows a Yes or No on a confirmation card. Both have to be
 * heard, and while either runs the spoken side has to know the chat is busy.
 *
 * Also what happens when the panel simply refuses a question, which is what it does whenever it is
 * already answering: the person must be told, not left waiting on a silent room.
 */
import { createSession } from '../../../src/live/core'
import { createLiveFeed, type AskLikeEvent, type LiveEvent } from '../../../src/live/feed'
import { check, report, same, settle } from '../check'
import { ReviewDevices, room, sound } from './fakes'

interface Client {
  ask(request: never, onEvent: (event: AskLikeEvent) => void, signal: AbortSignal): Promise<void>
  confirm(
    conversationId: string,
    confirmationId: string,
    approved: boolean,
    onEvent: (event: AskLikeEvent) => void,
    signal: AbortSignal,
  ): Promise<void>
}

/** The confirmation card, and the turn that follows Yes on it. */
async function theConfirmTurn() {
  const feed = createLiveFeed()
  const heard: LiveEvent[] = []
  feed.events((event) => heard.push(event))
  const answered: string[] = []
  const client: Client = {
    ask: async (_request, onEvent) => {
      onEvent({ type: 'text', delta: 'I can post that for you.' })
      onEvent({ type: 'confirm', summary: 'Post this to Telegram' } as AskLikeEvent)
    },
    confirm: async (_conversation, confirmation, approved, onEvent) => {
      answered.push(`${confirmation}:${approved}`)
      onEvent({ type: 'step', phase: 'start', title: 'Posting to Telegram' })
      onEvent({ type: 'text', delta: 'Posted. It is live now.' })
    },
  }
  const seen = feed.watch(client)

  await seen.ask(null as never, () => {}, new AbortController().signal)
  same('1a a question is heard as a whole turn',
    heard.map((event) => event.type), ['turn_start', 'text', 'text', 'turn_end'])
  const said = String(heard[2].text || '')
  check('1b and the confirmation the assistant is waiting on is read out, with what to press',
    said.includes('Post this to Telegram') && said.toLowerCase().includes('yes'), JSON.stringify(said))

  heard.length = 0
  await seen.confirm('conversation-1', 'confirmation-1', true, () => {}, new AbortController().signal)
  same('1c and the turn that follows Yes is a turn like any other, read out from beginning to end',
    heard.map((event) => event.type), ['turn_start', 'step', 'text', 'turn_end'])
  same('1d with the decision passed through to the chat untouched', answered, ['confirmation-1:true'])
}

/** The panel drops a question it is too busy to take. */
async function theDroppedQuestion() {
  const devices = new ReviewDevices()
  const tries: string[] = []
  // Exactly what useAsk.send does when it is already answering, or when the page has no context
  // yet: `if (!ctx || !question.trim() || inFlight.current) return`. No throw, no event, nothing.
  const session = createSession({ devices, send: (text) => { tries.push(text) } })
  await session.start()
  await room(devices)
  await sound(devices, [[0.30, 1000], [0.002, 1000]])
  await settle()
  check('2a the question was handed over', tries.length === 1, JSON.stringify(tries))
  check('2b the strip says Heard you', session.view().state === 'Heard you', session.view().state)
  await sound(devices, [[0.002, 4000]])
  await settle()
  check('2c and when nothing at all comes back, the person is told to say it again',
    session.view().state === 'Listening' && session.view().note.includes('say it again'),
    `${session.view().state}, note ${JSON.stringify(session.view().note)}`)
  session.stop()

  // A chat that can say no says no at once, rather than three seconds later.
  const quick = new ReviewDevices()
  const refused = createSession({ devices: quick, send: () => false })
  await refused.start()
  await room(quick)
  await sound(quick, [[0.30, 1000], [0.002, 1000]])
  await settle()
  check('2d a chat that answers false is heard straight away',
    refused.view().note.includes('say it again') && refused.view().state === 'Listening',
    `${refused.view().state}, note ${JSON.stringify(refused.view().note)}`)
  refused.stop()
}

/** A turn that is already running when the person turns Talk live on. */
async function joiningMidTurn() {
  const devices = new ReviewDevices()
  const session = createSession({ devices, send: () => {} })
  await session.start()
  await room(devices)
  // The person typed a question, then pressed the Talk live button while it was being answered.
  // turn_start happened before this session existed, so it never heard the beginning of it.
  session.handle({ type: 'text', text: 'Sentiment is steady tonight. ' })
  session.handle({ type: 'step', phase: 'start', title: 'Reading the last hour' })
  await settle()
  check('3a the tail of an answer that began before the talk did is not read out, mid sentence',
    devices.spoken.length === 0, JSON.stringify(devices.spoken))
  session.handle({ type: 'turn_end' })
  await settle()

  session.handle({ type: 'turn_start' })
  session.handle({ type: 'text', text: 'Traction has doubled since Friday. ' })
  await settle()
  same('3b and the next answer, which does begin with us, is read out in full',
    devices.spoken, ['Traction has doubled since Friday.'])
  session.stop()

  // Somebody who starts listening in the middle of a turn is told that a turn is running.
  const feed = createLiveFeed()
  feed.turnStart()
  const late: LiveEvent[] = []
  feed.events((event) => late.push(event))
  same('3c a listener that arrives mid turn is told the chat is busy',
    late.map((event) => event.type), ['turn_start'])
}

async function main() {
  await theConfirmTurn()
  await theDroppedQuestion()
  await joiningMidTurn()
  process.exit(report('Talk live review: plumbing'))
}

void main()
