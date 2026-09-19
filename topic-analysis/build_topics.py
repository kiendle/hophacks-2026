# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pyarrow>=20", "numpy>=2", "pytz"]
# ///
"""Build latest tweet state and exact lexical hashtag aggregates.

The firehose files are globally nondecreasing by numeric tweet id.  This
pipeline streams contiguous ID runs with Arrow, carrying only the trailing
run across batch and file boundaries, and selects max(version, added_at).
No full-corpus partition/window sort is used.

Run:
    uv run topic-analysis/build_topics.py [source_dir] [output_dir]
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


EXPECTED_SOURCE_FILES = 396
EXPECTED_SOURCE_ROWS = 395_352_258
EXPECTED_LATEST_ROWS = 377_270_972
THREADS = 4
MEMORY_LIMIT = "5GB"
LATEST_CHUNK_ROWS = 1_000_000
STAGE_ROW_GROUP_ROWS = 131_072

# This is the intentionally storage-reduced latest schema.  The omitted source
# columns (reply_to_user_id, poll, embed, source, synced, embedded) are not
# needed by lexical topic discovery and are recorded in topic-inventory.json.
PROJECTED_COLUMNS = [
    "id",
    "author_id",
    "body",
    "created_at",
    "version",
    "added_at",
    "lang",
    "like_count",
    "reply_count",
    "retweet_count",
    "quote_count",
    "views_count",
    "bookmarks_count",
    "reply_to_status_id",
    "quoting_id",
    "conversation_id",
    "media",
]
OUTPUT_COLUMNS = PROJECTED_COLUMNS + ["is_rt"]
HASHTAG_REGEX = r"#[\p{L}\p{M}\p{N}_]+"


def sql_quote(value: str) -> str:
    return value.replace("'", "''")


def sql_path(path: Path | str) -> str:
    return sql_quote(str(Path(path).resolve()))


def numeric_id_key(value: Any) -> int:
    if value is None:
        return -1
    return int(str(value))


def as_json_value(value: Any) -> Any:
    """Convert Arrow/DuckDB scalar values to JSON-safe values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): as_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_json_value(v) for v in value]
    return value


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    result = con.execute(sql)
    names = [d[0] for d in result.description]
    return [dict(zip(names, (as_json_value(v) for v in row))) for row in result.fetchall()]




class ChunkedParquetWriter:
    """Write RecordBatches into bounded-size sorted output parquet files."""

    def __init__(self, directory: Path, chunk_rows: int = LATEST_CHUNK_ROWS):
        self.directory = directory
        self.chunk_rows = chunk_rows
        self.chunk_index = 0
        self.rows_in_chunk = 0
        self.total_rows = 0
        self.writer: pq.ParquetWriter | None = None
        self.schema: pa.Schema | None = None
        self.paths: list[Path] = []

    def _open(self, schema: pa.Schema) -> None:
        if self.writer is not None:
            return
        self.schema = schema
        path = self.directory / f"tweets-{self.chunk_index:06d}.parquet"
        self.paths.append(path)
        self.writer = pq.ParquetWriter(path, schema, compression="zstd", use_dictionary=True)

    def append(self, batch: pa.RecordBatch) -> None:
        offset = 0
        while offset < batch.num_rows:
            self._open(batch.schema)
            room = self.chunk_rows - self.rows_in_chunk
            take = min(room, batch.num_rows - offset)
            self.writer.write_batch(batch.slice(offset, take), row_group_size=STAGE_ROW_GROUP_ROWS)
            self.rows_in_chunk += take
            self.total_rows += take
            offset += take
            if self.rows_in_chunk == self.chunk_rows:
                self.writer.close()
                self.writer = None
                self.rows_in_chunk = 0
                self.chunk_index += 1

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None



