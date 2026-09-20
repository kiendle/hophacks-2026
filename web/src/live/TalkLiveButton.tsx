import './live.css'
import { EndLiveIcon, TalkLiveIcon } from './icons'
import type { TalkLive } from './useTalkLive'
import { useEffect, useRef, useState } from 'react'
import { CloudOrb } from './CloudOrb'
import type { ChatMessage } from '../ask'
import { ToolActivity, ResultCard, ConfirmationCard } from '../components/ToolCards'

/** The composer button and voice dialog share one live connection. */
const ON_HINT = 'End the live talk'
const OFF_HINT = 'Talk live: ask questions with your voice.'

export function TalkLiveButton({ live, disabled }: { live: TalkLive; disabled?: boolean }) {
  if (!live.available) return null
  // While the browser is asking for the microphone the button already looks on, so nobody presses
  // it twice, and pressing it again ends the talk instead of starting a second one.
  const on = live.active || live.opening
  return (
    <button
      type="button"
      className={on ? 'talk-live-btn talk-live-on' : 'talk-live-btn'}
      aria-pressed={on}
      aria-busy={live.opening}
      aria-label={on ? ON_HINT : 'Talk live'}
      title={on ? ON_HINT : OFF_HINT}
      disabled={disabled && !on}
      onClick={live.toggle}
    >
      {live.active ? <EndLiveIcon size={17} /> : <TalkLiveIcon size={17} />}
    </button>
  )
}

export function TalkLiveStrip({ live, messages = [], busy = false, onConfirm }: { live: TalkLive; messages?: ChatMessage[]; busy?: boolean; onConfirm?: (id: string, approved: boolean) => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const transcript = useRef<HTMLDivElement>(null)
  const [captions, setCaptions] = useState(false)
  const [minimizedSession, setMinimizedSession] = useState<number | null>(null)
  const expanded = minimizedSession !== live.session
  const open = live.opening || live.active || Boolean(live.note)
  const activity = messages.filter(message => message.role === 'assistant' && !message.id.startsWith('voice-'))
  useEffect(() => {
    const node = dialog.current
    if (!node) return
    if (open && expanded && !node.open) node.showModal()
    else if ((!open || !expanded) && node.open) node.close()
  }, [open, expanded])
  useEffect(() => {
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: 'smooth' })
  }, [live.transcript, captions])
  const label = live.note ? 'Connection interrupted' : live.opening ? 'Connecting…' : live.muted && live.state !== 'Speaking' ? 'Microphone muted' : live.state === 'Listening' ? "I'm listening" : live.state === 'Thinking' ? 'Thinking…' : live.state === 'Speaking' ? 'Speaking' : 'Live voice'
  if (!live.available) return null
  return (
    <>
      {open && !expanded && <button className="live-return" type="button" onClick={() => setMinimizedSession(null)}><TalkLiveIcon /> Live voice · {label}</button>}
      <dialog ref={dialog} className="live-dialog" aria-labelledby="live-title" onCancel={event => { event.preventDefault(); live.stop() }}>
        <div className="live-room">
          <header className="live-room-header">
            <button type="button" className="live-circle" title="Back to chat" aria-label="Back to chat" onClick={() => setMinimizedSession(live.session)}>
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>
            </button>
            <div><span className="live-brand">Sentimeter</span><h2 id="live-title">Live voice</h2></div>
            <button type="button" className="live-circle" title={captions ? 'Hide transcript' : 'Show transcript'} aria-label={captions ? 'Hide transcript' : 'Show transcript'} aria-pressed={captions} onClick={() => setCaptions(value => !value)}>
              <svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="4" /><path d="M10 9H8a3 3 0 0 0 0 6h2m8-6h-2a3 3 0 0 0 0 6h2" /></svg>
            </button>
          </header>
          <div className="live-room-body">
          <div className="live-voice-column">
          <div className="live-room-center">
            <CloudOrb level={live.level} state={live.state} />
            <p className="live-room-state" role="status">{label}</p>
            {live.note && <div className="live-room-error"><p role="alert">{live.note}</p><button type="button" onClick={live.start}>Reconnect</button></div>}
          </div>
          {captions && <div className="live-transcript" ref={transcript} role="log" aria-label="Live transcript">
            {live.transcript.length ? live.transcript.map(row => <p key={row.id} data-role={row.role}><span>{row.role === 'user' ? 'You' : 'Sentimeter'}</span>{row.text}</p>) : <p className="live-transcript-empty">Your conversation will appear here.</p>}
          </div>}
          </div>
          <section className="live-workspace" aria-label="Live searches and results">
            <header className="live-workspace-heading"><h3>Searches & results</h3><span role="status">{busy ? 'Working' : activity.length ? 'Up to date' : 'Ready'}</span></header>
            <div className="live-workspace-scroll">
              {!activity.length && <p className="live-workspace-empty">Ask me to search or explore your data. You’ll see the tools and results here as I work.</p>}
              {[...activity].reverse().map(message => <article className="live-workspace-turn" key={message.id}>
                <ToolActivity steps={message.steps} />
                {message.status === 'streaming' && !message.steps.length && <p role="status" className="live-workspace-empty">Checking your workspace…</p>}
                {message.status === 'error' && <p role="alert">This request failed. You can ask me to try again.</p>}
                {message.status === 'stopped' && <p className="live-workspace-empty">Request interrupted.</p>}
                {message.text && <details className="live-workspace-answer"><summary>Written answer</summary><p>{message.text}</p></details>}
                {message.cards.map((card, index) => <ResultCard key={index} card={card} />)}
                {message.confirmation && onConfirm && <ConfirmationCard value={message.confirmation} busy={busy} decide={approved => onConfirm(message.id, approved)} />}
              </article>)}
            </div>
          </section>
          </div>
          <footer className="live-room-footer">
            <p>{live.muted ? 'Unmute whenever you’re ready.' : 'Speak naturally. You can interrupt anytime.'}</p>
            <div className="live-room-actions">
              <button type="button" className="live-circle live-mute" aria-pressed={live.muted} aria-label={live.muted ? 'Unmute microphone' : 'Mute microphone'} disabled={!live.active} onClick={live.toggleMute}>
                <svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8" />{live.muted && <path d="m3 3 18 18" />}</svg>
              </button>
              <button type="button" className="live-circle live-hangup" aria-label="End live voice" title="End live voice" onClick={live.stop}>
                <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6" /></svg>
              </button>
            </div>
          </footer>
        </div>
      </dialog>
    </>
  )
}
