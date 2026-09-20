import { MARGIN } from '../layout'
import './chart-loading.css'

const GRID_LINES = 5
const BARS = 17

interface Props {
  status: 'loading' | 'waiting' | 'paused' | 'complete' | 'error'
  live?: boolean
  error?: string
  onRetry?: () => void
}

/** Keep the chart's space visible from the first render through its first data frame. */
export function ChartLoading({ status, live, error, onRetry }: Props) {
  const busy = status === 'loading' || status === 'waiting'
  const title = {
    loading: 'Loading your chart',
    waiting: 'Waiting for the first posts',
    paused: live ? 'Tracking paused' : 'Replay paused',
    complete: 'No posts to show',
    error: 'Could not load your chart',
  }[status]
  const description = {
    loading: live ? 'Connecting to your tracker. New posts will appear here.' : 'Preparing saved posts. The chart will fill in as they arrive.',
    waiting: 'Your chart will update as matching posts arrive.',
    paused: live ? 'Start tracking to collect matching posts.' : 'Press Play to continue loading posts.',
    complete: 'No chart data matched these search filters. Try changing the filters.',
    error: error || 'The connection was interrupted. Try loading the chart again.',
  }[status]
  return (
    <div className="chart chart-loading" role={status === 'error' ? 'alert' : 'status'} aria-label={title}
      data-busy={busy}
      style={{ padding: `${MARGIN.top}px ${MARGIN.right}px ${MARGIN.bottom}px ${MARGIN.left}px` }}>
      <div className="chart-loading-plot">
        {Array.from({ length: GRID_LINES }, (_, i) => <span key={i} className="chart-loading-grid" aria-hidden="true" />)}
        {busy && <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <path pathLength={1} d="M0 38 C 12 30, 20 34, 30 36 S 48 50, 60 44 S 82 30, 100 34" />
          <path pathLength={1} d="M0 62 C 10 66, 22 58, 32 64 S 52 74, 64 66 S 86 60, 100 63" />
        </svg>}
        <div className="chart-loading-message">
          <h2>{title}</h2>
          <p>{description}</p>
          {status === 'error' && onRetry && <button className="chart-loading-retry" onClick={onRetry}>Try again</button>}
        </div>
      </div>
      <div className="chart-loading-bars" aria-hidden="true">
        {Array.from({ length: BARS }, (_, i) => <span key={i} style={{ animationDelay: `${i * 70}ms` }} />)}
      </div>
    </div>
  )
}
