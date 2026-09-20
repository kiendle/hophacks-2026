const HOUR = 60 * 60 * 1000

/** Time bucket size. Matches the engagement snapshot interval. */
export const BUCKET_MS = import.meta.env.VITE_DEMO_MODE === 'true' ? 4 * HOUR : 5 * 60 * 1000

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
