# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2"]
# ///
"""Prepare the classified package for streaming; no model calls or regrading.

uv run scripts/prepare_demo.py /path/to/processed-streams-...
"""
import argparse
import gzip
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import duckdb

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("package", type=Path)
parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/demo.json.gz")
args = parser.parse_args()
manifest = json.loads((args.package / "manifest.json").read_text())
for filename in ("post-events.parquet", "like-events.parquet"):
    actual = hashlib.sha256((args.package / filename).read_bytes()).hexdigest()
    if actual != manifest["files"][filename]["sha256"]:
        raise ValueError(f"Checksum mismatch: {filename}")

c = duckdb.connect()
c.read_parquet(str(args.package / "post-events.parquet")).create_view("posts")
c.read_parquet(str(args.package / "like-events.parquet")).create_view("likes")

def rows(sql):
    result = c.execute(sql)
    names = [col[0] for col in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]

posts = rows("""SELECT * EXCLUDE(time,content_observed_at), epoch_ms(time) AS t,
    epoch_ms(content_observed_at) AS observed FROM posts""")
likes = rows("""SELECT * EXCLUDE(time,observed_added_at), epoch_ms(time) AS t,
    epoch_ms(observed_added_at) AS observed FROM likes""")
parents = {p["post_id"]: p for p in posts}
assert len(parents) == len(posts), "Expected one publication per post ID"
companies = {x["id"] for x in manifest["taxonomy"]["categories"]}
events = []
for kind, source in (("post", posts), ("like", likes)):
    for row in source:
        grades = []
        for company in row["classification"]["companies"]:
            assert company in companies
            sentiment = row["sentiment"][company]
            probabilities = sentiment["probabilities"]
            assert all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
            assert abs(sum(probabilities.values()) - 1) <= 0.011
            score = 5 * (1 + probabilities["positive"] - probabilities["negative"])
            grades.append({"company": company, "choice": sentiment["choice"],
                "score": None if sentiment["choice"] == "insufficient_evidence" else round(score, 10),
                "confidence": sentiment["confidence"], "probabilities": probabilities})
        parent = row if kind == "post" else parents.get(row["post_id"])
        event = {"id": row["event_id"], "kind": kind, "t": row["t"],
            "postId": row["post_id"], "postTime": parent["t"] if parent else None,
            "text": row["content" if kind == "post" else "post_content"],
            "authorId": parent["author_id"] if parent else None,
            "contentVersion": row["content_version"], "observedAt": row["observed"], "grades": grades}
        if kind == "like":
            event.update(delta=row["likes_delta"], opening=row["is_opening"])
        events.append(event)
events.sort(key=lambda e: (e["t"], e["kind"] != "post", e["id"]))
assert len({e["id"] for e in events}) == len(events), "Duplicate event IDs"
assert len(posts) == manifest["counts"]["exported_post_events"]
assert len(likes) == manifest["counts"]["exported_like_events"]
payload = {"version": 1, "start": int(datetime.fromisoformat(manifest["window"]["start"]).timestamp() * 1000),
    "end": int(datetime.fromisoformat(manifest["window"]["end"]).timestamp() * 1000),
    "companies": [{"id": x["id"], "name": x["label"]} for x in manifest["taxonomy"]["categories"]],
    "counts": {"posts": len(posts), "likes": len(likes)}, "events": events}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(), mtime=0))
print(json.dumps({"output": str(args.output), **payload["counts"], "bytes": args.output.stat().st_size}))
