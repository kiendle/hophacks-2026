import { suggestSubtopics } from '../data/terms'
import { TokenField } from './TokenField'

interface Props {
  query: string
  terms: string[]
  subtopics: string[]
  onTermsChange: (terms: string[]) => void
  onSubtopicsChange: (subtopics: string[]) => void
  onConfirm: () => void
}

/** Sits in the empty chart area until the run is confirmed. */
export function SetupPanel({ query, terms, subtopics, onTermsChange, onSubtopicsChange, onConfirm }: Props) {
  return (
    <div className="setup">
      <div className="setup-row">
        <span className="setup-label">Search filters</span>
        <TokenField values={terms} onChange={onTermsChange} />
      </div>
      <div className="setup-row">
        <span className="setup-label">Subtopics</span>
        <TokenField
          values={subtopics}
          onChange={onSubtopicsChange}
          suggestions={suggestSubtopics(query)}
          autoFocus
        />
      </div>
      <div className="setup-actions">
        <button className="primary-btn" onClick={onConfirm} disabled={!terms.length || !subtopics.length}>
          Start
        </button>
      </div>
    </div>
  )
}
