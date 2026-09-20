interface Props {
  /** True while the view's right edge sits on the newest data. */
  live: boolean
  onGoLive: () => void
}

/** A pulsing red dot while tracking the live edge; a gray, clickable one when behind it. */
export function LiveButton({ live, onGoLive }: Props) {
  return (
    <button
      className={live ? 'live-btn live-on' : 'live-btn'}
      onClick={onGoLive}
      disabled={live}
      aria-label={live ? 'Live' : 'Go live'}
    >
      <span className="live-dot" />
    </button>
  )
}
