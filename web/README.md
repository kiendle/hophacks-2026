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

A topic such as **AI** uses the saved **X/Twitter archive**, never a background
Bluesky scan. The dashboard reads `harness/data/prepared/sample.parquet`, the
existing deduplicated 1% archive sample. It selects up to 150 matching posts,
prioritizing the chosen subtopics and spreading the selection across days,
then scores them with Jev. Results are cached by filters and dataset version.
The status line reports sample sizes; these are not full-archive totals.

Charts use four-hour buckets, a 0–10 sentiment scale, and saved engagement totals.
Historical playback moves through publication dates; it does not reconstruct how
engagement accumulated. Archive results do not poll continuously. Simultaneous
identical requests share one job instead of rejecting each other.

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
