import type { Session } from './session'
import { PlusIcon } from '../components/icons'

interface Props {
  recents: Session[]
  activeId: string | null
  onNew: () => void
  onHome: () => void
  onOpen: (session: Session) => void
}

export function Sidebar({ recents, activeId, onNew, onHome, onOpen }: Props) {
  return (
    <nav className="sidebar">
      <button className="brand" onClick={onHome}>
        Sentimeter
      </button>
      <button className="new-btn" onClick={onNew}>
        <PlusIcon size={15} />
        New
      </button>
      {recents.length > 0 && <div className="recents-label">Recents</div>}
      <ul className="recents">
        {recents.map((s) => (
          <li key={s.id}>
            <button className={s.id === activeId ? 'recent on' : 'recent'} onClick={() => onOpen(s)}>
              {s.query}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}
