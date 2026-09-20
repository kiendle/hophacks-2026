/**
 * Run: npx -y tsx web/tests/live/feed.test.ts
 *
 * How Talk live hears the assistant: the Ask client is wrapped once, and every event of every turn
 * is repeated to whoever is listening. The panel must not be able to tell the difference, so what
 * is checked here is mostly that nothing changed for it.
 */
import { createLiveFeed, type AskLikeClient, type AskLikeEvent, type LiveEvent } from '../../src/live/feed'
import { check, report, same } from './check'

/** Stands in for web/src/ask's client: it answers with the events the test hands it. */
function client(events: AskLikeEvent[], fail?: string) {
  const asked: unknown[] = []
  const confirmed: string[] = []
  return {
    asked,
    confirmed,
    api: {
      name: 'ask',
      async ask(request: never, onEvent: (event: AskLikeEvent) => void) {
        asked.push(request)
        for (const event of events) onEvent(event)
        if (fail) throw new Error(fail)
      },
      async confirm(id: string) {
        confirmed.push(id)
      },
    } as unknown as AskLikeClient,
  }
}

function feedChecks() {
  const feed = createLiveFeed()
  const heard: LiveEvent[] = []
  const stop = feed.events((event) => heard.push(event))

  const one = client([
    { type: 'text', delta: 'Sentiment is steady. ' },
    { type: 'step', phase: 'start', title: 'Looking at the last hour' },
    { type: 'step', phase: 'end', title: 'Looking at the last hour' },
    { type: 'citation', delta: undefined },
    { type: 'done' },
  ])
  const watched = one.api
  const wrapped = feed.watch(watched)
  check('1a the same client is wrapped once, so nothing has to be memoised at the call site',
    feed.watch(watched) === wrapped, 'one wrapper per client')

  const seen: AskLikeEvent[] = []
  return { feed, heard, stop, one, wrapped, seen }
}

async function main() {
  const { feed, heard, stop, wrapped, seen } = feedChecks()
  await wrapped.ask('a question' as never, (event) => seen.push(event), new AbortController().signal)

  same('1b the panel still receives every event, untouched',
    seen.map((event) => event.type), ['text', 'step', 'step', 'citation', 'done'])
  // The end of a step is passed on as well as its start, so a title waiting to be read out is
  // dropped the moment the thing it was about has finished.
  same('1c and the spoken side hears the turn in the five words it knows',
    heard.map((event) => `${event.type}${event.phase ? `:${event.phase}` : ''}`),
    ['turn_start', 'text', 'step:start', 'step:end', 'turn_end'])
  same('1d with the answer text and the step title in plain words',
    [heard[1].text, heard[2].title], ['Sentiment is steady. ', 'Looking at the last hour'])

  const broken = client([{ type: 'text', delta: 'Half an ans' }], 'The assistant could not answer.')
  let raised = ''
  try {
    await feed.watch(broken.api).ask('again' as never, () => {}, new AbortController().signal)
  } catch (error) {
    raised = error instanceof Error ? error.message : String(error)
  }
  check('2a a turn that fails is still one turn that ended, and the failure still reaches the panel',
    raised === 'The assistant could not answer.'
    && heard.slice(5).map((event) => event.type).join(' ') === 'turn_start text error turn_end',
    `${raised} / ${heard.slice(5).map((event) => event.type).join(' ')}`)

  const rude = createLiveFeed()
  rude.events(() => {
    throw new Error('a listener having a bad day')
  })
  const fine = client([{ type: 'text', delta: 'All quiet. ' }])
  let broke = ''
  try {
    await rude.watch(fine.api).ask('question' as never, () => {}, new AbortController().signal)
  } catch (error) {
    broke = error instanceof Error ? error.message : String(error)
  }
  check('2b a listener that throws never breaks the answer on the screen', broke === '', broke)

  // Anything else the client can do is still its own method, so the confirm button keeps working.
  const withConfirm = client([])
  const also = feed.watch(withConfirm.api) as AskLikeClient & { confirm(id: string): Promise<void>; name: string }
  await also.confirm('confirmation-1')
  same('3a everything else the client can do still works through the wrapper',
    [withConfirm.confirmed, also.name], [['confirmation-1'], 'ask'])

  stop()
  const after = heard.length
  await feed.watch(withConfirm.api).ask('quiet now' as never, () => {}, new AbortController().signal)
  check('3b and a listener that has stopped listening hears nothing more', heard.length === after, `${heard.length - after} extra`)

  process.exit(report('Talk live feed'))
}

void main()
