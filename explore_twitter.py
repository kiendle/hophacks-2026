# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pyarrow>=20", "pytz"]
# ///
"""Run: uv run explore_twitter.py [dataset_directory] [output_directory].

Full-corpus aggregates are observation-level. Content and trajectories use a
repeatable 0.1% hash sample of tweet IDs, retaining every sampled observation.
"""
import json
import sys
import time
from pathlib import Path

import duckdb
import pyarrow.compute as pc
import pyarrow.parquet as pq

root = Path(sys.argv[1] if len(sys.argv) > 1 else "twitter-firehose")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "twitter-analysis")
out.mkdir(exist_ok=True, parents=True)
con = duckdb.connect()
con.execute("SET TimeZone='UTC'")
con.execute("SET threads=6")
con.execute("SET memory_limit='8GB'")
con.execute("SET preserve_insertion_order=false")
con.execute("SET max_temp_directory_size='60GB'")
con.execute(f"SET temp_directory='{str(out / '.tmp').replace(chr(39), chr(39)*2)}'")
source = str(root / "tweets-*.parquet").replace("'", "''")
sample_path = str(out / "tweet-id-sample.parquet").replace("'", "''")
con.execute(f"CREATE VIEW observations AS SELECT * FROM read_parquet('{source}')")
results = {
    "method": {
        "full_corpus": "Observation-level; repeated IDs retained.",
        "sample": "hash(id) % 1000 = 0; all observations of selected IDs retained.",
        "latest_sample": "Latest version per sampled ID; same-version ties are broken by added_at and otherwise unspecified.",
        "time_zone": "UTC",
        "duckdb_version": duckdb.__version__,
    }
}


def query(name, sql):
    start = time.monotonic()
    cursor = con.execute(sql)
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    results[name] = rows
    (out / "profile.json").write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False) + "\n")
    print(f"{name}: {len(rows)} result rows ({time.monotonic()-start:.1f}s)", flush=True)
    return rows


query("schema", "DESCRIBE observations")
query("inventory", f"SELECT count(*) AS parquet_files, sum(num_rows) AS observations, sum(file_size_bytes) AS parquet_bytes, sum(num_row_groups) AS row_groups FROM parquet_file_metadata('{source}')")
query("column_metadata", f"""
    SELECT path_in_schema AS column_name, sum(num_values) AS values_including_nulls,
           sum(stats_null_count) AS nulls, count(*) FILTER (WHERE stats_null_count IS NULL) AS missing_null_statistics,
           sum(total_compressed_size) AS compressed_bytes,
           string_agg(DISTINCT compression, ', ') AS compression
    FROM parquet_metadata('{source}') GROUP BY path_in_schema ORDER BY path_in_schema
""")
print("Counting exact tweet IDs while checking global numeric ID order...", flush=True)
previous_id = None
id_runs = descents = streamed_rows = 0
files = con.execute(f"SELECT file_name FROM parquet_file_metadata('{source}') ORDER BY file_name").fetchall()
for (filename,) in files:
    for batch in pq.ParquetFile(filename).iter_batches(batch_size=262144, columns=["id"]):
        ids = pc.cast(batch.column(0), "uint64")
        if not len(ids):
            continue
        first, last = ids[0].as_py(), ids[-1].as_py()
        id_runs += int(previous_id is None or first != previous_id)
        descents += int(previous_id is not None and first < previous_id)
        if len(ids) > 1:
            id_runs += pc.sum(pc.not_equal(ids.slice(1), ids.slice(0, len(ids)-1))).as_py()
            descents += pc.sum(pc.less(ids.slice(1), ids.slice(0, len(ids)-1))).as_py()
        previous_id = last
        streamed_rows += len(ids)
