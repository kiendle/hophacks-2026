# Hopkins hackathon 2026 — memetics track datasets

Staged at **`s3://calcifer-hot/hopkins-hackathon-2026/`** (AWS acct 940863285575, us-east-2;
~57 GB). **Public, no credentials** — `aws s3 cp --recursive --no-sign-request s3://calcifer-hot/hopkins-hackathon-2026/…`
or over HTTPS at `https://calcifer-hot.s3.us-east-2.amazonaws.com/hopkins-hackathon-2026/…`.
Local working copy + build code: `~/hackathon/` on calco-tower.
Assembled 2026-09-16, all four complete 2026-09-17.

| # | Dataset | Headline | Status |
|---|---|---|---|
| 1 | Twitter firehose, last month | 395.4M tweets · created 2026-08-17 → 09-17 · 55.7 GB | done |
| 2 | US-government tweets | 2,156,147 tweets · 856-handle roster · 1999 → 2026-08-24 | done |
| 3 | Parkour Instagram Niche | 451,301 posts · 10.77M engagement observations | done |
| 4 | TikTok 4.5B video records | our 269.6 GiB mirror (HF source gone 2026-09-19) | done |

**Start here:** `gov-tweets-unified.parquet` and `parkour-instagram-niche/parkour_post_observations.parquet`.

---

## 2. US-government tweets

Every tweet in our archives authored by an account tied to a **sitting member of the US federal
government**, plus the profile and follow-graph context around it.

### `gov-tweets-unified.parquet` — 2,156,147 tweets (263 MB)

One row per distinct tweet, deduped by `tweet_id` across three extraction passes, with the
roster metadata joined on:

`tweet_id, author_id, author_handle, person, role, tier, state, party, bioguide, account_kind,
author_display_name, author_followers, created_at, text, lang, like_count, retweet_count,
reply_count, quote_count, view_count, bookmark_count, in_reply_to_tweet_id, quoted_tweet_id,
conversation_id, is_quote_status, is_blue_verified, observed_at, source_corpus, n_corpora`

- 1999 → **2026-08-24** · 797 distinct accounts · 2,030,250 congressional / 125,778 executive
- Per year: 2018 49.7k · 2019 140k · 2020 179k · 2021 207k · 2022 206k · 2023 198k ·
  2024 269k · 2025 414k · 2026-to-date 365k
- Heaviest: @corybooker 60.7k · @realdonaldtrump 34.2k · @rokhanna 25.7k · @sensanders 23.7k ·
  @tedcruz 23.3k · @basedmikelee 22.6k · @repthomasmassie 16.7k · @randpaul 15.7k
- `source_corpus` = which archive the surviving row came from; `n_corpora` = how many archives
  independently held that tweet (2 = corroborated).

The three raw passes are kept alongside it:

| Prefix | Corpus scanned | Match key | Rows |
|---|---|---|---:|
| `gov-tweets/` | R2 `twitter` bucket — all 1,731 `tweets/` files, 5.03 TB / 40.3B tweets | `lower(author_handle)` ∈ roster | 1,404,017 |
| `gov-tweets-byid/` | same 5.03 TB | `author_id` ∈ 797 resolved ids | 2,142,197 |
| `gov-tweets-x-archive/` | S3 X-archive corpora, 894 GB / 2,124 files (a *different* upstream crawl) | `author_id` | 1,587,104 raw → 263,328 distinct |

The by-id pass matters: 738k R2 rows carry a valid `author_id` but a **blank** `author_handle`
(renamed/deleted accounts), so a handle-only filter silently misses a third of the corpus. The
X archive contributed 13,950 tweets nobody else had, and corroborated 249,378 — which is
good evidence R2 really is a near-superset of the S3 twitter archive.

### The roster — `roster.json`, `handles.txt` (856 handles)

| Tier | Handles | How it was built |
|---|---:|---|
| Congress — official | 505 | `unitedstates/congress-legislators` (`legislators-social-media.yaml`) — the taxpayer-funded accounts |
| Congress — campaign/personal | 297 | Wikidata: US Congress Bio ID (P1157) ↔ Twitter username (P2002), restricted to the 539 sitting members |
| Congress — name-matched | 5 | Members with no roster handle, matched by display name against R2 `profiles_current` |
| Executive / judicial | 49 | Wikidata P39 officeholders with no end date (cabinet, WH staff, agency heads, USTR, SCOTUS); where a position had several claimants the **latest start date wins** |

533 of 539 sitting members of Congress have ≥1 handle; the single unresolved member is
James C. Justice (WV). Each row carries `person / role / tier / state / party / bioguide /
account_kind / source`, so you can slice to official-only, campaign-only, chamber, or party.
Executive rows carry `term_start` and `stale_risk` (true when Wikidata's start date predates
2025-01-20 — i.e. the missing end-date may just be missing data; 2 rows).

Spot-checked against R2 bios: the cabinet/agency handles resolve to the right people
(@SecRubio, @SecScottBessent, @FBIDirectorKash, @secduffy, @EDSecMcMahon …).

### Context tables

