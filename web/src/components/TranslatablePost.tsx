import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { postExcerpt } from '../postPresentation'
import { PostReader } from './PostReader'

interface Translation { translated_text: string; source_language: string; is_english: boolean }
const translations = new Map<string, Translation>()

interface Props { text: string; className: string; excerptLength?: number; onInteract?: () => void; readerMetadata?: ReactNode; readerFooter?: ReactNode }

export function TranslatablePost(props: Props) {
  // A changed post/text gets its own request state, even when the chart refreshes in place.
  return <PostText key={props.text} {...props} />
}

function PostText({ text, className, excerptLength, onInteract, readerMetadata, readerFooter }: Props) {
  const [result, setResult] = useState<Translation | undefined>(() => translations.get(text))
  const [english, setEnglish] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [clipped, setClipped] = useState(false)
  const paragraph = useRef<HTMLParagraphElement>(null)
  const textId = useId()
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
  const excerpt = excerptLength === undefined ? displayed : postExcerpt(displayed, excerptLength)
  useLayoutEffect(() => {
    if (paragraph.current) paragraph.current.scrollTop = 0
  }, [displayed, expanded])
  useLayoutEffect(() => {
    const node = paragraph.current
    if (!node || excerptLength === undefined || expanded) return
    const measure = () => setClipped(node.scrollHeight > node.clientHeight + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [displayed, excerptLength, expanded])
  const translation = text.trim() && <div className="post-translation">
      {result?.is_english ? <span className="muted">Already in English</span> :
        <button type="button" className="translate-button" disabled={busy} onClick={() => void toggle()}>
          {busy ? 'Translating…' : english ? 'Show original' : 'Translate to English'}
        </button>}
      {english && result && <span className="muted">Translated from {result.source_language}</span>}
      {error && <span className="translation-error" role="alert">{error}</span>}
    </div>
  return <>
    <p ref={paragraph} id={textId} className={className} lang={english ? 'en' : undefined}>{excerpt}</p>
    {excerptLength !== undefined && (expanded || clipped || excerpt !== displayed) && <button type="button" className="post-expand-button" aria-label="Show full text" aria-haspopup="dialog" onClick={() => { onInteract?.(); setExpanded(true) }}>
      Show full text <span aria-hidden="true">↗</span>
    </button>}
    {translation}
    {expanded && <PostReader text={displayed} english={english} metadata={readerMetadata} onClose={() => setExpanded(false)}>
      {translation}{readerFooter}
    </PostReader>}
  </>
}
