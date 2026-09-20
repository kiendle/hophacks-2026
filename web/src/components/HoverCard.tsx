import { useLayoutEffect, useRef, useState } from 'react'
import type { Post } from '../data/types'
import { formatCount, formatTime } from '../format'
import { sentimentColor } from '../sentimentColor'
import { HeartIcon, ReplyIcon, RetweetIcon } from './icons'
import { displayHandle, hoverCardLayout } from '../postPresentation'
import { TranslatablePost } from './TranslatablePost'

interface Props {
  post: Post
  anchor: { x: number; y: number }
  bounds: { width: number; height: number }
  onEnter?: () => void
  onLeave?: () => void
  onInteract?: () => void
  onClose?: () => void
}

export function HoverCard({ post, anchor, bounds, onEnter, onLeave, onInteract, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState(0)
  const layout = hoverCardLayout(anchor, bounds, height)
  const handle = displayHandle(post.handle)
  useLayoutEffect(() => {
    const card = ref.current
    if (!card) return
    const measure = () => setHeight(card.getBoundingClientRect().height)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(card)
    return () => observer.disconnect()
  }, [])

  return (
    <div ref={ref} className="card tweet-card post-hover-card" role="region" aria-label="Post preview"
      onPointerEnter={onEnter} onPointerLeave={onLeave} onFocus={onEnter}
      style={layout}>
      {onClose && <button type="button" className="post-preview-close" aria-label="Close post preview" onClick={onClose}>×</button>}
      <div className="card-head">
        {handle && <span className="handle">{handle}</span>}
        <span className="muted">{post.timeKnown === false ? '—' : formatTime(post.time)}</span>
      </div>
      <TranslatablePost text={post.text} className="card-text" excerptLength={400} onInteract={onInteract} />
      <div className="card-foot">
        <div className="card-stats muted">
          <span aria-label={post.periodLikes === undefined ? 'Likes' : 'Likes received in this period'}>
            <HeartIcon size={13} />
            {post.periodLikes !== undefined ? formatCount(post.periodLikes) : post.likesKnown === false ? '—' : formatCount(post.likes)}
          </span>
          {post.otherMetricsKnown !== false && <span>
            <ReplyIcon size={13} />
            {formatCount(post.replies)}
          </span>}
          {post.otherMetricsKnown !== false && <span>
            <RetweetIcon size={13} />
            {formatCount(post.retweets)}
          </span>}
        </div>
        <SentimentGauge value={post.sentiment} />
      </div>
    </div>
  )
}

/** A 0 to 10 track with a marker, plus the score. */
export function SentimentGauge({ value }: { value: number }) {
  if (!Number.isFinite(value)) return <span className="muted">—</span>
  const color = sentimentColor(value)
  return (
    <div className="gauge">
      <div className="gauge-track">
        <span className="gauge-mark" style={{ left: `${value * 10}%`, background: color }} />
      </div>
      <span className="gauge-value" style={{ color }}>
        {value.toFixed(1)}
      </span>
    </div>
  )
}
