import type { AskClient, AskEvent } from './protocol'

/**
 * POSTs the AskRequest as JSON to `url` and accepts whichever response shape
 * is easiest for the backend:
 * - `application/x-ndjson`: one AskEvent per line.
 * - `text/event-stream`: SSE, one AskEvent per `data:` line; `[DONE]` ends it.
 * - `application/json`: `{ answer, citations? }` or an array of AskEvents.
 * - anything else: the body is streamed in as answer text.
 */
export function createHttpAskClient(url: string, headers: Record<string, string> = {}): AskClient {
  return {
    async ask(request, onEvent, signal) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers },
        body: JSON.stringify(request),
        signal,
      })
      if (!res.ok) throw new Error(`Ask failed with HTTP ${res.status}`)
      const type = res.headers.get('content-type') ?? ''

      if (type.includes('application/json')) {
        const body = await res.json()
        if (Array.isArray(body)) (body as AskEvent[]).forEach(onEvent)
        else {
          if (body.answer) onEvent({ type: 'text', delta: String(body.answer) })
          for (const postId of body.citations ?? []) onEvent({ type: 'citation', postId })
        }
        onEvent({ type: 'done' })
        return
      }

      const lines = type.includes('ndjson') || type.includes('event-stream')
      const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        if (!lines) {
          onEvent({ type: 'text', delta: value })
          continue
        }
        buffer += value
        const parts = buffer.split('\n')
        buffer = parts.pop() ?? ''
        for (const line of parts) emitLine(line, onEvent)
      }
      if (lines) emitLine(buffer, onEvent)
      onEvent({ type: 'done' })
    },
  }
}

function emitLine(raw: string, onEvent: (e: AskEvent) => void) {
  const line = raw.startsWith('data:') ? raw.slice(5).trim() : raw.trim()
  // Blank lines, SSE comments and other SSE fields carry no events.
  if (!line || line.startsWith(':') || /^(event|id|retry):/.test(line)) return
  if (line === '[DONE]') return
  onEvent(JSON.parse(line) as AskEvent)
}
