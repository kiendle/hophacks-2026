# Twitter firehose — last month (`twitter-firehose-last-month/`)

Every tweet in our **live** X firehose **created in the trailing month**, pulled 2026-09-17 for
the Hopkins hackathon (memetics track). This is the fresh, high-velocity companion to the
historical archives — the slice for watching memes move in near-real-time.

## Contents

- **396 parquet files**, `tweets-000000.parquet … tweets-000395.parquet`, ~55.7 GB total (zstd).
- **395,352,258 rows** spanning `created_at` **2026-08-17 00:00:00 UTC → 2026-09-17 14:32 UTC**
  (exactly the trailing month; zero rows fall before the window).
- **~363.5M distinct tweets.** The extra ~8.8% of rows are **not duplicates to discard** — they
  are repeat observations of the same tweet at different `version` timestamps, i.e. **engagement
  trajectories.** A tweet can appear ~20+ times with a rising `like_count` / `views_count` (e.g.
  one popular tweet: 23 snapshots, likes 32,453 → 32,488). Treat `(id, version)` as the
  observation key.

### Two ways to use it
- **Engagement over time (recommended for memetics):** keep all rows; group by `id`, order by
  `version`, and you have a like/retweet/view curve per tweet — cascade and virality material.
- **One row per tweet:** `SELECT DISTINCT ON (id) * ORDER BY id, version DESC` (or `argMax` on
  each metric by `version`) for the latest state of each tweet.

## Schema

`id, author_id, body, created_at, like_count, reply_count, retweet_count, quote_count,
views_count, bookmarks_count, lang, source, reply_to_status_id, reply_to_user_id,
conversation_id, poll, embed, quoting_id, added_at, media, synced, embedded, version`

- `id` — tweet snowflake (string; `created_at ≈ (id>>22)+1288834974657` ms).
- `created_at` — tweet creation (the window key). `added_at` — when the crawler ingested it.
- `version` — observation timestamp; the trajectory axis.
- `reply_to_status_id` / `quoting_id` / `conversation_id` — the reply/quote graph.
- ~10M distinct authors; all languages (this is the global firehose, not English-only).

*Assembled 2026-09-17.*
