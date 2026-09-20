import { useId, useLayoutEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { CloseIcon } from './icons'

interface Props {
  text: string
  english: boolean
  metadata?: ReactNode
  children: ReactNode
  onClose: () => void
}

export function PostReader({ text, english, metadata, children, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useLayoutEffect(() => {
    const node = dialog.current!
    node.showModal()
    return () => node.close()
  }, [])
  useLayoutEffect(() => { if (body.current) body.current.scrollTop = 0 }, [text])

  return createPortal(<dialog ref={dialog} className="post-reader" aria-labelledby={titleId} onClose={event => { if (!event.currentTarget.open) onClose() }}
    onKeyDown={event => {
      if (event.key !== 'Tab') return
      const controls = event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]')
      const first = controls[0], last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }}
    onClick={event => { if (event.target === event.currentTarget) dialog.current?.close() }}>
    <div className="post-reader-surface">
      <header className="post-reader-header">
        <div><h2 id={titleId}>Full post</h2><div className="post-reader-meta">{metadata}</div></div>
        <button type="button" className="post-reader-close" aria-label="Close full post" autoFocus onClick={() => dialog.current?.close()}><CloseIcon size={20} /></button>
      </header>
      <div ref={body} className="post-reader-body" tabIndex={0} role="region" aria-label="Full post text">
        <p lang={english ? 'en' : undefined}>{text}</p>
      </div>
      <footer className="post-reader-footer">{children}</footer>
    </div>
  </dialog>, document.body)
}
