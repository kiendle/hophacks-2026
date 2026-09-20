const HOUR = 60 * 60 * 1000

/** Time bucket size. Matches the engagement snapshot interval. */
export const BUCKET_MS = 4 * HOUR

/** Display intervals are independent of the underlying stream and playback speed. */
export const LINE_INTERVALS = [
  { value: 4 * HOUR, label: '4h' },
  { value: 12 * HOUR, label: '12h' },
  { value: 24 * HOUR, label: '1d' },
] as const
export const DEFAULT_LINE_INTERVAL = 12 * HOUR
export const TREND_WINDOW_MS = 24 * HOUR

/** Paul Tol's muted scheme: nine colorblind-safe categorical colors. */
export const SERIES_COLORS = [
  '#CC6677',
  '#332288',
  '#88CCEE',
  '#117733',
  '#44AA99',
  '#DDCC77',
  '#882255',
  '#999933',
  '#AA4499',
]
