import type { Post } from '../data/types'
import { formatCount, formatTime } from '../format'
import { sentimentColor } from '../sentimentColor'
import { HeartIcon, ReplyIcon, RetweetIcon } from './icons'
import { TranslatablePost } from './TranslatablePost'

const CARD_WIDTH = 280
const GAP = 18

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
  const width = Math.min(CARD_WIDTH, Math.max(180, bounds.width - 16))
  const flip = anchor.x + GAP + width > bounds.width
  const left = flip ? anchor.x - GAP - width : anchor.x + GAP
  const top = Math.max(8, Math.min(anchor.y - 80, bounds.height - 320))

  return (
    <div className="card post-hover-card" role="region" aria-label="Post preview"
      onPointerEnter={onEnter} onPointerLeave={onLeave} onFocus={onEnter}
      style={{ left: Math.max(0, left), top, width, maxHeight: Math.max(100, bounds.height - top - 8) }}>
      {onClose && <button type="button" className="post-preview-close" aria-label="Close post preview" onClick={onClose}>×</button>}
      <div className="card-head">
        <span className="handle">{post.handle}</span>
        <span className="muted">{post.timeKnown === false ? '—' : formatTime(post.time)}</span>
      </div>
      <TranslatablePost text={post.text} className="card-text" onInteract={onInteract} />
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
