# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Build the prepared tables the harness reads (DESIGN.md 6.4). One full scan of the firehose.

Run: python -m uv run harness/prepare_data.py [--force] [repo_root]

Outputs into harness/data/prepared/:
  sample.parquet        1% of tweet ids (hash(id) % 100 = 0), latest version per id -> month-wide estimates
  congress.parquet      congress tweets with chamber normalized, undated rows dropped
  daily_totals.parquet  the already-computed daily denominators, as parquet

Resumable: an output that exists is skipped unless --force.
"""
import sys
import time
from pathlib import Path

import duckdb

SAMPLE_COLUMNS = ["id", "author_id", "body", "created_at", "lang", "like_count", "retweet_count",
                  "views_count", "reply_to_status_id", "quoting_id"]
CHAMBER_NORM_SQL = (
    "CASE WHEN lower(chamber) IN ('house', 'representative') THEN 'House'"
    " WHEN lower(chamber) IN ('senate', 'senator') THEN 'Senate'"
    " WHEN chamber IS NOT NULL THEN 'Executive' END")

force = "--force" in sys.argv
positional = [a for a in sys.argv[1:] if not a.startswith("-")]
root = Path(positional[0] if positional else Path(__file__).resolve().parent.parent).resolve()
out = root / "harness" / "data" / "prepared"
tmp = root / "harness" / "data" / ".tmp"
out.mkdir(parents=True, exist_ok=True)
tmp.mkdir(parents=True, exist_ok=True)


def path(*parts):
    return str(root.joinpath(*parts)).replace("\\", "/").replace("'", "''")


con = duckdb.connect()
con.execute("SET TimeZone='UTC'")            # a bare date literal shifts by 4 h on this laptop otherwise
con.execute("SET memory_limit='4GB'")
con.execute("SET threads=4")
con.execute("SET preserve_insertion_order=false")
con.execute("SET max_temp_directory_size='40GB'")
con.execute(f"SET temp_directory='{path('harness', 'data', '.tmp')}'")
print(f"duckdb {duckdb.__version__}, root {root}", flush=True)


def build(name, sql, before=None):
    target = out / f"{name}.parquet"
    if target.exists() and not force:
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{path('harness', 'data', 'prepared', name + '.parquet')}')").fetchone()[0]
        print(f"{name}: exists, {rows:,} rows - skipped (--force to rebuild)", flush=True)
        return
    started = time.monotonic()
    if before:
        before()
    destination = path("harness", "data", "prepared", name + ".parquet")
    con.execute(f"COPY ({sql}) TO '{destination}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{destination}')").fetchone()[0]
    size = (out / f"{name}.parquet").stat().st_size / 1e6
    print(f"{name}: {rows:,} rows, {size:,.0f} MB, {time.monotonic() - started:.0f}s", flush=True)


def report_undated():
    total, undated = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE created_at IS NULL)
            FROM read_parquet('{path('congress-tweets', 'congress-tweets-unified.parquet')}')""").fetchone()
    print(f"congress: dropping {undated} of {total:,} rows with a NULL created_at", flush=True)


build("daily_totals", f"SELECT * FROM read_csv('{path('topic-analysis', 'daily-denominators.csv')}', header=true)")

build("congress", f"""
    SELECT tweet_id, author_handle, author_name, party, chamber, {CHAMBER_NORM_SQL} AS chamber_norm,
           state, created_at, text, topic, source_corpus
    FROM read_parquet('{path('congress-tweets', 'congress-tweets-unified.parquet')}')
    WHERE created_at IS NOT NULL
""", before=report_undated)

# The hash filter runs before the window function, so the dedupe only sees the ~3.6M sampled rows.
build("sample", f"""
    SELECT {', '.join(SAMPLE_COLUMNS)}
    FROM read_parquet('{path('twitter-firehose', 'tweets-*.parquet')}')
    WHERE hash(id) % 100 = 0
    QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, added_at DESC) = 1
""")

DISTINCT_TWEETS = 377_000_000  # measured: 395M observations are 377M tweets plus version snapshots
sampled, observations = con.execute(f"""
    SELECT count(*),
           (SELECT sum(num_rows) FROM parquet_file_metadata('{path('twitter-firehose', 'tweets-*.parquet')}'))
    FROM read_parquet('{path('harness', 'data', 'prepared', 'sample.parquet')}')""").fetchone()
print(f"sample: {sampled:,} tweets = {sampled / DISTINCT_TWEETS:.4%} of ~{DISTINCT_TWEETS:,} distinct tweets "
      f"(and {sampled / observations:.4%} of {observations:,} observations, which repeat a tweet per version)", flush=True)
con.close()
