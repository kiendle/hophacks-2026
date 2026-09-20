import type { Session } from './session'
import { useEffect, useRef, useState } from 'react'
import { RecentItem, type RecentChanges } from './RecentItem'
import { PlusIcon } from '../components/icons'

interface Props {
  recents: Session[]
  activeId: string | null
  onNew: () => void
  onHome: () => void
  onOpen: (session: Session) => void
  onDelete: (id: string) => void
  onEdit: (id: string, changes: RecentChanges) => void
}

export function Sidebar({ recents, activeId, onNew, onHome, onOpen, onDelete, onEdit }: Props) {
  const [showArchived, setShowArchived] = useState(false)
  const [width, setWidth] = useState(240)
  const [maximum, setMaximum] = useState(420)
  const [resizing, setResizing] = useState(false)
  const drag = useRef<{ x: number; width: number } | null>(null)
  const resize = (next: number) => setWidth(Math.max(180, Math.min(maximum, next)))
  useEffect(() => {
    const fit = () => {
      const max = Math.max(180, Math.min(420, window.innerWidth * 0.3))
      setMaximum(max)
      setWidth(previous => Math.min(previous, max))
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [])
  useEffect(() => {
    if (!resizing) return
    const { cursor, userSelect } = document.body.style
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    return () => { document.body.style.cursor = cursor; document.body.style.userSelect = userSelect }
  }, [resizing])
  const endDrag = () => { drag.current = null; setResizing(false) }
  return (
    <nav className="sidebar" id="recents-sidebar" style={{ width, position: 'relative' }}>
      <div className="chat-resize-handle recents-resize-handle" role="separator" tabIndex={0}
        aria-label="Resize Recents sidebar" aria-controls="recents-sidebar" aria-orientation="vertical"
        aria-valuemin={180} aria-valuemax={Math.round(maximum)} aria-valuenow={Math.round(width)}
        title="Drag to resize" data-resizing={resizing || undefined}
        onPointerDown={event => {
          if (event.button !== 0) return
          event.preventDefault()
          event.currentTarget.focus()
          event.currentTarget.setPointerCapture(event.pointerId)
          drag.current = { x: event.clientX, width }
          setResizing(true)
        }}
        onPointerMove={event => { if (drag.current) resize(drag.current.width + event.clientX - drag.current.x) }}
        onPointerUp={event => {
          endDrag()
          if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
        }}
        onPointerCancel={endDrag} onLostPointerCapture={endDrag}
        onDoubleClick={() => resize(240)}
        onKeyDown={event => {
          const step = event.shiftKey ? 40 : 10
          if (event.key === 'ArrowLeft') resize(width - step)
          else if (event.key === 'ArrowRight') resize(width + step)
          else if (event.key === 'Home') resize(180)
          else if (event.key === 'End') resize(maximum)
          else return
          event.preventDefault()
        }}
      />
      <button className="brand" onClick={onHome}>
        Sentimeter
      </button>
      <button className="new-btn" onClick={onNew}>
        <PlusIcon size={15} />
        New
      </button>
      <div className="recent-groups">
        {(['Pinned', 'Recents'] as const).map(group => {
          const items = recents.filter(s => !s.archived && (group === 'Pinned' ? s.pinned : !s.pinned))
          if (!items.length) return null
          return <section key={group} aria-label={group}>
            <div className="recents-label">{group}</div>
            <ul className="recents">{items.map(s => <RecentItem key={s.id} session={s} active={s.id === activeId} onOpen={onOpen} onDelete={onDelete} onEdit={onEdit} />)}</ul>
          </section>
        })}
        {recents.some(s => s.archived) && <>
          <button className="archived-toggle" aria-expanded={showArchived} onClick={() => setShowArchived(value => !value)}>Archived chats</button>
          {showArchived && <ul className="recents">{recents.filter(s => s.archived).map(s => <RecentItem key={s.id} session={s} active={s.id === activeId} onOpen={onOpen} onDelete={onDelete} onEdit={onEdit} />)}</ul>}
        </>}
      </div>
    </nav>
  )
}
