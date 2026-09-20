import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { ChatMessage } from './useAsk'

const size = (node: HTMLDivElement) => `${node.clientWidth}:${node.clientHeight}:${node.scrollHeight}`

/** Follow replies until the reader scrolls away; also track non-message layout changes. */
export function useChatScroll(messages: ChatMessage[], active: boolean) {
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const following = useRef(true)
  const lastUser = useRef<string | undefined>(undefined)
  const dimensions = useRef('')
  const [showLatest, setShowLatest] = useState(false)

  const measure = useCallback(() => {
    const node = scroller.current
    if (!node || !node.clientHeight) return
    dimensions.current = size(node)
    const atBottom = node.scrollHeight - node.clientHeight - node.scrollTop <= 32
    following.current = atBottom
    setShowLatest(!atBottom)
  }, [])

  const scrollToLatest = useCallback(() => {
    following.current = true
    const node = scroller.current
    if (node?.clientHeight) {
      node.scrollTop = node.scrollHeight
      dimensions.current = size(node)
    }
    setShowLatest(false)
  }, [])

  useLayoutEffect(() => {
    const userId = messages.findLast(message => message.role === 'user')?.id
    if (userId !== lastUser.current) following.current = true
    lastUser.current = userId
    if (!active) return
    if (following.current) scrollToLatest()
    else measure()
  }, [messages, active, measure, scrollToLatest])

  useLayoutEffect(() => {
    const node = scroller.current
    const body = content.current
    if (!active || !node || !body) return
    const observer = new ResizeObserver(() => {
      if (following.current) scrollToLatest()
      else measure()
    })
    observer.observe(node)
    observer.observe(body)
    return () => observer.disconnect()
  }, [active, measure, scrollToLatest])

  const onScroll = () => {
    const node = scroller.current
    // Layout can fire scroll before ResizeObserver. Preserve the previous follow
    // intent until the observer reconciles the new viewport/content dimensions.
    if (node && dimensions.current === size(node)) measure()
  }

  return { scroller, content, showLatest, onScroll, scrollToLatest }
}