| Object | Rows | What |
|---|---:|---|
| `gov-profiles_current.parquet` | 794 | Current profile per roster account (bio, followers, location, account age, verified) |
| `gov-profiles_history.parquet` | 5,665,858 | Monthly profile snapshots 2026-04 → 2026-08 — follower trajectories |
| `gov-bio_history.parquet` | 860 | Bio-change history |
| `gov-follow_edges.parquet` | 417,693 | Follow edges with ≥1 roster endpoint (14,559 are government→government) |

### Known limits

- **The R2 export stops at 2026-08-24** — nothing here covers the last three weeks. That gap is
  exactly what dataset #1 fills.
- R2 is a crawl, not a complete archive: coverage is dense for high-follower accounts and patchy
  for backbenchers. 60 roster handles have no presence in R2 at all.
- "Sitting member of the US government" = Congress + executive principals + agency heads. Not
  governors, not state legislatures, not institutional accounts (@WhiteHouse, @StateDept…).
  Adding an institutional tier is a small follow-up.
- Tweets *mentioning* government accounts are not included — that's another full-corpus pass with
  a much bigger output. Say the word if the track wants the diffusion side of the graph.

---

## 3. Parkour Instagram Niche — `parkour-instagram-niche/` (421 MB)

The largest parkour index we hold: Instagram niche campaign `cmp_RqY0FFNLiF-n`, seeded
2026-07-07 from 6 accounts and snapshotted daily since (71 dates). Rebuilt from the raw lake
(205,990 objects / 7.3 GB).

**451,301 distinct posts · 10,770,173 engagement observations · 15,003 member accounts ·
595,730 edges.** 250,621 posts were observed more than once, so the engagement trajectories are
genuine time series — diffusion curves inside one tightly-bounded subculture. Posts span
2011-08-13 → 2026-09-16; 3.89B likes / 51.8M comments / 24.4B video views.

Files and caveats: `parkour-instagram-niche/README.md`.

---

## 4. TikTok — 4.5B video records

- **The upstream HF repo is GONE.** `huggingface.co/datasets/kuben-developer/tiktok-videos-4b`
  returns 401 as of 2026-09-19 (account still live, repo deleted or made private). Do not
  point anyone at it.
- **Our mirror is the only known copy** and is now the canonical source:
  **`s3://calcifer-hot/tiktok/tiktok-videos-4b/`** — 30 objects / 269.6 GiB (27 shards
  `videos-00..26.parquet` + README/download.sh/download.log), egressed 2026-09-09, byte sizes
  verified against the tower copy. Public-read since 2026-09-19.
- Start with one shard: the 27 shards are split on a hash of the creator ID, so each ~10 GiB
  file is an unbiased ~167M-video sample.
  `aws s3 cp --no-sign-request s3://calcifer-hot/tiktok/tiktok-videos-4b/videos-00.parquet .`
- `download.sh` inside the mirror still references the dead HF URL — it is kept as a
  provenance artifact, not a working script.
- Nearby but different: `s3://calcifer-hot/tiktok/clickhouse-archive/` (the older 341M-video
  nooscope TikTok set) and `s3://calcifer-hot/tiktok/latent_embeddings.npy`

---

## 1. Twitter firehose — last month (`twitter-firehose-last-month/`)

Every tweet in our **live** X firehose created in the trailing month — the fresh,
high-velocity slice that R2 (which stops 2026-08-24) can't cover.

- **396 parquet files, 395,352,258 rows, 55.7 GB**, `created_at` 2026-08-17 00:00 → 2026-09-17
  14:32 UTC (zero rows before the window).
- **~363.5M distinct tweets.** The extra ~8.8% of rows are repeat observations of the same tweet
  at successive `version` timestamps — **engagement trajectories** (a popular tweet appears 20+
  times with rising like/view counts), not junk. Use `(id, version)`; group by `id` for a curve,
  or `argMax(metric, version)` for the latest state. Full schema + recipes in
  `twitter-firehose-last-month/README.md`.
- Global firehose (all languages, ~10M distinct authors). Reply/quote graph via
  `reply_to_status_id` / `quoting_id` / `conversation_id`.
- Pulled with `scripts/firehose_last_month.py` in 2.14 h; resumable, `--since <date>` to extend.
  (Transport note: the source's proxy strips the POST body — creds + SQL go in URL query params.)

---

## Distribution

The whole prefix is public-read (2026-09-17) — students pull directly, no credentials. Egress
note: the 55.7 GB firehose at ~$0.09/GB is the cost driver on public S3; move it to R2 (zero
egress) if traffic gets heavy.

## Reproducing

| Script | What |
|---|---|
| `scripts/modal/gov_tweets.py` | Handle-matched R2 scan + profile/bio/follow side tables (Modal fan-out, DuckDB) |
| `scripts/modal/gov_tweets_ids.py` | `author_id` passes over R2 and the S3 X-archive corpora |
| `scripts/build_roster.py`, `roster_final.py`, `roster_patch.py`, `wd*.sparql` | Roster construction |
| `scripts/gov_profiles.py` | R2 `profiles_current` regex sweep for government accounts |
| `scripts/parkour_build.py`, `parkour_build2.py` | parkour lake → parquet datasets |
| `scripts/unify2.py` | The dedupe + roster join that produces `gov-tweets-unified.parquet` |

Cost of the whole build: ~6.2 TB read across Modal fan-outs (161 + 161 + 37 + 4 containers),
zero R2 egress fees, roughly $20–30 of Modal CPU.
