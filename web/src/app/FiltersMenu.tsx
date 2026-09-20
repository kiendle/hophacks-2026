import { useEffect, useRef, useState } from 'react'
import { TokenField } from './TokenField'

interface Props {
  terms: string[]
  onChange: (terms: string[]) => void
}

/** Top bar control: the search terms a post must match, editable mid-run. */
export function FiltersMenu({ terms, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="filters" ref={ref}>
      <button className={open ? 'chip-btn on' : 'chip-btn'} onClick={() => setOpen((v) => !v)}>
        Search filters
        <span className="count">{terms.length}</span>
      </button>
      {open && (
        <div className="popover">
          <TokenField values={terms} onChange={onChange} autoFocus />
        </div>
      )}
    </div>
  )
}
