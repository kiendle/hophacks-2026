import { useState } from 'react'
import { AmbientBubbles } from '../components/AmbientBubbles'
import { SendIcon } from '../components/icons'
import { Logo } from '../components/Logo'
import { automationRequest } from '../data/automationSource'

export function Home({ onSubmit, onCreateAutomation }: { onSubmit: (query: string) => void; onCreateAutomation: (question: string) => void }) {
  const [automation, setAutomation] = useState(false)
  const [query, setQuery] = useState('')
  const [error, setError] = useState(false)
  const [notice, setNotice] = useState('')
  const [checking, setChecking] = useState(false)
  const send = async () => {
    const q = query.trim()
    if (automation) {
      if (!q || q.length > 4000) { setError(true); return }
      onCreateAutomation(q)
      return
    }
    if (!/^[\p{L}\p{N}][\p{L}\p{N}\p{M}_+#.&-]{0,79}$/u.test(q)) {
      setError(true)
      return
    }
    setError(false)
    setChecking(true); setNotice('')
    try {
      const data = await automationRequest<{ taxonomy: { categories: { id: string; label: string; products?: string[] }[] } }>('historical')
      const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '')
      const known = new Set(['ai', 'artificialintelligence', ...data.taxonomy.categories.flatMap(c => [c.id, c.label, ...(c.products ?? [])]).map(normalize)])
      if (!known.has(normalize(q))) {
        setNotice('Historical data covers AI company sentiment only. Try AI or OpenAI, or choose Live data for this topic.')
        return
      }
      onSubmit(q)
    } catch (error) { setNotice(error instanceof Error ? error.message : String(error)) }
    finally { setChecking(false) }
  }

  return (
    <div className="home">
      <AmbientBubbles />
      <div className="home-content">
      <h1 className="wordmark"><Logo variant="ascii" /></h1>
      <p className="tagline">{automation ? 'What would you like to track live?' : '“How do people feel about AI companies?”'}</p>
      <div className="home-modes" aria-label="Choose a workflow">
        <button type="button" aria-pressed={!automation} onClick={() => { setAutomation(false); setError(false); setNotice('') }}>Historical data</button>
        <button type="button" aria-pressed={automation} onClick={() => { setAutomation(true); setError(false); setNotice('') }}>Live data</button>
      </div>
      <form
        className="home-input"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <input autoFocus value={query} maxLength={automation ? 4000 : 80} placeholder={automation ? 'Analyze sentiment about AI companies' : 'Keyword'} onChange={(e) => { setQuery(e.target.value); setError(false) }} aria-label={automation ? 'Automation goal' : 'Keyword'} aria-invalid={error} aria-describedby={error ? 'keyword-error' : undefined} />
        <button type="submit" className="send" aria-label="Start" disabled={checking || !query.trim()}>
          <SendIcon size={16} />
        </button>
      </form>
      {error && <p id="keyword-error" role="alert" className="msg-error">{automation ? 'Describe your goal in up to 4,000 characters.' : 'Enter a keyword.'}</p>}
      {notice && <p role="alert" className="msg-error">{notice}</p>}
      </div>
    </div>
  )
}
