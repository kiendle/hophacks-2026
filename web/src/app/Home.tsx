import { useState } from 'react'
import { AmbientBubbles } from '../components/AmbientBubbles'
import { SendIcon } from '../components/icons'

export function Home({ onSubmit, onCreateAutomation }: { onSubmit: (query: string) => void; onCreateAutomation: (question: string) => void }) {
  const [automation, setAutomation] = useState(false)
  const [query, setQuery] = useState('')
  const [error, setError] = useState(false)
  const send = () => {
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
    onSubmit(q)
  }

  return (
    <div className="home">
      <AmbientBubbles />
      <div className="home-content">
      <h1 className="wordmark">Sentimeter</h1>
      <p className="tagline">{automation ? 'What would you like to track?' : 'Explore a keyword'}</p>
      <div className="home-modes" aria-label="Choose a workflow">
        <button type="button" aria-pressed={!automation} onClick={() => { setAutomation(false); setError(false) }}>Explore data</button>
        <button type="button" aria-pressed={automation} onClick={() => { setAutomation(true); setError(false) }}>Build automation</button>
      </div>
      <form
        className="home-input"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <input autoFocus value={query} maxLength={automation ? 4000 : 80} placeholder={automation ? 'Analyze sentiment about AI companies' : 'Keywords'} onChange={(e) => { setQuery(e.target.value); setError(false) }} aria-label={automation ? 'Automation goal' : 'Keywords'} aria-invalid={error} aria-describedby={error ? 'keyword-error' : undefined} />
        <button type="submit" className="send" aria-label="Start" disabled={!query.trim()}>
          <SendIcon size={16} />
        </button>
      </form>
      {error && <p id="keyword-error" role="alert" className="msg-error">{automation ? 'Describe your goal in up to 4,000 characters.' : 'Enter a keyword.'}</p>}
      </div>
    </div>
  )
}
