import { useState } from 'react'
import { AmbientBubbles } from '../components/AmbientBubbles'
import { SendIcon } from '../components/icons'

export function Home({ onSubmit }: { onSubmit: (query: string) => void }) {
  const [query, setQuery] = useState('')
  const send = () => {
    const q = query.trim()
    if (q) onSubmit(q)
  }

  return (
    <div className="home">
      <AmbientBubbles />
      <div className="home-content">
      <h1 className="wordmark">Sentimeter</h1>
      <p className="tagline">“What movies are people talking about now?”</p>
      <form
        className="home-input"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Topic" />
        <button type="submit" className="send" aria-label="Start" disabled={!query.trim()}>
          <SendIcon size={16} />
        </button>
      </form>
      </div>
    </div>
  )
}
