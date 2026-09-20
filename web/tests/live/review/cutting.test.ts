/**
 * Run: npx -y tsx web/tests/live/review/cutting.test.ts
 *
 * Where the answer is cut, and what is said when it is read out. These are all taken from real
 * captures against the running server.
 *
 * The worst of them: the one sentence whose whole job is to cover a long wait, "that needs a live
 * look, give me about half a minute", was not spoken until after the wait was over, and arrived
 * welded onto the first words of the answer. The cutter was never closed when a tool call began, so
 * everything written before it sat in the buffer for the whole tool call, and the blank line that
 * would have ended the sentence is dropped at the boundary between text blocks.
 */
import { createCutter, createSession, speakable, spokenTitle } from '../../../src/live/core'
import { check, report, same, settle } from '../check'
import { ReviewDevices, room } from './fakes'

/** The deltas of a real turn, as they arrived, with the blank line lost at the tool call. */
const WAIT = ['That needs a', ' live look,', ' give me about a minute.']
const AFTER = ['The mood on', ' Bluesky right now leans negative.']

function theToolCallEndsTheSentence() {
  const cutter = createCutter()
  const spoken: string[] = []
  for (const piece of WAIT) spoken.push(...cutter.feed(piece))
  check('1a nothing can be said yet: the sentence has no whitespace after its full stop',
    spoken.length === 0, JSON.stringify(spoken))

  spoken.push(...cutter.close()) // the tool call begins
  same('1b closing at the tool call says the whole sentence, and only it',
    spoken, ['That needs a live look, give me about a minute.'])

  for (const piece of AFTER) spoken.push(...cutter.feed(piece))
  spoken.push(...cutter.flush())
  same('1c and the answer that comes back afterwards is a sentence of its own',
    spoken.slice(1), ['The mood on Bluesky right now leans negative.'])
}

/** The same thing through the session, which is where it has to work. */
async function theWaitIsCoveredInTime() {
  const devices = new ReviewDevices()
  const session = createSession({ devices, send: () => {} })
  await session.start()
  await room(devices)
  session.handle({ type: 'turn_start' })
  for (const piece of WAIT) session.handle({ type: 'text', text: piece })
  await settle()
  check('2a while the assistant is still writing, nothing is asked for yet',
    devices.spoken.length === 0, JSON.stringify(devices.spoken))

  session.handle({ type: 'step', phase: 'start', title: 'Reading the last hour of posts' })
  await settle()
  same('2b the moment the tool call starts, the sentence that covers the wait is read out',
    devices.spoken[0], 'That needs a live look, give me about a minute.')
  session.stop()
}

function neverInsideAName() {
  // A real answer: the early cut used to land at 55 characters, inside the project name, leaving a
  // quotation open and splitting "Anthropic and AI safety" across two clips of sound.
  const cutter = createCutter()
  const line = 'I\'m reading from the finished project "Anthropic and AI safety", based on posts in the X/Twitter archive.'
  const spoken = [...cutter.feed(line), ...cutter.flush()]
  check('3a no piece is left with a quotation hanging open',
    spoken.every((piece) => (piece.match(/"/g) || []).length % 2 === 0), JSON.stringify(spoken))
  check('3b and the name is never split across two of them',
    spoken.some((piece) => piece.includes('Anthropic and AI safety')), JSON.stringify(spoken))

  const bracket = createCutter()
  const held = bracket.feed('The biggest day by far (and the one everybody is quoting) was Sep. 9 this year. ')
  check('3c a bracket is kept whole in the same way',
    held.length === 1 && held[0].includes('(and the one everybody is quoting)'), JSON.stringify(held))

  // A quotation that never closes must not hold speech up for ever.
  const runOn = createCutter()
  const open = runOn.feed(`He said "${'and on '.repeat(120)}`)
  check('3d but a quotation nobody closes still gets cut, at the limit',
    open.length >= 1 && open.every((piece) => piece.length <= 600), `${open.length} pieces`)

  // The early cut is still early when there is nothing open.
  const plain = createCutter()
  const first = plain.feed('Sentiment is holding steady across every subtopic we are watching today and nothing has moved')
  check('3e and an ordinary first sentence is still cut early, so speech starts fast',
    first.length === 1 && first[0].length <= 60, `${first[0]?.length} characters`)
}

function saidTheWayAPersonSaysIt() {
  const said = speakable('The mood in the X/Twitter archive is calm, and he/she agrees.')
  check('4a a slash between two letters is not read out',
    !said.includes('/') && said.includes('X Twitter'), said)
  check('4b while a date and a number keep theirs',
    speakable('On 9/10 it was 3.5, up from 2.1.') === 'On 9/10 it was 3.5, up from 2.1.',
    speakable('On 9/10 it was 3.5, up from 2.1.'))

  same('4c a long tool title is read out as its first clause',
    spokenTitle('Asking Jev to read 150 Bluesky posts about "AI", "artificial intelligence", "chatbot"'),
    'Asking Jev to read 150 Bluesky posts')
  same('4d a short one is left exactly as it was written',
    spokenTitle('Reading the last hour of posts'), 'Reading the last hour of posts')
  same('4e and nothing at all stays nothing', spokenTitle(''), '')
}

async function main() {
  theToolCallEndsTheSentence()
  await theWaitIsCoveredInTime()
  neverInsideAName()
  saidTheWayAPersonSaysIt()
  process.exit(report('Talk live review: cutting'))
}

void main()
