# Sentimeter demo

The app opens the fixed **AI companies** query and streams the prepared dataset
through a local WebSocket at `/api/replay`. Both `npm run dev` and
`npm run preview` include the replay service.

```bash
npm install
npm run dev
```

The prepared package is `data/demo.json.gz` (113,381 posts and 39,060 like
updates from `processed-streams-20260920T034707Z`). It is kept locally and ignored
by Git; a fresh clone needs the dataset prepared separately:

```bash
uv run scripts/prepare_demo.py /path/to/processed-streams-20260920T034707Z
```

Alternatively install DuckDB in a Python environment and run the script there.
It verifies the source checksums, preserves IDs, classifications, confidence and
observation times, then writes a deterministic gzip file. No model calls occur.
This export includes all events from `processed-streams-20260920T015207Z` plus
newly processed events and five revised rows, so it replaces the older export
rather than being appended. After preparing a newer package, reload the browser
to start a replay with that version; an existing replay keeps its original data.

## Controls

- Playback starts automatically at 14,400×: four historical hours per real second.
  Events are delivered in 100 ms batches; buckets update while they fill.
- The compact speed selector supports one, four, or twelve hours per second.
- Play/pause controls actual delivery. After completion, Play starts again.
- The existing red dot returns to the newest delivered time after scrubbing.
- While dragging the timeline, its scale and viewport hold still without pausing
  delivery. Resizing at the live edge resumes following at the chosen width on
  release. Go Live preserves that width; restarting resets it.
- New starts a fresh replay. Company pills change visibility without restarting.
- The line view starts with OpenAI and Anthropic; the bubble view also includes
  Google, xAI and Nvidia. Other companies are available under +25.

## Statistics

For each accepted company, a post's score is
`5 * (1 + P(positive) - P(negative))`. Insufficient-evidence choices count toward
volume but not mean sentiment or spread. Other choices retain their full
probability-derived score. Model confidence is preserved, not used as a weight.

Sentiment measures content published or receiving engagement during the period.
For each distinct post, combine its activity in that interval:

- `P = 1` if the post is published in the interval, otherwise `0`.
- `L = initial likes first received in the interval + subsequent signed deltas`.
- `w = P + ln(1 + max(0, L))`.
- `sentiment = sum(w * score) / sum(w)` for scored posts with positive weight.

An opening balance contributes initial popularity once, at its own event time,
not retroactively at publication. If publication and opening occur in the same
bucket, their combined weight is `1 + ln(1 + initial likes)`. Older posts receive
only the log weight of their activity in the current period, without another
publication bonus or their old lifetime balance. Negative deltas reduce the
period's net contribution, never invert its sentiment or create negative weights.
Per-post totals are grouped before logarithmic damping, so splitting a count
update into several deliveries cannot increase its influence.

Sentiment spread is the weighted population standard deviation of post scores
within the trailing 24 hours: `sqrt(sum(w * score²) / sum(w) - mean²)`.
Bubble area is proportional to distinct active posts, including older posts
receiving positive net engagement. The bubble post counter uses that same count;
the line volume band and global post counter still count new publications only.
Bubbles with fewer than 30 scored active posts are faded.
The axes are fixed at sentiment 0–10 and spread 0–5.

Post counts include RT-prefixed records and overlapping company classifications.
Global counters count each source event once. Repeated delivery of the same event
ID is ignored. Historical windows select exact activity timestamps in
`[start, end)`, not a uniform-within-bucket approximation. Scored-post counts
remain actual counts, not sums of engagement weights.

Opening balances initialize cumulative popularity, but only subsequent signed
deltas count as new engagement in the traction counter. Missing cumulative
baselines stay unknown; observed deltas can still contribute without a parent
publication because like events carry their own text and company grades.
When a post has multiple content versions within a period, its latest received
version/grade supplies its text and score for that period.

The hover tweet has the largest raw period like contribution, including its
opening balance if first received there. Its heart counter shows this period
value, not lifetime likes; its sentiment remains the individual tweet's score.
Closed line buckets and their hover selections are fixed at the bucket end.
Later likes affect only later buckets. Rolling bubble windows regroup the
original events by post across the entire window, rather than adding already
log-damped bucket weights. Historical trails never use events beyond their end.

Log weighting intentionally dampens viral outliers; the top tweet can still
disagree with the weighted mean. Likes measure attention to sentiment-bearing
content, not proof of endorsement. Count updates mark when changes were observed,
not individual click times. Missing engagement coverage limits these statistics
to recorded engagement, not all audience opinion. Reply/repost/quote counts are
unavailable and are not invented.

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

With the dev server running, `npm run test:stream` checks all 152,441 events over
the actual socket, including pause/resume, speed changes, completion and restart.

Tests cover the clock, exact partial windows, duplicate and multi-company events,
negative likes, initial popularity, missing parents/baselines, split updates,
weighted means and spreads, frozen historical points and popup rankings, exact
bubble windows, independent source counts and raw-event reference calculations,
and live-resize/follow behavior.
