import type { Voice } from './useVoice'
import { useEffect, useId, useRef, useState, type SVGProps } from 'react'
import './voice.css'

type IconProps = SVGProps<SVGSVGElement>
export function SpeakerIcon(props: IconProps) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}><path d="m11 5-6 4H2v6h3l6 4V5Z" /><path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14" /></svg>
}
function MicIcon() {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8" /></svg>
}

export function VoiceMic({ voice, busy }: { voice: Voice; busy: boolean }) {
  const active = voice.recording === 'recording'
  const working = voice.recording === 'permission' || voice.recording === 'transcribing'
  const label = active ? 'Stop dictation' : 'Dictate a message'
  return <button type="button" className="icon-btn voice-mic" aria-label={label} title={voice.available ? label : voice.unavailable}
    aria-pressed={active} disabled={!voice.available || busy || working} onClick={() => void voice.record()}><MicIcon /></button>
}

export function VoiceBar({ voice }: { voice: Voice }) {
  const recording = voice.recording !== 'idle'
  const status = voice.error || (voice.recording === 'permission' ? 'Allow microphone access in your browser.'
    : voice.recording === 'recording' ? 'Listening… press the microphone to finish.'
    : voice.recording === 'transcribing' ? 'Transcribing…' : voice.speechState)
  if (!recording && !status && !voice.speakingId) return null
  return <div className="voice-controls">
    <div className="voice-control-row">
      {voice.speakingId && <button type="button" className="voice-action" onClick={voice.stopSpeaking}>Stop audio</button>}
      {recording && <button type="button" className="voice-action" onClick={voice.cancelRecording}>Cancel recording</button>}
    </div>
    {(status || !voice.available) && <p className={voice.error ? 'voice-status voice-error' : 'voice-status'} role="status">{status || voice.unavailable}</p>}
  </div>
}

export function ReplyActions({ voice, id, text, disabled }: { voice: Voice; id: string; text: string; disabled: boolean }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const action = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const active = voice.speakingId === id
  useEffect(() => {
    if (!open) return
    action.current?.focus()
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', escape)
    }
  }, [open])
  return <div className="reply-actions" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }}>
    <button type="button" className="icon-btn reply-more" ref={trigger} aria-label="More actions" title="More actions"
      aria-expanded={open} aria-controls={menuId} onClick={() => setOpen(value => !value)}>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" />
      </svg>
    </button>
    {open && <div className="reply-menu" id={menuId}>
      <button type="button" ref={action} className="reply-menu-item" disabled={!active && (!voice.available || disabled || voice.recording !== 'idle')}
        title={voice.available ? undefined : voice.unavailable}
        onClick={() => {
          if (active) voice.stopSpeaking(); else void voice.speak(id, text)
          setOpen(false)
          trigger.current?.focus()
        }}>
        <SpeakerIcon />{active ? 'Stop reading' : 'Read aloud'}
      </button>
    </div>}
  </div>
}
