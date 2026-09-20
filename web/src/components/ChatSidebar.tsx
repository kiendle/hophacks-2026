import { liveFeed, useTalkLive, TalkLiveButton, TalkLiveStrip } from '../live'
import { ToolActivity, ResultCard, ConfirmationCard } from './ToolCards'
import { useVoice, type Voice } from '../voice/useVoice'
import { VoiceBar, VoiceMic, ReplyActions } from '../voice/VoiceControls'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { askClient, buildAskRequest, useAsk, type AskContext, type ChatMessage, type EvidencePost } from '../ask'
import '../ask/ask.css'
import { textOn } from '../color'
import type { Selection, Series } from '../data/types'
import { formatRange } from '../format'
import { sentimentColor } from '../sentimentColor'
import { BotIcon, CloseIcon, SendIcon, StopIcon } from './icons'
import { harnessAskClient } from '../ask/harnessClient'
import { TranslatablePost } from './TranslatablePost'

interface Props {
  active?: boolean
  initialQuestion?: string
  proposalMode?: boolean
  selection: Selection
  /** Used to name and color the selected subtopics. */
  series: Series[]
  onClearSelection: () => void
  /** Snapshot of what the app shows, read when a question is sent. */
  getContext: () => AskContext | null
}

export function ChatSidebar({ selection, series, onClearSelection, getContext, initialQuestion, proposalMode = false, active = true }: Props) {
  const sidebarId = useId()
  const sidebar = useRef<HTMLElement>(null)
  const drag = useRef<{ x: number; width: number } | null>(null)
  const [width, setWidth] = useState(340)
  const [maxWidth, setMaxWidth] = useState(720)
  const [resizing, setResizing] = useState(false)
  const minWidth = Math.min(280, maxWidth)
  const resize = (next: number) => setWidth(Math.min(maxWidth, Math.max(minWidth, next)))

  useEffect(() => {
    const parent = sidebar.current?.parentElement
    if (!parent) return
    const observer = new ResizeObserver(([entry]) => {
      if (!entry.contentRect.width) return
      const maximum = Math.max(180, Math.min(720, entry.contentRect.width - 320))
      setMaxWidth(maximum)
      setWidth(previous => Math.min(maximum, Math.max(Math.min(280, maximum), previous)))
    })
    observer.observe(parent)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!resizing) return
    const { cursor, userSelect } = document.body.style
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    return () => {
      document.body.style.cursor = cursor
      document.body.style.userSelect = userSelect
    }
  }, [resizing])

  const [text, setText] = useState('')
  const composer = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    const field = composer.current
    if (!field) return
    field.style.height = 'auto'
    field.style.height = `${field.scrollHeight}px`
  }, [text, width, active])
  const [talking, setTalking] = useState(false)
  const [liveStart, setLiveStart] = useState(0)
  const { messages, send, stop, confirm, addVoiceMessage, busy } = useAsk(liveFeed.watch(proposalMode ? harnessAskClient : askClient), getContext)
  const initialSent = useRef(false)
  useEffect(() => {
    if (!initialQuestion || initialSent.current) return
    const timer = setTimeout(() => { initialSent.current = true; void send(initialQuestion) }, 0)
    return () => clearTimeout(timer)
  }, [initialQuestion, send])
  const voice = useVoice(busy, (words) => setText(previous => previous ? `${previous} ${words}` : words))
  const live = useTalkLive({
    enabled: active,
    send: words => send(words, { fromVoice: true }), stop, onTranscript: addVoiceMessage,
    context: () => messages.slice(-10).map(m => `${m.role}: ${m.text}`).join('\n'),
    workspaceContext: () => {
      const context = getContext()
      if (!context) return ''
      const request = buildAskRequest(context, '', [], '')
      return JSON.stringify({ purpose: request.purpose, topic: request.topic, scope: request.scope, dataset: request.dataset })
    },
    onActiveChange: active => { if (active && !talking) setLiveStart(messages.length); setTalking(active); if (active) { voice.stopSpeaking(); voice.cancelRecording() } },
  })
  useEffect(() => {
    if (active) return
    voice.stopSpeaking()
    voice.cancelRecording()
    setResizing(false)
    drag.current = null
  }, [active, voice.stopSpeaking, voice.cancelRecording])
  const byId = new Map(series.map((s) => [s.id, s]))
  const subtopics = selection.subtopics.map((id) => byId.get(id)).filter((s) => s !== undefined)
  const empty = !selection.range && subtopics.length === 0

  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [messages, active])

  const submit = () => {
    if (busy || !text.trim()) return
    voice.stopSpeaking()
    voice.cancelRecording()
    send(text)
    setText('')
  }

  return (
    <aside className="chat" id={sidebarId} ref={sidebar} style={{ width }}>
      <div
        className="chat-resize-handle"
        role="separator"
        tabIndex={0}
        aria-label="Resize chat sidebar"
        aria-controls={sidebarId}
        aria-orientation="vertical"
        aria-valuemin={minWidth}
        aria-valuemax={maxWidth}
        aria-valuenow={Math.round(width)}
        aria-valuetext={`${Math.round(width)} pixels wide`}
        title="Drag to resize"
        data-resizing={resizing || undefined}
        onPointerDown={event => {
          if (event.button !== 0) return
          event.preventDefault()
          event.currentTarget.focus()
          event.currentTarget.setPointerCapture(event.pointerId)
          drag.current = { x: event.clientX, width }
          setResizing(true)
        }}
        onPointerMove={event => {
          if (drag.current) resize(drag.current.width + drag.current.x - event.clientX)
        }}
        onPointerUp={event => {
          drag.current = null
          setResizing(false)
          if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
        }}
        onPointerCancel={() => { drag.current = null; setResizing(false) }}
        onLostPointerCapture={() => { drag.current = null; setResizing(false) }}
        onDoubleClick={() => resize(340)}
        onKeyDown={event => {
          const step = event.shiftKey ? 40 : 10
          if (event.key === 'ArrowLeft') resize(width + step)
          else if (event.key === 'ArrowRight') resize(width - step)
          else if (event.key === 'Home') resize(minWidth)
          else if (event.key === 'End') resize(maxWidth)
          else return
          event.preventDefault()
        }}
      />
      <div className="messages" ref={scroller}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <BotIcon size={28} />
            Ask Sentibot anything
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} byId={byId} voice={voice} talking={talking} busy={busy} onConfirm={(approved) => void confirm(m.id, approved)} />
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
            {selection.range && <span className="chip-range">{formatRange(selection.range)} UTC</span>}
          </div>
          <button className="icon-btn" aria-label="Clear selection" onClick={onClearSelection}>
            <CloseIcon size={13} />
          </button>
        </div>
      )}
      <TalkLiveStrip live={live} messages={messages.slice(liveStart)} busy={busy} onConfirm={(id, approved) => void confirm(id, approved)} />
      <VoiceBar voice={voice} />
      <form
        className="input-row"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <textarea
          ref={composer}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={selection.range ? 'Ask about this time range' : 'Ask'}
          aria-label="Message"
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              submit()
            }
          }}
        />
        <div className="input-actions">
          <VoiceMic voice={voice} busy={busy || talking} />
          <TalkLiveButton live={live} disabled={busy || voice.recording !== 'idle'} />
          {busy ? (
            <button type="button" className="send" aria-label="Stop" onClick={stop}>
              <StopIcon size={16} />
            </button>
          ) : (
            <button type="submit" className="send" aria-label="Send">
              <SendIcon size={16} />
            </button>
          )}
        </div>
      </form>
    </aside>
  )
}

