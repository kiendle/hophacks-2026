# Morning Brief

Follow a few interests; get a 90-second podcast from the last 24 hours of saved posts.
The live, personal end of the project: `topic-analysis/` finds what spiked last month, Signal
(`jetstream-demo/`) shows one stream live, and this turns the stream into something you listen to.

```
uv run morning-brief/server.py        # then open http://127.0.0.1:5194
```

Copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY`. Both are
optional: without Claude the brief just reads out the top posts, and without ElevenLabs the page
uses the browser's voice. Set `BRIEF_AT=07:30` to have a brief recorded every morning.

A brief has a length, chosen on the page anywhere from **45 to 90 seconds**. The length is a
spoken-word budget given to Claude; the on-screen rundown stays complete at every length. The script
opens cold on the biggest story: no greeting, no sign-off.

The chat can pass `focus` and `exclude_terms` to `make_brief`, or narrate an exact sourced draft
with `record_brief`. `previous_brief_id` preserves omitted preferences and excludes earlier source
posts and story titles when choosing a new rundown. Each generated brief calculates a fresh window
ending at generation time; old or future posts never fill an empty window. A custom draft may use
selected X/Twitter evidence, with its actual dates and sources recorded in `source_context`.

Creating a recording does not send it anywhere. Chat delivery uses a human confirmation button
bound to that recording and Telegram destination, or the explicit **Send to Telegram** button.
An approved custom script is not rewritten or clipped to fit: scripts must fit the word budget,
and recordings run slightly faster if necessary to preserve the ending within the duration limit.

Telegram retries temporary connection failures. Completed replies and confirmation cards are
saved in `harness/state/telegram/outbox.json`; messages known not to have reached Telegram are
retried after reconnection without rerunning the agent or regenerating audio. An uncertain send
(such as a response timeout) is retained for review and is not replayed automatically. This
recovery queue never uploads recordings or bypasses delivery confirmation.

## How it works

| Step | Where | What |
|---|---|---|
| Collect | `collector.py` | Tails Bluesky Jetstream (posts only) and keeps posts matching an interest. Whole-word match on text plus link-card title/description; short all-caps terms (`AI`) are case-sensitive. |
| Replay | `collector.py` | Nobody waits 8 hours for a demo. Jetstream replays from a sequence cursor at ~15-25x real time per connection and cursors resume exactly, so the window is split into contiguous ranges and replayed over 12 connections: **8 hours in about 8 minutes**. Runs on first start, after downtime (only the gap), and for every new interest. |
| Rank | `briefing.py` | Asks the public AppView (`getPosts`, no auth) for current likes/reposts/quotes/replies and author names. Shortlists the most engaged top-level posts (two per author, near-duplicates folded) and the links shared by the most distinct people. |
| Write | `briefing.py` | One Claude call with structured output: picks and merges stories, writes the on-screen rundown and a script for the ear. Posts are passed as untrusted data and are the only source; every story cites the posts behind it. |
| Voice | `briefing.py` | ElevenLabs, one continuous MP3 of the whole brief: no chapters, no gaps. |

State lives in `morning-brief/data/` (gitignored): `interests.json`, `posts.jsonl`, `state.json`
(the resume cursor) and `briefs/<id>/`. Delete the folder to start over.

## Limits worth knowing

- Automatic current-story selection uses collected Bluesky posts. Archived X/Twitter evidence can
  support a custom script, but must keep its historical dates and cannot be labelled current news.
- Engagement is each post's current total, not its growth inside the window.
- Jetstream's replay buffer is short: measured 2026-09-19, the oldest replayable event was 36.7 hours
  old, and older cursors are refused with `CursorTooOld ... below lookback floor`. Longer windows need
  the collector left running. Older posts are only reachable per account (`com.atproto.repo.listRecords`).
- Matching is lexical. "AI" also catches news roundups that mention AI once; Claude's story selection
  is what filters those out, so the no-key fallback looks noisier than the real thing.
