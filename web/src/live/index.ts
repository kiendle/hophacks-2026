/**
 * Talk live: a hands free spoken conversation with the same assistant the Ask box talks to.
 *
 * Three lines mount it, and nothing else in the app changes. See web/src/live/README.md.
 *
 *     const live = useTalkLive({ send, events: liveFeed.events, onActiveChange })
 *     <TalkLiveStrip live={live} />        above the input row
 *     <TalkLiveButton live={live} />       inside it, next to Send
 *
 * with the Ask client wrapped once so the answer can be heard as it is written:
 *
 *     const { messages, send, stop, busy } = useAsk(liveFeed.watch(askClient), getContext)
 */
export {
  createCutter,
  createDetector,
  createSession,
  plain,
  speakable,
  spokenTitle,
  type Cutter,
  type Devices,
  type LiveEvent,
  type LiveState,
  type LiveView,
  type Mic,
  type Playing,
  type Session,
} from './core'
export { browser, createDevices, type Browser } from './devices'
export { createLiveFeed, type AskLikeClient, type AskLikeEvent, type LiveEvents, type LiveFeed } from './feed'
export { useTalkLive, type TalkLive, type TalkLiveOptions } from './useTalkLive'
export { TalkLiveButton, TalkLiveStrip } from './TalkLiveButton'

import { createLiveFeed } from './feed'

/**
 * The one feed the app shares: the Ask client is wrapped with it, the hook listens to it, and the
 * spoken side hears exactly what the panel draws. Making it here keeps the wrapper the same object
 * on every render, so nothing has to be memoised at the call site.
 */
export const liveFeed = createLiveFeed()
