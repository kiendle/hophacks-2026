# Replay performance

The larger export contains 766,605 posts and 273,718 like updates. Splitting
network messages into 1,000-event frames kept messages manageable, but the UI
recalculated and published a chart for every frame. Completed historical
intervals were also regrouped repeatedly.

The client still ingests every event, validates message sequence, and retains
the exact activity used by historical queries and popups. Ordinary messages
share a chart publication every 100 ms; the first data frame, pause, resume, speed changes, errors,
and completion publish immediately. This changes presentation frequency, not
event delivery, playback speed, or data coverage.

Completed display intervals reuse their calculations, with invalidation for
changed buckets or activity. The active aggregation bucket processes new
arrivals incrementally. Live viewport calculation no longer renders the
previous playhead and rebuilds a historical snapshot before following the new
tick. Historical inspection and drag gestures retain their chosen coordinates.

The workspace renders its loading shell before replay metadata arrives and
keeps it visible until the first chart buckets exist. Empty, paused, and failed
starts have explicit states; a failed replay can reconnect with Try again.
The Python all-AI selection reuses the original event list and caches its exact
company membership and counts, avoiding repeated full-export selection passes.

## Measurements on the development machine

| Measurement | Before | After |
| --- | ---: | ---: |
| Late replay line calculation, median | 436.23 ms | 13.40 ms |
| Late replay line calculation, p95 | 489.53 ms | 33.46 ms |
| Browser animation-frame interval, p95 | 50.1 ms | 16.8 ms |
| Browser tasks longer than 50 ms in 30 seconds | 223 | 0 |

Engine measurements use the complete 1,040,323-event export: preload
1,016,323 events, then measure 24 deliveries of 1,000 events, showing OpenAI
and Anthropic with 12-hour points across the full history. The line metric
includes ingestion, aggregation, projection, and D3 path generation, not DOM
painting. A separate full-history analytic query remains expensive and is not
part of that metric. Browser runs use the same 30-second duration, viewport,
and 12-hours-per-second playback speed. Animation callbacks are not measured
paint FPS; other applications and memory pressure affect timings.

Full-data comparisons verified event counts, ordered activity-ID hashes,
historical snapshots, bucket/window statistics, chart points and popup fields.
No source events or displayed points were sampled out. A separate 56,000-event
scheduling scenario preserved its complete activity while reducing 24 message
publications to four.

The local detailed report and raw measurements are in
`harness/data/replay-performance/engine-summary.md`. Baseline source and browser
assets are retained under `harness/data/replay-performance/before/`.

Run `npm --prefix web test` and `npm --prefix web run build` for correctness and
build checks. `scripts/benchmark-replay.ts` provides reproducible before/after
engine and publication benchmarks; its full commands are in the local report.
`tests/replay-performance-browser.cjs` measures browser responsiveness, and
`tests/replay-complete-browser.cjs` verifies complete delivery and interactions.
`tests/replay-startup-browser.cjs` holds back replay metadata in the browser to
verify first-paint loading, gradual data, empty completion, and retry without
loading the real export.
