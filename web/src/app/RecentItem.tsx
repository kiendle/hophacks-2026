import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { shareSessionUrl, type Session } from './session'

export type RecentChanges = Partial<Pick<Session, 'title' | 'pinned' | 'archived'>>
type Action = 'share' | 'rename' | 'pin' | 'archive' | 'delete'
const paths: Record<Action, ReactNode> = {
  share: <path d="M12 16V3m-4 4 4-4 4 4M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />,
  rename: <path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14v6Z" />,
  pin: <path d="m15 3 6 6-4 1-4 5-1 4-7-7 4-1 5-4 1-4ZM8 16l-5 5" />,
  archive: <><rect x="3" y="3" width="18" height="5" rx="1" /><path d="M5 8v12h14V8M10 12h4" /></>,
  delete: <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />,
}
function ActionIcon({ kind }: { kind: Action }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind]}</svg>
}

export function RecentItem({ session, active, onOpen, onDelete, onEdit }: {
  session: Session; active: boolean; onOpen: (session: Session) => void
  onDelete: (id: string) => void; onEdit: (id: string, changes: RecentChanges) => void
}) {
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null)
  const [dialog, setDialog] = useState<'rename' | 'share' | null>(null)
  const [title, setTitle] = useState('')
  const [copied, setCopied] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const modal = useRef<HTMLDialogElement>(null)
  const id = useId()
  const label = session.title || session.query
  const close = () => { setPosition(null); trigger.current?.focus() }

  useEffect(() => {
    if (!position) return
    menu.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target) && !trigger.current?.contains(event.target)) setPosition(null)
    }
    const dismiss = () => setPosition(null)
    document.addEventListener('pointerdown', outside)
    window.addEventListener('resize', dismiss)
    window.addEventListener('scroll', dismiss, true)
    return () => {
      document.removeEventListener('pointerdown', outside)
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('scroll', dismiss, true)
    }
  }, [position])
  useEffect(() => {
    if (dialog) modal.current?.showModal()
  }, [dialog])

  const choose = (kind: Action) => {
    close()
    if (kind === 'rename') { setTitle(label); setDialog('rename') }
    if (kind === 'share') { setCopied(''); setDialog('share') }
    if (kind === 'pin') onEdit(session.id, { pinned: !session.pinned })
    if (kind === 'archive') onEdit(session.id, { archived: !session.archived })
    if (kind === 'delete') onDelete(session.id)
  }
  return <li className={`recent-row${position ? ' menu-open' : ''}`}>
    <button className={active ? 'recent on' : 'recent'} onClick={() => onOpen(session)} title={label}>{label}</button>
    {session.pinned && <span className="recent-pin" title="Pinned"><ActionIcon kind="pin" /></span>}
    <button type="button" className="icon-btn recent-more" ref={trigger} title="More actions"
      aria-label={`More actions for ${label}`} aria-haspopup="menu" aria-expanded={Boolean(position)} aria-controls={position ? id : undefined}
      onClick={() => {
        if (position) { close(); return }
        const rect = trigger.current!.getBoundingClientRect()
        setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - 228)),
          top: Math.max(8, rect.top >= 278 ? rect.top - 270 : Math.min(rect.bottom + 6, window.innerHeight - 278)) })
      }}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" /></svg>
    </button>
    {position && createPortal(<div className="recent-menu" ref={menu} id={id} role="menu" aria-label={`Actions for ${label}`} style={position}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget) && event.relatedTarget !== trigger.current) setPosition(null) }}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); close() }
        const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button')]
        const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
        let next = current
        if (event.key === 'ArrowDown') next = (current + 1) % buttons.length
        else if (event.key === 'ArrowUp') next = (current - 1 + buttons.length) % buttons.length
        else if (event.key === 'Home') next = 0
        else if (event.key === 'End') next = buttons.length - 1
        else return
        event.preventDefault(); buttons[next]?.focus()
      }}>
      {(['share', 'rename', 'pin', 'archive', 'delete'] as Action[]).map(kind => <button key={kind} type="button" role="menuitem"
        className={`recent-menu-action${kind === 'delete' ? ' danger' : ''}${kind === 'pin' ? ' divided' : ''}`} onClick={() => choose(kind)}>
        <ActionIcon kind={kind} />
        {kind === 'share' ? 'Share' : kind === 'rename' ? 'Rename' : kind === 'pin' ? session.pinned ? 'Unpin chat' : 'Pin chat' : kind === 'archive' ? session.archived ? 'Unarchive' : 'Archive' : 'Delete'}
      </button>)}
    </div>, document.body)}
    {dialog && createPortal(<dialog ref={modal} className="recent-dialog" aria-labelledby={`${id}-title`}
      onClose={() => { setDialog(null); trigger.current?.focus() }} onClick={event => { if (event.target === event.currentTarget) modal.current?.close() }}>
      <form onSubmit={event => { event.preventDefault(); if (dialog === 'rename' && title.trim()) { onEdit(session.id, { title: title.trim() }); modal.current?.close() } }}>
        <h2 id={`${id}-title`}>{dialog === 'rename' ? 'Rename chat' : 'Share workspace'}</h2>
        {dialog === 'rename' ? <input aria-label="Chat name" value={title} maxLength={120} onChange={event => setTitle(event.target.value)} autoFocus />
          : <><p>This link shares the topic and search settings. It does not include your chat messages.</p>
            <input aria-label="Workspace link" readOnly value={shareSessionUrl(session)} onFocus={event => event.currentTarget.select()} />
            <p role="status">{copied}</p></>}
        <div className="recent-dialog-actions"><button type="button" onClick={() => modal.current?.close()}>Close</button>
          {dialog === 'rename' ? <button type="submit" disabled={!title.trim()}>Save</button> : <button type="button" onClick={async () => {
            try { await navigator.clipboard.writeText(shareSessionUrl(session)); setCopied('Link copied') }
            catch { setCopied('Select the link above and copy it.') }
          }}>Copy link</button>}
        </div>
      </form>
    </dialog>, document.body)}
  </li>
}
