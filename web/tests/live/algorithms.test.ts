/**
 * Run: npx -y tsx web/tests/live/algorithms.test.ts
 *
 * The three rules a live talk stands on, with no browser and no network: where a sentence ends,
 * what must never be read out loud, and when a person has stopped talking.
 */
import { createCutter, createDetector, plain, speakable, type Move } from '../../src/live/core'
import { check, report, same } from './check'

function cutterChecks() {
  // The first sentence is cut early, because the wait before the first word is the whole difference
  // between a live talk and a form.
  const early = createCutter()
  const opening = early.feed('Sentiment is holding steady across every subtopic we are watching today and nothing has moved')
  check('1a the first piece is cut early, at a word, so speech starts fast',
    opening.length === 1 && opening[0].length <= 60 && !opening[0].endsWith(' ') && opening[0].split(' ').length > 6,
    `${opening[0]?.length} characters: ${JSON.stringify(opening[0])}`)

  const numbers = createCutter({ firstCut: 400 })
  const heard = numbers.feed('Sentiment moved from 3.5 to 4.2 this week. That is a real rise. ')
  same('1b a number with a full stop in it is never cut in half', heard,
    ['Sentiment moved from 3.5 to 4.2 this week.', 'That is a real rise.'])

  const dates = createCutter({ firstCut: 400 })
  same('1c a date said the short way stays in one piece',
    dates.feed('Posts rose on Sep. 9 and fell again on Sep. 10. The weekend was quiet. '),
    ['Posts rose on Sep. 9 and fell again on Sep. 10.', 'The weekend was quiet.'])

  const names = createCutter({ firstCut: 400 })
  same('1d a name with full stops in it stays in one piece',
    names.feed('Most of them are in the U.S. and a few are in the U.K. Dr. Chen said the same. '),
    ['Most of them are in the U.S. and a few are in the U.K. Dr. Chen said the same.'])

  const long = createCutter({ firstCut: 20, limit: 60 })
  const runOn = long.feed(`${'word '.repeat(40)}`)
  check('1e an answer with no sentence end at all is still cut into pieces',
    runOn.length >= 3 && runOn.every((piece) => piece.length <= 60), `${runOn.length} pieces`)
  const rest = long.flush()
  check('1f and what is left at the end comes out of flush', rest.length >= 1 && rest.join(' ').includes('word'),
    JSON.stringify(rest))

  // The answer really arrives a few characters at a time, so the same text fed one character at a
  // time has to come out as the same sentences.
  const whole = 'Traction doubled overnight. The biggest day was Sep. 10. Nothing else changed. '
  const atOnce = createCutter({ firstCut: 400 })
  const bitByBit = createCutter({ firstCut: 400 })
  const trickled: string[] = []
  for (const letter of whole) trickled.push(...bitByBit.feed(letter))
  same('1g a streamed answer is cut exactly like a finished one', trickled, atOnce.feed(whole))

  const tail = createCutter()
  tail.feed('All quiet')
  same('1h flush never loses the last words', tail.flush(), ['All quiet'])
}

function cleanChecks() {
  const said = speakable('See **this** at https://example.com/x and the post at://did:plc:abc/app.bsky.feed.post/xyz3')
  check('2a web addresses and post addresses are never read out',
    !said.includes('http') && !said.includes('at://') && !said.includes('*') && said.includes('See'), said)

  const ids = speakable('Post 1892345678901234567 and hash a3f9c2b1d4e6f8a0b2c4 say the same thing (did:plc:abcd1234)')
  check('2b long ids, hashes and an id in brackets are dropped',
    !/\d{12}/.test(ids) && !ids.includes('a3f9c2b1d4e6f8a0b2c4') && !ids.includes('did:plc'), ids)

  const code = speakable('Run `select count(*) from posts` and read <b>the total</b>')
  check('2c code spans and tags are dropped', !code.includes('select') && !code.includes('<b>'), code)

  const marks = speakable('- **Sentiment** rose\n- Traction _fell_')
  check('2d bullets, stars and underscores are gone', !/[*_-]/.test(marks) && marks.includes('Sentiment'), marks)

  const hidden = speakable('Most posts ‮came on Sep 9‬​ and ﻿that is\u0000 that.')
  /* oxlint-disable no-control-regex -- This assertion verifies that control characters were removed. */
  check('2e invisible characters that reorder a line are dropped',
    !/[​‬‮﻿\u0000]/.test(hidden), JSON.stringify(hidden))
  /* oxlint-enable no-control-regex */

  const punctuation = plain('Posts rose — a lot → then fell; nobody knows why')
  check('2f dashes, arrows and semicolons become words a person hears',
    !/[—→;]/.test(punctuation), punctuation)

  check('2g a number keeps its full stop', speakable('Sentiment is 3.5 today.').includes('3.5'), speakable('Sentiment is 3.5 today.'))
  check('2h nothing but markup leaves nothing to say', speakable('**`https://example.com`**') === '', JSON.stringify(speakable('**`https://example.com`**')))
}

/** Feeds a loudness pattern to a detector and gives back the moves it made, with their times. */
function run(detect: (level: number, at: number) => Move, pattern: [number, number][], step = 50) {
  const moves: { move: Move; at: number }[] = []
  let at = 1_000_000
  for (const [level, ms] of pattern) {
    for (let left = ms; left > 0; left -= step) {
      at += step
      const move = detect(level, at)
      if (move) moves.push({ move, at })
    }
  }
  return moves
}

function detectorChecks() {
  const quiet = run(createDetector(), [[0.002, 1000], [0.2, 1200], [0.001, 1000]])
  same('3a in a quiet room, one question is one turn', quiet.map((m) => m.move), ['start', 'end'])

  const loudRoom = run(createDetector(), [[0.05, 1000], [0.05, 1500], [0.4, 1200], [0.05, 1000]])
  same('3b in a noisy room, the room itself is not mistaken for speech',
    loudRoom.map((m) => m.move), ['start', 'end'])

  const cough = run(createDetector(), [[0.002, 1000], [0.3, 300], [0.001, 1000]])
  same('3c a cough is heard and then thrown away', cough.map((m) => m.move), ['start', 'drop'])

  const thinking = run(createDetector(), [[0.002, 1000], [0.25, 700], [0.002, 500], [0.25, 700], [0.002, 1000]])
  same('3d a pause in the middle of a sentence does not end the turn',
    thinking.map((m) => m.move), ['start', 'end'])
  check('3e and the turn ends about nine hundred milliseconds after the last word',
    thinking.length === 2 && thinking[1].at - thinking[0].at >= 1900, `${thinking[1].at - thinking[0].at} ms of turn`)

  const silence = run(createDetector(), [[0.002, 3000]])
  same('3f a room nobody speaks in produces no turn at all', silence, [])

  // A tab that was asleep comes back with one reading a minute later. That minute is not speech,
  // and it is not a turn that has already ended: it is one loud reading, worth at most a start.
  const stalled = createDetector()
  run(stalled, [[0.002, 1000]])
  const jump = [stalled(0.3, 1_000_000 + 61_000), stalled(0.3, 1_000_000 + 61_100), stalled(0.002, 1_000_000 + 61_200)]
  check('3g a minute a sleeping tab missed is never a turn that came and went',
    jump.filter((move) => move === 'end' || move === 'drop').length === 0
    && jump.filter((move) => move === 'start').length <= 1, JSON.stringify(jump))
}

cutterChecks()
cleanChecks()
detectorChecks()
process.exit(report('Talk live algorithm'))
