import { useState } from 'react'
import { CloseIcon, PlusIcon } from '../components/icons'

interface Props {
  values: string[]
  onChange: (values: string[]) => void
  /** Optional one-click additions, e.g. suggested subtopics. */
  suggestions?: string[]
  autoFocus?: boolean
}

/** A list of removable chips plus an input that appends one. */
export function TokenField({ values, onChange, suggestions = [], autoFocus }: Props) {
  const [draft, setDraft] = useState('')
  const add = (value: string) => {
    const v = value.trim()
    if (v && !values.some((x) => x.toLowerCase() === v.toLowerCase())) onChange([...values, v])
    setDraft('')
  }
  const unused = suggestions.filter((s) => !values.some((v) => v.toLowerCase() === s.toLowerCase()))

  return (
    <div className="tokens">
      {values.map((v) => (
        <span key={v} className="token">
          {v}
          <button
            className="icon-btn"
            aria-label={`Remove ${v}`}
            onClick={() => onChange(values.filter((x) => x !== v))}
          >
            <CloseIcon size={12} />
          </button>
        </span>
      ))}
      <form
        className="token-add"
        onSubmit={(e) => {
          e.preventDefault()
          add(draft)
        }}
      >
        <input
          autoFocus={autoFocus}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Add"
          size={10}
        />
        <button type="submit" className="icon-btn" aria-label="Add" disabled={!draft.trim()}>
          <PlusIcon size={13} />
        </button>
      </form>
      {unused.map((s) => (
        <button key={s} className="token token-suggest" onClick={() => add(s)}>
          {s}
          <PlusIcon size={12} />
        </button>
      ))}
    </div>
  )
}