def clean_output(output: Path) -> tuple[Path, Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    latest = output / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    temp = output / ".tmp"
    temp.mkdir(parents=True, exist_ok=True)
    for path in latest.glob("tweets-*.parquet"):
        path.unlink()
    for path in temp.glob("stage-*.parquet"):
        path.unlink()
    for path in [output / "daily-denominators.csv", output / "hashtag-daily.parquet", output / "topic-inventory.json"]:
        if path.exists():
            path.unlink()
    return latest, temp, output

def configure_connection(con: duckdb.DuckDBPyConnection, temp: Path) -> None:
    con.execute("SET TimeZone='UTC'")
    con.execute(f"SET threads={THREADS}")
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=false")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute(f"SET temp_directory='{sql_path(temp)}'")

def timestamp_numpy(array: pa.Array) -> np.ndarray:
    values = pc.cast(array, pa.int64())
    values = pc.fill_null(values, pa.scalar(np.iinfo(np.int64).min, type=pa.int64()))
    return values.to_numpy(zero_copy_only=False)


def latest_selection(
    table: pa.Table,
    starts: np.ndarray,
    ends: np.ndarray,
    completed_groups: int,
) -> pa.Array:
    """Return latest row indices for all complete contiguous id runs."""
    if completed_groups <= 0:
        return pa.array([], type=pa.int64())
    selected = starts[:completed_groups].copy()
    lengths = ends - starts
    duplicate_groups = np.flatnonzero(lengths[:completed_groups] > 1)
    if duplicate_groups.size == 0:
        return pa.array(selected, type=pa.int64())
    versions = timestamp_numpy(table["version"].combine_chunks())
    added = timestamp_numpy(table["added_at"].combine_chunks())
    for group_index in duplicate_groups:
        start = int(starts[group_index])
        end = int(ends[group_index])
        best = start
        for candidate in range(start + 1, end):
            if (versions[candidate], added[candidate]) >= (versions[best], added[best]):
                best = candidate
        selected[group_index] = best
    return pa.array(selected, type=pa.int64())


def write_selected_table(table: pa.Table, selection: pa.Array, output: ChunkedParquetWriter) -> None:
    if len(selection) == 0:
        return
    selected = table.take(selection)
    prefix = pc.starts_with(selected["body"].combine_chunks(), "RT @")
    is_rt = pc.fill_null(prefix, False)
    selected = selected.append_column("is_rt", is_rt)
    for batch in selected.to_batches(max_chunksize=STAGE_ROW_GROUP_ROWS):
        output.append(batch)


def build_latest(
    con: duckdb.DuckDBPyConnection,
    source_files: list[Path],
    latest: Path,
    temp: Path,
) -> tuple[ChunkedParquetWriter, list[dict[str, Any]], float]:
    """Stream sorted source runs; only the trailing id run is retained."""
    del con, temp
    started = time.monotonic()
    writer = ChunkedParquetWriter(latest)
    source_stage_rows: list[dict[str, Any]] = []
    pending_batch: pa.RecordBatch | None = None
    previous_source_last_id: int | None = None
    for index, source in enumerate(source_files):
        parquet = pq.ParquetFile(source)
        source_rows = int(parquet.metadata.num_rows)
        source_groups = 0
        for batch in parquet.iter_batches(
            batch_size=262_144,
            columns=PROJECTED_COLUMNS,
            use_threads=True,
        ):
            if batch.num_rows == 0:
                continue
            first_id = numeric_id_key(batch.column(0)[0].as_py())
            last_id = numeric_id_key(batch.column(0)[batch.num_rows - 1].as_py())
            if first_id > last_id or (
                previous_source_last_id is not None and previous_source_last_id > first_id
            ):
                raise RuntimeError(f"source numeric id order regressed in {source.name}")
            previous_source_last_id = last_id
            current = pa.Table.from_batches(
                [pending_batch, batch] if pending_batch is not None else [batch]
            )
            pending_batch = None
            encoded = pc.run_end_encode(current["id"].combine_chunks())
            ends = encoded.run_ends.to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
            if len(ends) == 0:
                continue
            starts = np.empty_like(ends)
            starts[0] = 0
            starts[1:] = ends[:-1]
            source_groups += len(ends)
            completed_groups = len(ends) - 1
            selection = latest_selection(current, starts, ends, completed_groups)
            write_selected_table(current, selection, writer)
            pending_batch = current.slice(int(starts[-1])).to_batches()[0]
        source_stage_rows.append(
            {
                "file": source.name,
                "observations": source_rows,
                "source_run_groups_seen": source_groups,
            }
        )
        elapsed = time.monotonic() - started
        print(
            f"latest {index + 1}/{len(source_files)} {source.name}: "
            f"{source_rows:,} observations, emitted {writer.total_rows:,} rows, "
            f"{elapsed / 60:.1f} min",
            flush=True,
        )
    if pending_batch is not None:
        final = pa.Table.from_batches([pending_batch])
        encoded = pc.run_end_encode(final["id"].combine_chunks())
        ends = encoded.run_ends.to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
        starts = np.empty_like(ends)
        starts[0] = 0
        starts[1:] = ends[:-1]
        write_selected_table(final, latest_selection(final, starts, ends, len(ends)), writer)
    writer.close()
    if writer.total_rows != EXPECTED_LATEST_ROWS:
        raise RuntimeError(
            f"latest row reconciliation failed: {writer.total_rows:,} != {EXPECTED_LATEST_ROWS:,}"
        )
    return writer, source_stage_rows, time.monotonic() - started


def source_inventory(con: duckdb.DuckDBPyConnection, source_glob: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = fetch_dicts(
        con,
        f"""
SELECT file_name, num_rows, num_row_groups, file_size_bytes
FROM parquet_file_metadata('{sql_quote(source_glob)}')
ORDER BY file_name
""",
    )
    files = [
        {
            "file": Path(str(row["file_name"])).name,
            "observations": int(row["num_rows"]),
            "row_groups": int(row["num_row_groups"]),
            "bytes": int(row["file_size_bytes"]),
        }
        for row in rows
    ]
    inventory = {
        "file_count": len(files),
        "observation_rows": sum(row["observations"] for row in files),
        "compressed_bytes": sum(row["bytes"] for row in files),
        "row_groups": sum(row["row_groups"] for row in files),
        "files": files,
    }
    return inventory, files

def get_schema(con: duckdb.DuckDBPyConnection, source_glob: str) -> list[dict[str, Any]]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{sql_quote(source_glob)}') LIMIT 0").fetchall()
    return [
        {"column_name": row[0], "column_type": row[1], "nullable": True}
        for row in rows
    ]

def latest_days(con: duckdb.DuckDBPyConnection, latest_glob: str) -> list[str]:
    rows = con.execute(
        f"""
SELECT DISTINCT CAST(created_at AS DATE) AS day
FROM read_parquet('{sql_quote(latest_glob)}')
WHERE created_at IS NOT NULL
ORDER BY day
"""
    ).fetchall()
    return [row[0].isoformat() for row in rows]


def run_daily_denominators(
    con: duckdb.DuckDBPyConnection,
    latest_glob: str,
    output_csv: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    # One all-language grouping and one day-only author grouping avoid the
    # two simultaneous grouping sets that exceeded the worker memory bound.
    # Both are exact across the complete latest corpus (no shard sums for
    # authors); two full scans are materially cheaper than 32 day rescans.
    con.execute("SET threads=2")
    source = sql_quote(latest_glob)
    language_rows = fetch_dicts(
        con,
        f"""
SELECT
    CAST(created_at AS DATE) AS day,
    coalesce(lang, '<NULL>') AS lang,
    count(*) AS tweets,
    count(DISTINCT author_id) AS authors,
    count(*) FILTER (WHERE NOT is_rt) AS originals,
    count(*) FILTER (WHERE is_rt) AS rt_tweets,
    coalesce(sum(like_count) FILTER (WHERE NOT is_rt), 0) AS likes,
    coalesce(sum(views_count) FILTER (WHERE NOT is_rt), 0) AS views,
    coalesce(sum(retweet_count) FILTER (WHERE NOT is_rt), 0) AS retweets,
    count(*) FILTER (WHERE body IS NOT NULL AND length(trim(body)) > 0) AS body_nonnull,
    count(*) FILTER (WHERE body IS NULL OR length(trim(body)) = 0) AS body_null_or_empty,
    0 AS created_at_null
FROM read_parquet('{source}')
WHERE created_at IS NOT NULL
GROUP BY CAST(created_at AS DATE), coalesce(lang, '<NULL>')
ORDER BY day, lang
""",
    )
    total_author_rows = fetch_dicts(
        con,
        f"""
SELECT CAST(created_at AS DATE) AS day, count(DISTINCT author_id) AS authors
FROM read_parquet('{source}')
WHERE created_at IS NOT NULL
GROUP BY CAST(created_at AS DATE)
ORDER BY day
""",
    )
    authors_by_day = {str(row["day"]): int(row["authors"]) for row in total_author_rows}
    sum_fields = [
        "tweets", "originals", "rt_tweets", "likes", "views", "retweets",
        "body_nonnull", "body_null_or_empty", "created_at_null",
    ]
    totals: dict[str, dict[str, Any]] = {}
    for row in language_rows:
        day_text = str(row["day"])
        total = totals.setdefault(
            day_text,
            {"day": day_text, "lang": "<ALL>", "authors": authors_by_day.get(day_text, 0)},
        )
        for field in sum_fields:
            total[field] = int(total.get(field, 0)) + int(row[field] or 0)
    rows = language_rows + list(totals.values())
    partial_day = max(totals, default=None)
    fieldnames = [
        "day", "lang", "tweets", "authors", "originals", "rt_tweets", "likes", "views",
        "retweets", "body_nonnull", "body_null_or_empty", "created_at_null", "partial_day",
    ]
    rows.sort(key=lambda row: (str(row["day"]), 0 if row["lang"] == "<ALL>" else 1, str(row["lang"])))
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            row["partial_day"] = bool(partial_day is not None and str(row["day"]) == partial_day)
            for key in fieldnames:
                if key not in row:
                    row[key] = 0
            writer.writerow({key: row[key] for key in fieldnames})
    con.execute(f"SET threads={THREADS}")
    return rows, partial_day

def run_hashtag_discovery(
    con: duckdb.DuckDBPyConnection,
    latest_glob: str,
    output_parquet: Path,
    days: list[str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    con.execute("SET threads=2")
    regex = sql_quote(HASHTAG_REGEX)
    day_values = days or latest_days(con, latest_glob)
    temp = output_parquet.parent / ".tmp"
    for old in temp.glob("hashtag-day-*.parquet"):
        old.unlink()
    stage_paths: list[Path] = []
    hashtag_mentions = 0
    tagged_tweets = 0
    latest_tweets = 0
    for day_text in day_values:
        start = date.fromisoformat(day_text)
        next_day = start.fromordinal(start.toordinal() + 1).isoformat()
        stage = temp / f"hashtag-day-{day_text}.parquet"
        query = f"""
COPY (
    WITH exploded AS (
        SELECT
            lower(tag) AS topic,
            id,
            author_id,
            is_rt,
            like_count,
            views_count,
            retweet_count
        FROM read_parquet('{sql_quote(latest_glob)}')
        CROSS JOIN UNNEST(regexp_extract_all(coalesce(body, ''), '{regex}')) AS u(tag)
        WHERE created_at >= TIMESTAMPTZ '{day_text} 00:00:00+00'
          AND created_at < TIMESTAMPTZ '{next_day} 00:00:00+00'
    ), deduplicated AS (
        SELECT topic, id, author_id, is_rt, like_count, views_count, retweet_count
        FROM exploded
        QUALIFY row_number() OVER (PARTITION BY id, topic ORDER BY topic) = 1
    )
    SELECT
        topic,
        DATE '{day_text}' AS day,
        count(*) AS tweets,
        count(DISTINCT author_id) AS authors,
        count(*) FILTER (WHERE NOT is_rt) AS originals,
        coalesce(sum(like_count) FILTER (WHERE NOT is_rt), 0) AS likes,
        coalesce(sum(views_count) FILTER (WHERE NOT is_rt), 0) AS views,
        coalesce(sum(retweet_count) FILTER (WHERE NOT is_rt), 0) AS retweets
    FROM deduplicated
    GROUP BY topic
) TO '{sql_path(stage)}'
(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {STAGE_ROW_GROUP_ROWS});
"""
        con.execute(query)
        stats = fetch_dicts(
            con,
            f"""
SELECT coalesce(sum(tweets), 0) AS hashtag_mentions
FROM read_parquet('{sql_path(stage)}')
""",
        )[0]
        coverage = fetch_dicts(
            con,
            f"""
SELECT
    count(*) AS latest_tweets,
    count(*) FILTER (WHERE regexp_matches(coalesce(body, ''), '{regex}')) AS tweets_with_hashtags
FROM read_parquet('{sql_quote(latest_glob)}')
WHERE created_at >= TIMESTAMPTZ '{day_text} 00:00:00+00'
  AND created_at < TIMESTAMPTZ '{next_day} 00:00:00+00'
""",
        )[0]
        hashtag_mentions += int(stats["hashtag_mentions"])
        tagged_tweets += int(coverage["tweets_with_hashtags"])
        latest_tweets += int(coverage["latest_tweets"])
        stage_paths.append(stage)
        print(
            f"hashtags {day_text}: {int(stats['hashtag_mentions']):,} topic tweets, "
            f"{int(coverage['tweets_with_hashtags']):,} tagged tweets",
            flush=True,
        )
    stage_glob = sql_path(temp / "hashtag-day-*.parquet")
    con.execute(
        f"""
COPY (
    SELECT topic, day, tweets, authors, originals, likes, views, retweets
    FROM read_parquet('{stage_glob}')
    ORDER BY topic, day
) TO '{sql_path(output_parquet)}'
(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {STAGE_ROW_GROUP_ROWS});
"""
    )
    result = fetch_dicts(
        con,
        f"""
SELECT count(*) AS topic_day_rows,
       count(DISTINCT topic) AS topics,
       coalesce(sum(tweets), 0) AS hashtag_mentions,
       count(*) FILTER (WHERE tweets = 1) AS singleton_topic_days
FROM read_parquet('{sql_path(output_parquet)}')
""",
    )[0]
    for stage in stage_paths:
        stage.unlink()
    result.update(
        {
            "latest_tweets": latest_tweets,
            "tweets_with_hashtags": tagged_tweets,
            "fraction_tweets_with_hashtags": tagged_tweets / latest_tweets if latest_tweets else None,
            "elapsed_seconds": time.monotonic() - started,
        }
    )
    con.execute(f"SET threads={THREADS}")
    return {
        key: int(value) if isinstance(value, (int, float)) and key != "fraction_tweets_with_hashtags" else value
        for key, value in result.items()
    }


def compute_quality(con: duckdb.DuckDBPyConnection, latest_glob: str) -> dict[str, Any]:
    regex = sql_quote(HASHTAG_REGEX)
    expressions = [
        "count(*) AS latest_rows",
        "count(*) FILTER (WHERE id IS NULL) AS id_null",
        "count(*) FILTER (WHERE author_id IS NULL) AS author_id_null",
        "count(*) FILTER (WHERE body IS NULL) AS body_null",
        "count(*) FILTER (WHERE body IS NOT NULL AND length(trim(body)) = 0) AS body_empty",
        "count(*) FILTER (WHERE created_at IS NULL) AS created_at_null",
        "count(*) FILTER (WHERE version IS NULL) AS version_null",
        "count(*) FILTER (WHERE added_at IS NULL) AS added_at_null",
        "count(*) FILTER (WHERE lang IS NULL) AS lang_null",
        "count(*) FILTER (WHERE like_count IS NULL) AS like_count_null",
        "count(*) FILTER (WHERE views_count IS NULL) AS views_count_null",
        "count(*) FILTER (WHERE retweet_count IS NULL) AS retweet_count_null",
        "count(*) FILTER (WHERE is_rt) AS rt_rows",
        f"count(*) FILTER (WHERE regexp_matches(coalesce(body, ''), '{regex}')) AS tweets_with_hashtags",
        f"count(*) FILTER (WHERE created_at IS NULL AND regexp_matches(coalesce(body, ''), '{regex}')) AS tweets_with_hashtags_null_date",
    ]
    row = fetch_dicts(
        con,
        f"SELECT {', '.join(expressions)} FROM read_parquet('{sql_quote(latest_glob)}')",
    )[0]
    return {key: int(value) if isinstance(value, (int, float)) else value for key, value in row.items()}


def compute_rankings(
    con: duckdb.DuckDBPyConnection,
    output_csv: Path,
    output_parquet: Path,
    partial_day: str | None,
) -> dict[str, Any]:
    con.execute(
        f"""
CREATE OR REPLACE TEMP TABLE daily_denominators AS
SELECT * FROM read_csv('{sql_path(output_csv)}', header=true, auto_detect=true, dateformat='%Y-%m-%d')
"""
    )
    # Ranking uses only complete days and explicit minimum support.  The full
    # hashtag-daily parquet is intentionally unthresholded, including one-day
    # bursts and low-count topics.
    complete_filter = "d.lang = '<ALL>' AND NOT d.partial_day"
    volume = fetch_dicts(
        con,
        f"""
SELECT h.topic,
       sum(h.tweets) AS tweets,
       sum(h.originals) AS originals,
       sum(h.authors) AS author_topic_days,
       count(DISTINCT h.day) AS active_days,
       max(h.day) AS last_day
FROM read_parquet('{sql_path(output_parquet)}') h
JOIN daily_denominators d ON d.day = h.day
WHERE {complete_filter}
GROUP BY h.topic
HAVING sum(h.tweets) >= 100
ORDER BY tweets DESC, h.topic
LIMIT 100
""",
    )
    spikes = fetch_dicts(
        con,
        f"""
WITH complete AS (
    SELECT h.topic, h.day, h.tweets, h.authors, h.originals, h.likes, h.views, h.retweets,
           d.tweets AS denominator_tweets,
           h.tweets::DOUBLE / NULLIF(d.tweets, 0) AS share
    FROM read_parquet('{sql_path(output_parquet)}') h
    JOIN daily_denominators d ON d.day = h.day
    WHERE {complete_filter}
), baseline AS (
    SELECT topic,
           sum(tweets) AS topic_tweets,
           sum(denominator_tweets) AS denominator_tweets,
           sum(tweets)::DOUBLE / NULLIF(sum(denominator_tweets), 0) AS baseline_share
    FROM complete
    GROUP BY topic
)
SELECT c.topic, c.day, c.tweets, c.authors, c.originals, c.likes, c.views, c.retweets,
       c.denominator_tweets, c.share, b.baseline_share,
       c.share / NULLIF(b.baseline_share, 0) AS share_ratio_vs_baseline,
       b.topic_tweets
FROM complete c
JOIN baseline b ON b.topic = c.topic
WHERE c.tweets >= 20
ORDER BY c.share DESC, c.tweets DESC, c.topic
LIMIT 200
""",
    )
    single_day = fetch_dicts(
        con,
        f"""
SELECT count(*) AS single_day_topics
FROM (
    SELECT topic
    FROM read_parquet('{sql_path(output_parquet)}') h
    JOIN daily_denominators d ON d.day = h.day
    WHERE {complete_filter}
    GROUP BY topic
    HAVING count(DISTINCT h.day) = 1
)
""",
    )[0]
    return {
        "ranking_policy": {
            "complete_days_only": True,
            "partial_day_excluded": partial_day,
            "volume_min_tweets": 100,
            "share_spike_min_tweets_per_day": 20,
            "full_discovery_thresholded": False,
        },
        "top_volume_complete_days": volume,
        "top_normalized_share_spikes": spikes,
        "single_day_topics_complete_days": int(single_day["single_day_topics"]),
    }


def main() -> None:
    aggregate_only = "--aggregate-only" in sys.argv[1:]
    finalize_only = "--finalize-only" in sys.argv[1:]
    positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    source_root = Path(positional[0] if positional else "twitter-firehose").resolve()
    output_root = Path(positional[1] if len(positional) > 1 else "topic-analysis").resolve()
    if aggregate_only or finalize_only:
        output = output_root
        latest = output / "latest"
        temp = output / ".tmp"
        output.mkdir(parents=True, exist_ok=True)
        latest.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        if aggregate_only:
            for path in [output / "daily-denominators.csv", output / "hashtag-daily.parquet", output / "topic-inventory.json"]:
                if path.exists():
                    path.unlink()
        latest_paths = sorted(latest.glob("tweets-*.parquet"))
        if not latest_paths:
            raise RuntimeError("resume/finalize requested but topic-analysis/latest is empty")
        latest_rows = sum(int(pq.ParquetFile(path).metadata.num_rows) for path in latest_paths)
        if latest_rows != EXPECTED_LATEST_ROWS:
            raise RuntimeError(
                f"existing latest row reconciliation failed: {latest_rows:,} != {EXPECTED_LATEST_ROWS:,}"
            )
        stage_rows: list[dict[str, Any]] = []
        latest_seconds = 0.0
    else:
        latest, temp, output = clean_output(output_root)
        latest_paths = []
        latest_rows = 0
        stage_rows = []
        latest_seconds = 0.0
    source_files = sorted(source_root.glob("tweets-*.parquet"))
    if len(source_files) != EXPECTED_SOURCE_FILES:
        raise RuntimeError(f"expected {EXPECTED_SOURCE_FILES} source shards, found {len(source_files)}")
    source_glob = str((source_root / "tweets-*.parquet").resolve())
    con = duckdb.connect()
    configure_connection(con, temp)
    started = time.monotonic()
    inventory, _ = source_inventory(con, source_glob)
    if inventory["observation_rows"] != EXPECTED_SOURCE_ROWS:
        raise RuntimeError(
            f"source row reconciliation failed: {inventory['observation_rows']:,} != {EXPECTED_SOURCE_ROWS:,}"
        )
    schema = get_schema(con, source_glob)
    source_schema_names = {row["column_name"] for row in schema}
    missing = [column for column in PROJECTED_COLUMNS if column not in source_schema_names]
    if missing:
        raise RuntimeError(f"missing projected source columns: {missing}")
    print(
        f"Source inventory: {inventory['file_count']} files, {inventory['observation_rows']:,} rows, "
        f"{inventory['compressed_bytes'] / 1e9:.2f} GB compressed",
        flush=True,
    )
    if not aggregate_only and not finalize_only:
        writer, stage_rows, latest_seconds = build_latest(con, source_files, latest, temp)
        latest_paths = writer.paths
        latest_rows = writer.total_rows
    latest_glob = str((latest / "tweets-*.parquet").resolve())
    if latest_rows != EXPECTED_LATEST_ROWS:
        raise RuntimeError(f"latest row reconciliation failed: {latest_rows:,} != {EXPECTED_LATEST_ROWS:,}")
    print(f"Latest state complete: {latest_rows:,} rows in {len(latest_paths)} files", flush=True)

    if finalize_only:
        with (output / "daily-denominators.csv").open(newline="", encoding="utf-8") as handle:
            denominator_rows = list(csv.DictReader(handle))
        partial_rows = [
            row["day"] for row in denominator_rows
            if row["lang"] == "<ALL>" and row["partial_day"].lower() == "true"
        ]
        partial_day = partial_rows[0] if partial_rows else None
        hashtag_stats = fetch_dicts(
            con,
            f"""
SELECT count(*) AS topic_day_rows,
       count(DISTINCT topic) AS topics,
       coalesce(sum(tweets), 0) AS hashtag_mentions,
       count(*) FILTER (WHERE tweets = 1) AS singleton_topic_days
FROM read_parquet('{sql_path(output / "hashtag-daily.parquet")}')
""",
        )[0]
        hashtag_stats.update({"elapsed_seconds": 0.0})
    else:
        denominator_rows, partial_day = run_daily_denominators(
            con, latest_glob, output / "daily-denominators.csv"
        )
        print(f"Daily denominators complete: {len(denominator_rows)} day/language rows", flush=True)
        days = sorted({str(row["day"]) for row in denominator_rows if row["lang"] == "<ALL>"})
        hashtag_stats = run_hashtag_discovery(
            con, latest_glob, output / "hashtag-daily.parquet", days=days
        )
    quality = compute_quality(con, latest_glob)
    dated_latest = quality["latest_rows"] - quality["created_at_null"]
    dated_tagged = quality["tweets_with_hashtags"] - quality["tweets_with_hashtags_null_date"]
    hashtag_stats["latest_tweets_with_created_at"] = dated_latest
    hashtag_stats["tweets_with_hashtags_with_created_at"] = dated_tagged
    hashtag_stats["latest_tweets"] = quality["latest_rows"]
    hashtag_stats["tweets_with_hashtags"] = quality["tweets_with_hashtags"]
    hashtag_stats["tagged_tweets_without_created_at"] = quality["tweets_with_hashtags_null_date"]
    hashtag_stats["fraction_tweets_with_hashtags"] = (
        hashtag_stats["tweets_with_hashtags"] / quality["latest_rows"] if quality["latest_rows"] else None
    )
    rankings = compute_rankings(
        con,
        output / "daily-denominators.csv",
        output / "hashtag-daily.parquet",
        partial_day,
    )
    latest_schema = fetch_dicts(con, f"DESCRIBE SELECT * FROM read_parquet('{sql_path(latest_glob)}')")
    hashtag_schema = fetch_dicts(con, f"DESCRIBE SELECT * FROM read_parquet('{sql_path(output / 'hashtag-daily.parquet')}')")
    denominator_schema = [
        {"column_name": field, "column_type": "CSV"}
        for field in [
            "day", "lang", "tweets", "authors", "originals", "rt_tweets", "likes", "views",
            "retweets", "body_nonnull", "body_null_or_empty", "created_at_null", "partial_day",
        ]
    ]
    total_seconds = time.monotonic() - started
    inventory_json = {
        "method": {
            "time_zone": "UTC",
            "source_id_order": "globally nondecreasing numeric id; prior full scan verified and streaming boundaries checked",
            "deduplication": "Arrow run-end encoding over each sorted source batch; max(version, added_at) selected per contiguous ID run; trailing run carried across batch/file boundaries",
            "latest_tie_break": "version DESC, then added_at DESC; exact ties retain the later physical row",
            "memory_bound": {
                "duckdb_threads": THREADS,
                "duckdb_memory_limit": MEMORY_LIMIT,
                "latest_batch_rows": 262144,
                "latest_pending_rows": "one trailing ID run",
                "aggregate_partition": "one UTC day",
            },
            "hashtag_regex": HASHTAG_REGEX,
            "hashtag_normalization": "Unicode regex extraction including combining marks, lower() case normalization, distinct (id, topic) before aggregation",
            "engagement_policy": "likes/views/retweets in hashtag-daily and daily denominators sum only non-RT-prefixed rows; counts include every latest unique tweet",
            "partial_day_policy": "last observed created_at UTC date is marked partial_day and excluded from ranked comparisons",
        },
        "source_inventory": inventory,
        "source_schema": schema,
        "projected_latest_schema": latest_schema,
        "omitted_source_columns": sorted(column for column in source_schema_names if column not in PROJECTED_COLUMNS),
        "latest_output": {
            "directory": str(latest.relative_to(output_root)),
            "files": len(latest_paths),
            "rows": latest_rows,
            "source_shards_processed": EXPECTED_SOURCE_FILES if not aggregate_only else "existing complete latest",
            "per_source_stage": stage_rows,
            "non_overlapping_ids": True,
        },
        "daily_denominators": {
            "path": str((output / "daily-denominators.csv").relative_to(output_root)),
            "schema": denominator_schema,
            "rows": len(denominator_rows),
            "partial_day": partial_day,
            "language_rows_include_all_total": True,
        },
        "hashtag_discovery": {
            "path": str((output / "hashtag-daily.parquet").relative_to(output_root)),
            "schema": hashtag_schema,
            **hashtag_stats,
            **rankings,
        },
        "latest_quality_nulls": quality,
        "runtime_seconds": total_seconds,
        "runtime_minutes": total_seconds / 60.0,
        "latest_build_seconds": None if (aggregate_only or finalize_only) else latest_seconds,
        "aggregate_only_resume": aggregate_only,
        "finalize_only": finalize_only,
    }
    with (output / "topic-inventory.json").open("w", encoding="utf-8") as handle:
        json.dump(inventory_json, handle, ensure_ascii=False, indent=2, default=as_json_value)
        handle.write("\n")
    shutil.rmtree(temp, ignore_errors=True)
    con.close()
    print(
        f"Finished topic analysis in {total_seconds / 60:.1f} min; outputs under {output}",
        flush=True,
    )


if __name__ == "__main__":
    main()
