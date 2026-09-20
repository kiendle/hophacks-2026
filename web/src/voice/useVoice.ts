import { useCallback, useEffect, useRef, useState } from 'react'
import { speechChunks, voiceError } from './speech'

const RECORD_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
type RecordingState = 'idle' | 'permission' | 'recording' | 'transcribing'

export function useVoice(busy: boolean, onTranscript: (text: string) => void) {
  const [available, setAvailable] = useState(false)
  const [unavailable, setUnavailable] = useState('Checking voice availability…')
  const [speakingId, setSpeakingId] = useState<string | null>(null)
  const [speechState, setSpeechState] = useState('')
  const [recording, setRecording] = useState<RecordingState>('idle')
  const [error, setError] = useState('')
  const speech = useRef<AbortController | null>(null)
  const capture = useRef<AbortController | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const microphone = useRef<MediaStream | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const transcript = useRef(onTranscript)
  useEffect(() => { transcript.current = onTranscript }, [onTranscript])

  useEffect(() => {
    const controller = new AbortController()
    void fetch('/api/voice/status', { signal: controller.signal }).then(async response => {
      if (!response.ok) throw await voiceError(response, 'Voice is unavailable right now.')
      const result = await response.json()
      if (controller.signal.aborted) return
      setAvailable(result.available === true)
      setUnavailable(result.available ? '' : result.reason || 'Voice is not configured on this server.')
    }).catch(error => { if (!controller.signal.aborted) setUnavailable(error.message) })
    return () => controller.abort()
  }, [])

  const stopSpeaking = useCallback(() => {
    speech.current?.abort()
    speech.current = null
    setSpeakingId(null)
    setSpeechState('')
  }, [])

  const speak = useCallback(async (id: string, text: string) => {
    stopSpeaking()
    setError('')
    const parts = speechChunks(text)
    if (!parts.length) return
    const controller = new AbortController()
    speech.current = controller
    setSpeakingId(id)
    try {
      for (const part of parts) {
        setSpeechState('Preparing audio…')
        const response = await fetch('/api/voice/speak', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: part }), signal: controller.signal,
        })
        if (!response.ok) throw await voiceError(response, 'This answer could not be read aloud.')
        const blob = await response.blob()
        if (controller.signal.aborted) return
        if (!blob.size || !blob.type.startsWith('audio/')) throw new Error('The voice service returned no playable audio.')
        const url = URL.createObjectURL(blob)
        const audio = new Audio(url)
        try {
          await new Promise<void>((resolve, reject) => {
            const finish = (error?: Error) => {
              audio.onended = audio.onerror = null
              controller.signal.removeEventListener('abort', aborted)
              audio.pause()
              if (error) reject(error); else resolve()
            }
            const aborted = () => finish()
            controller.signal.addEventListener('abort', aborted, { once: true })
            audio.onended = () => finish()
            audio.onerror = () => finish(new Error('The audio could not play. Try Read aloud again.'))
            audio.onplaying = () => { if (!controller.signal.aborted) setSpeechState('Reading aloud') }
            void audio.play().catch(() => finish(new Error('Playback was blocked. Choose More actions, then Read aloud on the reply to try again.')))
          })
        } finally {
          audio.onplaying = null
          audio.pause()
          audio.removeAttribute('src')
          audio.load()
          URL.revokeObjectURL(url)
        }
        if (controller.signal.aborted) return
      }
    } catch (error) {
      if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Unable to read the answer aloud.')
    } finally {
      if (speech.current === controller) {
        speech.current = null
        setSpeakingId(null)
        setSpeechState('')
      }
    }
  }, [stopSpeaking])

  const releaseMic = useCallback(() => {
    microphone.current?.getTracks().forEach(track => track.stop())
    microphone.current = null
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }, [])

  const cancelRecording = useCallback(() => {
    capture.current?.abort()
    capture.current = null
    if (recorder.current?.state === 'recording') recorder.current.stop()
    recorder.current = null
    releaseMic()
    setRecording('idle')
  }, [releaseMic])

  const record = useCallback(async () => {
    if (recorder.current?.state === 'recording') { recorder.current.stop(); return }
    if (capture.current || busy) return
    stopSpeaking()
    setError('')
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('Microphone recording is not supported in this browser. You can still type and listen to replies.')
      return
    }
    const controller = new AbortController()
    capture.current = controller
    setRecording('permission')
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (controller.signal.aborted) { media.getTracks().forEach(track => track.stop()); return }
      microphone.current = media
      const mimeType = RECORD_TYPES.find(type => MediaRecorder.isTypeSupported(type))
      const active = new MediaRecorder(media, mimeType ? { mimeType } : undefined)
      recorder.current = active
      const pieces: Blob[] = []
      let bytes = 0
      active.ondataavailable = event => {
        if (!event.data.size) return
        pieces.push(event.data)
        bytes += event.data.size
        if (bytes > 9 * 1024 * 1024 && active.state === 'recording') active.stop()
      }
      active.onerror = () => {
        cancelRecording()
        setError('The microphone stopped unexpectedly. Please try again.')
      }
      active.onstop = () => {
        releaseMic()
        recorder.current = null
        if (controller.signal.aborted) return
        setRecording('transcribing')
        void (async () => {
          try {
            const clip = new Blob(pieces, { type: active.mimeType || pieces[0]?.type || 'audio/webm' })
            const response = await fetch('/api/voice/transcribe', {
              method: 'POST', headers: { 'Content-Type': clip.type }, body: clip, signal: controller.signal,
            })
            if (!response.ok) throw await voiceError(response, 'We could not transcribe that recording.')
            const result = await response.json()
            if (!controller.signal.aborted && typeof result.text === 'string') transcript.current(result.text)
          } catch (error) {
            if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Transcription failed.')
          } finally {
            if (capture.current === controller) { capture.current = null; setRecording('idle') }
          }
        })()
      }
      active.start(1000)
      setRecording('recording')
      timer.current = setTimeout(() => { if (active.state === 'recording') active.stop() }, 120_000)
    } catch (error) {
      releaseMic()
      if (!controller.signal.aborted) {
        setError(error instanceof DOMException && error.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow the microphone in your browser to use voice input.'
          : 'The microphone could not start. Check that a microphone is connected.')
        capture.current = null
        setRecording('idle')
      }
    }
  }, [busy, stopSpeaking, releaseMic, cancelRecording])

  useEffect(() => { if (busy) speech.current?.abort() }, [busy])
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { stopSpeaking(); cancelRecording() } }
    window.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('keydown', escape)
      speech.current?.abort()
      capture.current?.abort()
      if (recorder.current?.state === 'recording') recorder.current.stop()
      releaseMic()
    }
  }, [stopSpeaking, cancelRecording, releaseMic])

  return { available, unavailable, speakingId, speechState,
    speak, stopSpeaking, recording, record, cancelRecording, error }
}

export type Voice = ReturnType<typeof useVoice>