function Message({ message: m, byId, voice, talking, busy, onConfirm }: { message: ChatMessage; byId: Map<string, Series>; voice: Voice; talking: boolean; busy: boolean; onConfirm: (approved: boolean) => void }) {
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
      <ToolActivity steps={m.steps} />
      {m.text && (
        <p className="msg-text">
          {m.text}
          {m.status === 'streaming' && <span className="caret" />}
        </p>
      )}
      {!m.text && !m.steps.length && m.status === 'streaming' && <span className="typing" aria-label="Answering" />}
      {m.status === 'error' && <p className="msg-error">No response</p>}
      {m.cards.map((card, index) => <ResultCard key={index} card={card} />)}
      {m.confirmation && <ConfirmationCard value={m.confirmation} busy={busy} decide={onConfirm} />}
      {m.citations.map((p) => (
        <Citation key={p.id} post={p} color={byId.get(p.subtopic)?.color} />
      ))}
      {m.status === 'done' && m.text && <ReplyActions voice={voice} id={m.id} text={m.text} disabled={busy || talking} />}
    </div>
  )
}

function Citation({ post, color }: { post: EvidencePost; color?: string }) {
  return (
    <div className="cite" style={{ borderLeftColor: color }}>
      <div className="cite-head">
        <span className="handle">{post.handle}</span>
        <span className="cite-score" style={{ color: post.scored === false ? undefined : sentimentColor(post.sentiment) }}>
          {post.scored === false ? 'Unscored' : post.sentiment.toFixed(1)}
        </span>
      </div>
      <TranslatablePost text={post.text} className="cite-text" />
    </div>
  )
}
