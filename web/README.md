# Sentimeter UI

The interface from `origin/UI`, connected to the existing harness. The landing
page, chart layout, colors, and sidebar are retained.

## Run

From the repository root:

```powershell
npm --prefix web install
npm --prefix web run build
uv run harness/ui_server.py
```

Open http://127.0.0.1:5196. For frontend development, also run
`npm --prefix web run dev`; Vite proxies `/api` to port 5196.

The assistant uses the installed, authenticated Claude CLI. Jev reads
`TYPESAFE_API_KEY`; voice reads `ELEVENLABS_API_KEY` from the root `.env`.
Missing services produce a visible error, not mock results.

## Default dataset

A topic such as **AI** uses the Jev-classified **X/Twitter export**, never a
background Bluesky scan or a new Jev scoring call. Supplied ZIPs are extracted
under `harness/data/classified/`. The `processed-streams-20260920T063350Z` package
is the active version: 766,605 post events, 273,718 like events and 27 company
categories. It includes every event from `processed-streams-20260920T034707Z`,
including its updated classifications. Earlier packages are retained for rollback.
All manifest file hashes were verified. This is a partial classified export,
not the entire Twitter firehose.

The dashboard uses the UI branch's event-stream playback through `/api/replay`.
Its clock sends only the saved posts and like changes reached so far, starting
at 4 archive hours per second. Pause, resume, speed changes and restart operate
on that clock; historical inspection uses the received events only. There is no
full-dataset download before drawing the chart and no new Jev scoring.
`preview_keywords` searches the same export; `classified_sentiment` reports
saved sentiment for exact selected timestamps. Opening like balances count once,
later changes affect their own periods, and multi-company posts retain all labels.

To rebuild the local chart data without model calls:

```powershell
uv run web/scripts/prepare_demo.py harness/data/classified/processed-streams-20260920T063350Z
```

The generated `web/data/demo.json.gz` and source Parquet files remain local.
Preparation uses bounded batches and writes valid JSON with one event per line,
so runtime loaders can read large exports without building an oversized string.
The Python server serves the same replay messages consumed by the UI branch.
The UI retains every event while coalescing redraws and reusing completed chart
calculations. See [replay performance](REPLAY_PERFORMANCE.md) for measurements
and integrity checks.

Explicitly asking for live/real-time data or Bluesky enables that source. The
assistant follows the same archive-first rule. A spoken conversation is separate
from the dataset choice and does not enable live data collection.

## Assistant features

- Live tool activity: operation, reason, duration, result, and expandable request details.
- Keyword previews, linked/full example posts, analysis charts, and project drafts.
- Confirm/Cancel cards that use the backend's confirmation records.
- **Auto-play replies** is off on every visit, even when an old saved preference is on.
  Enabling it explicitly reads new replies; **Stop audio** stops the current playback.
- Microphone input: click to record, click again to finish. The transcript goes
  into the Ask field for review before sending. Cancel or Escape releases the mic.
- **Talk live** opens a cloud-orb voice view powered by the ElevenLabs realtime
  agent over WebRTC, with transcript, mute and end controls. Spoken replies are
  enabled only inside an explicitly started live session. See [live voice setup](src/live/README.md).
- Audio brief cards with progress, a manual audio player, and **Send to Telegram**.
  The assistant can also send the existing recording when explicitly asked.
  Telegram delivery sends now for later listening, not on a new recurring schedule.
  New briefs default to 90 seconds and the recorded audio is capped at 90 seconds.

Audio briefs use the existing Morning Brief service on port 5194, through the
same-origin proxy on 5196. Start `uv run harness/signal_server.py` if it is not
already running. Only one process may own the collector data folder.

Telegram delivery uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS` from
the root `.env`. Open the bot and send `/start` first. If more than one chat is
allowed, set `TELEGRAM_DEFAULT_CHAT_ID` to one of them. Successful sends are
recorded locally so repeated clicks or chat requests do not duplicate delivery.
An uncertain upload is not automatically retried. Existing Telegram `/schedule`
commands remain available, with new recordings limited to 90 seconds.

`VITE_ASK_URL` overrides the Ask endpoint. `VITE_DEMO_MODE=true` explicitly enables
the original synthetic charts and mock chat. `?stream=<speed>` is fixture replay.

## Verify

```powershell
npm --prefix web run build
npm --prefix web run lint
uv run --with aiohttp --with duckdb --with pytz --with 'mcp>=2' python harness/tests/test_ui_server.py
npx -y tsx web/tests/live/algorithms.test.ts
npx -y tsx web/tests/live/session.test.ts
```

`web/tests/integration-browser.cjs` exercises the UI against mocked tool, audio,
preview, brief and confirmation responses, plus a fake microphone. Run from the
repo root with the app running and Playwright available. `PLAYWRIGHT_MODULE` can
point to its package directory and `UI_BROWSER_PATH` to an installed Chromium
binary. The test generates its own audio and makes no paid model/voice calls.

## UI branch integration (75bf5e2)

The current homepage/setup flow and harness remain the default. The updated charts
support 4h, 12h and 1d points, a trailing 24h trend, display toggles, adaptive axes,
and improved timeline gestures. Recents can be deleted; both sidebars resize.
Selected ranges and subtopics are sent to text and voice context; the range chip
shows dates without a ?Focus:? prefix.

Saved sentiment probabilities map to `5 * (1 + P(positive) - P(negative))`.
Within a period, each published post contributes a baseline of one, plus
`log(1 + max(0, net likes))`. Insufficient-evidence results do not contribute
to sentiment means; their post events remain part of volume counts.

Event replay is the default archive dashboard. The package is not included in
Git; the three tests requiring it explicitly skip when it is absent. The local
package is installed, so those tests run here. Verify the running server with
`npx --prefix web tsx web/tests/stream.integration.ts ws://127.0.0.1:5196/api/replay`.
