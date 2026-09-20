# Sentimeter demo

The app opens the fixed **AI companies** query and streams the prepared dataset
through a local WebSocket at `/api/replay`. Both `npm run dev` and
`npm run preview` include the replay service.

```bash
npm install
npm run dev
```

The prepared package is `data/demo.json.gz` (50,377 posts and 16,510 like
updates). To regenerate it from the original export:

```bash
uv run scripts/prepare_demo.py /path/to/processed-streams-20260920T015207Z
```

Alternatively install DuckDB in a Python environment and run the script there.
It verifies the source checksums, preserves IDs, classifications, confidence and
observation times, then writes a deterministic gzip file. No model calls occur.

## Controls

- Playback starts automatically at 14,400×: four historical hours per real second.
  Events are delivered in 100 ms batches; buckets update while they fill.
- The compact speed selector supports one, four, or twelve hours per second.
- Play/pause controls actual delivery. After completion, Play starts again.
- The existing red dot returns to the newest delivered time after scrubbing.
- New starts a fresh replay. Company pills change visibility without restarting.
- The line view starts with OpenAI and Anthropic; the bubble view also includes
  Google, xAI and Nvidia. Other companies are available under +25.

## Statistics

For each accepted company, a post's score is
`5 * (1 + P(positive) - P(negative))`. Insufficient-evidence choices count toward
volume but not mean sentiment or spread. Other choices retain their full
probability-derived score. Model confidence is preserved, not used as a weight.

Sentiment weights each scored post by `w = 1 + ln(1 + known likes)` and uses
`sum(w * score) / sum(w)`. Unknown like balances receive baseline weight 1,
without being marked as measured zero likes. Nonnegative balances are used for
weighting; signed updates still remain intact in engagement counters. Reply,
repost and quote counts are unavailable and are not invented.

Sentiment spread is the weighted population standard deviation of post scores
within the trailing 24 hours: `sqrt(sum(w * score²) / sum(w) - mean²)`.
Bubble area is
proportional to post volume; bubbles with fewer than 30 scored posts are faded.
The axes are fixed at sentiment 0–10 and spread 0–5.

Post counts include RT-prefixed records and overlapping company classifications.
Global counters count each source event once. Repeated delivery of the same event
ID is ignored. Historical windows select exact publication timestamps in
`[start, end)`, not a uniform-within-bucket approximation. Scored-post counts
remain actual counts, not sums of engagement weights.

Opening like balances initialize known engagement. Only subsequent signed deltas
contribute to the window's engagement updates. Both opening balances and later
deltas affect sentiment weights for the original post. Later engagement can
therefore revise an older line bucket when viewed at a newer inspection time.
Missing baselines remain unknown. Hover evidence remains the most-liked post
using engagement known by the inspection time; its individual sentiment is not
the bucket average. Unavailable metrics are not displayed as measured zeros.
Bubble windows and trails each use engagement known before their own window
end, capped by the snapshot time, so later likes never leak into historical views.

Log weighting intentionally dampens viral outliers; the top tweet can still
disagree with the weighted mean. Comparisons with equal, square-root, and linear
weighting on real conflicting buckets informed this choice. Missing engagement
coverage limits these statistics to recorded engagement, not all audience opinion.

The source package is partial, and captured text/grades can postdate publication.
The demo replays pre-enriched publication/count events, not original classifier
arrival times. Generated run IDs identify only the local simulated session.
The chatbot remains unchanged and is outside this integration.

## Verification

```bash
npm test
npm run build
npm run lint
```

With the dev server running, `npm run test:stream` checks all 66,887 events over
the actual socket, including pause/resume, speed changes, completion and restart.

Tests cover the clock, exact partial windows, duplicate and multi-company events,
negative likes, missing parents/baselines, weighted means and spreads, historical
inspection and bubble trails, independent source counts, and an independent
event-time weighted reference calculation over the real package.
