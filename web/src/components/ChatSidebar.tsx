import { useEffect, useRef, useState } from 'react'
import { askClient, useAsk, type AskContext, type ChatMessage, type EvidencePost } from '../ask'
import '../ask/ask.css'
import { textOn } from '../color'
import type { Selection, Series } from '../data/types'
import { formatRange } from '../format'
import { sentimentColor } from '../sentimentColor'
import { BotIcon, CloseIcon, SendIcon, StopIcon } from './icons'

interface Props {
  selection: Selection
  /** Used to name and color the selected subtopics. */
  series: Series[]
  onClearSelection: () => void
  /** Snapshot of what the app shows, read when a question is sent. */
  getContext: () => AskContext | null
}

export function ChatSidebar({ selection, series, onClearSelection, getContext }: Props) {
  const [text, setText] = useState('')
  const { messages, send, stop, busy } = useAsk(askClient, getContext)
  const byId = new Map(series.map((s) => [s.id, s]))
  const subtopics = selection.subtopics.map((id) => byId.get(id)).filter((s) => s !== undefined)
  const empty = !selection.range && subtopics.length === 0

  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [messages])

  const submit = () => {
    if (busy || !text.trim()) return
    send(text)
    setText('')
  }

  return (
    <aside className="chat">
      <div className="messages" ref={scroller}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <BotIcon size={28} />
            Ask Sentibot anything
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} byId={byId} />
        ))}
      </div>
      {!empty && (
        <div className="chip">
          <div className="chip-body">
            {subtopics.map((s) => (
              <span key={s.id} className="subtopic-tag" style={{ background: s.color, color: textOn(s.color) }}>
                {s.name}
              </span>
            ))}
            {selection.range && <span className="chip-range">{formatRange(selection.range)}</span>}
          </div>
          <button className="icon-btn" aria-label="Clear selection" onClick={onClearSelection}>
            <CloseIcon size={13} />
          </button>
        </div>
      )}
      <form
        className="input-row"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask" />
        {busy ? (
          <button type="button" className="send" aria-label="Stop" onClick={stop}>
            <StopIcon size={16} />
          </button>
        ) : (
          <button type="submit" className="send" aria-label="Send">
            <SendIcon size={16} />
          </button>
        )}
      </form>
    </aside>
  )
}

function Message({ message: m, byId }: { message: ChatMessage; byId: Map<string, Series> }) {
  if (m.role === 'user')
    return (
      <div className="msg msg-user">
        <div className="msg-bubble">{m.text}</div>
        {m.scope && (
          <div className="msg-scope">
            {m.scope.subtopics.map((id) => {
              const s = byId.get(id)
              return s ? <span key={id} className="msg-scope-dot" style={{ background: s.color }} /> : null
            })}
            {formatRange(m.scope.range)}
          </div>
        )}
      </div>
    )

  return (
    <div className="msg msg-assistant">
      {m.text && (
        <p className="msg-text">
          {m.text}
          {m.status === 'streaming' && <span className="caret" />}
        </p>
      )}
      {!m.text && m.status === 'streaming' && <span className="typing" aria-label="Answering" />}
      {m.status === 'error' && <p className="msg-error">No response</p>}
      {m.citations.map((p) => (
        <Citation key={p.id} post={p} color={byId.get(p.subtopic)?.color} />
      ))}
    </div>
  )
}

function Citation({ post, color }: { post: EvidencePost; color?: string }) {
  return (
    <div className="cite" style={{ borderLeftColor: color }}>
      <div className="cite-head">
        <span className="handle">{post.handle}</span>
        <span className="cite-score" style={{ color: sentimentColor(post.sentiment) }}>
          {post.sentiment.toFixed(1)}
        </span>
      </div>
      <p className="cite-text">{post.text}</p>
    </div>
  )
}
