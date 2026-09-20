import { useState } from 'react'
import { postExcerpt } from '../postPresentation'

interface Translation { translated_text: string; source_language: string; is_english: boolean }
const translations = new Map<string, Translation>()

interface Props { text: string; className: string; excerptLength?: number; onInteract?: () => void }

export function TranslatablePost({ text, className, excerptLength, onInteract }: Props) {
  // A changed post/text gets its own request state, even when the chart refreshes in place.
  return <PostText key={text} text={text} className={className} excerptLength={excerptLength} onInteract={onInteract} />
}

function PostText({ text, className, excerptLength, onInteract }: Props) {
  const [result, setResult] = useState<Translation | undefined>(() => translations.get(text))
  const [english, setEnglish] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const toggle = async () => {
    onInteract?.()
    if (result) { setEnglish(!english); return }
    setBusy(true); setError('')
    try {
      const response = await fetch('/api/posts/translate', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal: AbortSignal.timeout(75_000) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || 'Translation failed. Please try again.')
      if (typeof data.translated_text !== 'string' || typeof data.is_english !== 'boolean') throw new Error('Translation returned an unreadable response.')
      translations.set(text, data)
      if (translations.size > 512) translations.delete(translations.keys().next().value!)
      setResult(data); setEnglish(!data.is_english)
    } catch (error) {
      setError(error instanceof Error && error.name !== 'TimeoutError' ? error.message : 'Translation timed out. Please try again.')
    } finally { setBusy(false) }
  }
  const displayed = english && result ? result.translated_text : text
  return <>
    <p className={className} lang={english ? 'en' : undefined}>{excerptLength === undefined ? displayed : postExcerpt(displayed, excerptLength)}</p>
    {text.trim() && <div className="post-translation">
      {result?.is_english ? <span className="muted">Already in English</span> :
        <button type="button" className="translate-button" disabled={busy} onClick={() => void toggle()}>
          {busy ? 'Translating…' : english ? 'Show original' : 'Translate to English'}
        </button>}
      {english && result && <span className="muted">Translated from {result.source_language}</span>}
      {error && <span className="translation-error" role="alert">{error}</span>}
    </div>}
  </>
}
