import { createHttpAskClient } from './httpClient'
import { mockAskClient } from './mockClient'
import type { AskClient } from './protocol'

export * from './protocol'
export { buildAskRequest, type AskContext } from './context'
export { useAsk, type ChatMessage } from './useAsk'

/**
 * Set VITE_ASK_URL (e.g. in web/.env.local) to point the panel at the real
 * chatbot; without it, answers come from the mock.
 */
const url = import.meta.env.VITE_ASK_URL as string | undefined
export const askClient: AskClient = url ? createHttpAskClient(url) : mockAskClient
