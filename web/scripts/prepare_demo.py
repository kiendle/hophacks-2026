# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5"]
# ///
"""Prepare the classified package for streaming; no model calls or regrading.

uv run scripts/prepare_demo.py /path/to/processed-streams-...
"""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

import duckdb

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("package", type=Path)
parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/demo.json.gz")
args = parser.parse_args()
manifest = json.loads((args.package / "manifest.json").read_text(encoding="utf-8"))
for filename in ("post-events.parquet", "like-events.parquet"):
    with (args.package / filename).open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != manifest["files"][filename]["sha256"]:
        raise ValueError(f"Checksum mismatch: {filename}")

c = duckdb.connect(config={"memory_limit": "1GB", "threads": "1", "preserve_insertion_order": "false"})
c.read_parquet(str(args.package / "post-events.parquet")).create_view("posts")
c.read_parquet(str(args.package / "like-events.parquet")).create_view("likes")

post_count, post_ids = c.execute("SELECT count(*), count(DISTINCT post_id) FROM posts").fetchone()
like_count = c.execute("SELECT count(*) FROM likes").fetchone()[0]
assert post_ids == post_count, "Expected one publication per post ID"
assert post_count == manifest["counts"]["exported_post_events"]
assert like_count == manifest["counts"]["exported_like_events"]
event_count, event_ids = c.execute("""SELECT count(*), count(DISTINCT event_id) FROM (
    SELECT event_id FROM posts UNION ALL SELECT event_id FROM likes)""").fetchone()
assert event_count == event_ids, "Duplicate event IDs"
companies = {x["id"] for x in manifest["taxonomy"]["categories"]}

# Project only accepted companies and join parent metadata in DuckDB. Fetching
# the full nested classifications for every row exhausts memory on large exports.
projection = """s.event_id AS id, '{kind}' AS kind, epoch_ms(s.time) AS t,
    s.post_id AS postId, epoch_ms({parent}.time) AS postTime,
    s.{content} AS text, {parent}.author_id AS authorId,
    s.content_version AS contentVersion, epoch_ms(s.{observed}) AS observedAt,
    list_transform(s.classification.companies, company ->
        struct_pack(company := company, sentiment := s.sentiment[company])) AS classified,
    {delta} AS delta, {opening} AS opening"""
post_projection = projection.format(kind="post", parent="s", content="content",
    observed="content_observed_at", delta="NULL", opening="NULL")
like_projection = projection.format(kind="like", parent="p", content="post_content",
    observed="observed_added_at", delta="s.likes_delta", opening="s.is_opening")


def events():
    result = c.execute(f"""SELECT * FROM (
        SELECT {post_projection} FROM posts s
        UNION ALL
        SELECT {like_projection} FROM likes s LEFT JOIN posts p ON p.post_id = s.post_id
        ) ORDER BY t, kind != 'post', id""")
    names = [column[0] for column in result.description]
    while batch := result.fetchmany(512):
        for values in batch:
            row = dict(zip(names, values))
            grades = []
            for classified in row.pop("classified"):
                company = classified["company"]
                assert company in companies
                sentiment = classified["sentiment"]
                probabilities = sentiment["probabilities"]
                assert all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
                assert abs(sum(probabilities.values()) - 1) <= 0.011
                score = 5 * (1 + probabilities["positive"] - probabilities["negative"])
                grades.append({"company": company, "choice": sentiment["choice"],
                    "score": None if sentiment["choice"] == "insufficient_evidence" else round(score, 10),
                    "confidence": sentiment["confidence"], "probabilities": probabilities})
            delta, opening = row.pop("delta"), row.pop("opening")
            row["grades"] = grades
            if row["kind"] == "like":
                row.update(delta=delta, opening=opening)
            yield row


payload = {"version": 1, "start": int(datetime.fromisoformat(manifest["window"]["start"]).timestamp() * 1000),
    "end": int(datetime.fromisoformat(manifest["window"]["end"]).timestamp() * 1000),
    "companies": [{"id": x["id"], "name": x["label"]} for x in manifest["taxonomy"]["categories"]],
    "counts": {"posts": post_count, "likes": like_count}}
args.output.parent.mkdir(parents=True, exist_ok=True)
encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
temporary = None
try:
    with tempfile.NamedTemporaryFile(dir=args.output.parent, suffix=".tmp", delete=False) as raw:
        temporary = Path(raw.name)
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as stream:
                # Keep valid JSON, with one event per line so runtime loaders do
                # not allocate a single string larger than the JS engine limit.
                stream.write(encode(payload)[:-1] + ',"events":[\n')
                written = 0
                for event in events():
                    if written:
                        stream.write(",\n")
                    stream.write(encode(event))
                    written += 1
                assert written == event_count
                stream.write("\n]}\n" if written else "]}\n")
    os.replace(temporary, args.output)
finally:
    c.close()
    if temporary is not None:
        temporary.unlink(missing_ok=True)
print(json.dumps({"output": str(args.output), **payload["counts"], "bytes": args.output.stat().st_size}))