results["full_exact_ids"] = {
    "observations": streamed_rows,
    "adjacent_id_descents": descents,
    "distinct_tweets": id_runs if descents == 0 else None,
    "extra_observations": streamed_rows-id_runs if descents == 0 else None,
    "method": "Count numeric ID runs, valid only when verified globally nondecreasing.",
}
print(f"Exact ID scan: {results['full_exact_ids']}", flush=True)
print("Extracting 0.1% tweet-ID sample from every shard...", flush=True)
con.execute(f"COPY (SELECT * FROM observations WHERE hash(id) % 1000 = 0) TO '{sample_path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
con.execute(f"CREATE VIEW sampled AS SELECT * FROM read_parquet('{sample_path}')")
con.execute("CREATE TABLE latest AS SELECT * FROM sampled QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, added_at DESC)=1")
query("sample_size", "SELECT count(*) AS observations, count(DISTINCT id) AS tweets, count(DISTINCT author_id) AS authors, count(DISTINCT (id, version)) AS observation_keys FROM sampled")
query("sample_snapshot_distribution", "SELECT snapshots, count(*) AS tweets FROM (SELECT id, count(*) AS snapshots FROM sampled GROUP BY id) GROUP BY snapshots ORDER BY snapshots")
query("sample_duplicate_keys", """
    SELECT count(*) AS repeated_keys, coalesce(sum(n-1), 0) AS extra_rows,
           count(*) FILTER (WHERE states>1) AS keys_with_conflicting_states
    FROM (SELECT id, version, count(*) n, count(DISTINCT sampled) states
          FROM sampled GROUP BY id, version HAVING count(*)>1)
""")
query("sample_languages_latest", "SELECT coalesce(lang, '<NULL>') AS language, count(*) AS tweets FROM latest GROUP BY lang ORDER BY tweets DESC")
query("sample_quality_latest", """
    SELECT count(*) AS tweets,
      count(*) FILTER (WHERE trim(body)='') AS empty_body,
      count(*) FILTER (WHERE reply_to_status_id IS NOT NULL AND reply_to_status_id!='') AS replies,
      count(*) FILTER (WHERE quoting_id IS NOT NULL AND quoting_id!='') AS quotes,
      count(*) FILTER (WHERE media IS NOT NULL AND media!='') AS with_media,
      count(*) FILTER (WHERE contains(body, 'https://t.co/')) AS with_tco_link,
      count(*) FILTER (WHERE starts_with(body, 'RT @')) AS rt_prefixed,
      quantile_cont(length(body), [0.5, 0.9, 0.99]) AS body_characters_p50_p90_p99,
      max(length(body)) AS longest_body_characters,
      count(*) FILTER (WHERE version < created_at) AS version_before_creation,
      count(*) FILTER (WHERE added_at < created_at) AS ingestion_before_creation,
      count(*) FILTER (WHERE version > added_at) AS version_after_added_at,
      count(*) FILTER (WHERE abs(epoch_ms(created_at) - ((try_cast(id AS BIGINT) >> 22)+1288834974657))>1000) AS snowflake_time_mismatch_over_one_second,
      quantile_cont(date_diff('second',created_at,added_at), [0.5,0.9,0.99]) AS ingest_delay_seconds_p50_p90_p99,
      quantile_cont(date_diff('second',created_at,version), [0.5,0.9,0.99]) AS observation_age_seconds_p50_p90_p99
    FROM latest
""")
metrics = ["like_count", "reply_count", "retweet_count", "quote_count", "views_count", "bookmarks_count"]
query("sample_engagement_latest", " UNION ALL ".join(
    f"SELECT '{metric}' AS metric, count({metric}) AS nonnull_tweets, count(*) FILTER (WHERE {metric}=0) AS zero_tweets, count(*) FILTER (WHERE {metric}<0) AS negative_tweets, avg({metric}) AS mean, quantile_cont({metric}, [0.5,0.9,0.99,0.999]) AS p50_p90_p99_p999, max({metric}) AS maximum FROM latest"
    for metric in metrics
))
query("sample_engagement_by_post_type", """
    SELECT CASE WHEN starts_with(body, 'RT @') THEN 'RT-prefixed'
                WHEN reply_to_status_id IS NOT NULL AND reply_to_status_id!='' THEN 'reply'
                WHEN quoting_id IS NOT NULL AND quoting_id!='' THEN 'quote'
                ELSE 'other' END AS post_type,
           count(*) AS tweets,
           quantile_cont(like_count, [0.5,0.9,0.99]) AS likes_p50_p90_p99,
           quantile_cont(retweet_count, [0.5,0.9,0.99]) AS retweets_p50_p90_p99,
           quantile_cont(views_count, [0.5,0.9,0.99]) AS views_p50_p90_p99
    FROM latest GROUP BY post_type ORDER BY tweets DESC
""")
query("sample_repeated_tweet_coverage", """
    SELECT count(*) AS tweets_with_repeated_observations,
           quantile_cont(snapshots, [0.5,0.9,0.99]) AS snapshots_p50_p90_p99,
           quantile_cont(span_seconds, [0.5,0.9,0.99]) AS span_seconds_p50_p90_p99,
           max(snapshots) AS maximum_snapshots
    FROM (SELECT id, count(*) AS snapshots,
                 date_diff('second', min(version), max(version)) AS span_seconds
          FROM sampled GROUP BY id HAVING count(*)>1)
""")
query("sample_trajectory_changes", """
    WITH lagged AS (
      SELECT *, lag(version) OVER w AS previous_version,
                lag(like_count) OVER w AS previous_likes,
                lag(views_count) OVER w AS previous_views
      FROM sampled WINDOW w AS (PARTITION BY id ORDER BY version, added_at)
    ) SELECT count(*) AS transitions,
        count(*) FILTER (WHERE version=previous_version) AS same_version_transitions,
        count(*) FILTER (WHERE like_count>previous_likes) AS likes_increased,
        count(*) FILTER (WHERE like_count<previous_likes) AS likes_decreased,
        count(*) FILTER (WHERE views_count>previous_views) AS views_increased,
        count(*) FILTER (WHERE views_count<previous_views) AS views_decreased,
        quantile_cont(date_diff('second',previous_version,version), [0.5,0.9,0.99]) AS observation_interval_seconds_p50_p90_p99
    FROM lagged WHERE previous_version IS NOT NULL
""")
query("sample_top_hashtags_latest", r"""
    SELECT lower(tag) AS hashtag, count(DISTINCT id) AS tweets
    FROM latest, unnest(regexp_extract_all(body, '#[\p{L}\p{N}_]+')) AS tags(tag)
    GROUP BY lower(tag) ORDER BY tweets DESC LIMIT 30
""")
query("sample_repeated_bodies_latest", """
    SELECT left(body,240) AS body_excerpt, count(*) AS tweets, count(DISTINCT author_id) AS authors
    FROM latest WHERE length(trim(body))>=20
    GROUP BY body HAVING count(*)>1 ORDER BY tweets DESC LIMIT 15
""")
query("sample_top_liked_latest", """
    SELECT id, lang, created_at, version, like_count, retweet_count, views_count,
           left(body,300) AS body_excerpt
    FROM latest ORDER BY like_count DESC NULLS LAST LIMIT 10
""")
query("sample_nested_fields", " UNION ALL ".join(
    f"(SELECT '{field}' AS field, left({field}, 1600) AS example FROM latest WHERE {field} IS NOT NULL AND {field}!='' LIMIT 2)"
    for field in ["media", "poll", "embed", "source"]
))
query("sample_nested_validity", " UNION ALL ".join(
    f"SELECT '{field}' AS field, count(*) FILTER (WHERE {field} IS NOT NULL) AS present, count(*) FILTER (WHERE {field} IS NOT NULL AND json_valid({field})) AS valid_json FROM latest"
    for field in ["media", "poll", "embed"]
))
print("Computing full-corpus observation-level aggregates...", flush=True)
query("full_coverage", """
    SELECT count(*) AS observations,
      min(created_at) AS first_creation, max(created_at) AS last_creation,
      min(added_at) AS first_ingestion, max(added_at) AS last_ingestion,
      min(version) AS first_version, max(version) AS last_version,
      count(DISTINCT author_id) AS distinct_authors,
      count(*) FILTER (WHERE version<created_at) AS version_before_creation,
      count(*) FILTER (WHERE added_at<created_at) AS ingestion_before_creation,
      count(*) FILTER (WHERE version>added_at) AS version_after_added_at,
      count(*) FILTER (WHERE created_at<'2026-08-17'::TIMESTAMPTZ) AS before_documented_window,
      count(*) FILTER (WHERE reply_to_status_id IS NOT NULL AND reply_to_status_id!='') AS reply_observations,
      count(*) FILTER (WHERE quoting_id IS NOT NULL AND quoting_id!='') AS quote_observations
    FROM observations
""")
query("full_engagement", "SELECT " + ", ".join(
    f"max({metric}) AS {metric}_max, count(*) FILTER (WHERE {metric}<0) AS {metric}_negative_rows, count(*) FILTER (WHERE {metric}=0) AS {metric}_zero_rows"
    for metric in metrics
) + " FROM observations")
query("full_daily_and_languages", """
    SELECT CASE WHEN grouping(lang)=1 THEN 'day' ELSE 'language' END AS dimension,
      CASE WHEN grouping(lang)=1 THEN cast(cast(created_at AS DATE) AS VARCHAR) ELSE coalesce(lang, '<NULL>') END AS value,
      count(*) AS observations
    FROM observations GROUP BY GROUPING SETS ((cast(created_at AS DATE)), (lang))
    ORDER BY dimension, observations DESC
""")
for dimension, filename in [("day", "daily-observations.csv"), ("language", "language-observations.csv")]:
    import csv
    with (out / filename).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["value", "observations"])
        writer.writeheader()
        for row in results["full_daily_and_languages"]:
            if row["dimension"] == dimension:
                writer.writerow({key: row[key] for key in writer.fieldnames})
print(f"Finished. Results: {out / 'profile.json'}", flush=True)
