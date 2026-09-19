# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=20,<23"]
# ///
"""Reuse entity sentiment predictions and merge novel-body delta scores.

The entity sample was scored first.  This utility prepares one inference row per
exact raw body string absent from that entity cache, then merges the resulting
novel-body scores back to the final topic/day input.  Exact body reuse is
intentional and language-independent because ``score_sentiment.py`` also
caches by exact raw body.  The final output remains one row per final input ID;
body, topic, period, and author membership stay in the membership table.

Two invocations are expected:

1. ``--prepare-only`` writes ``final-sentiment-delta-input.parquet``.
2. Run ``score_sentiment.py`` on that delta input.
3. Run this utility without ``--prepare-only`` to write
   ``final-sentiment.parquet`` and its complete provenance JSON.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

LABELS = ("negative", "neutral", "positive")
SCORE_COLUMNS = ("negative", "neutral", "positive", "sentiment_score", "predicted_label")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-input", type=Path, default=Path("topic-analysis/sentiment-input.parquet"))
    parser.add_argument("--cache-input", type=Path, default=Path("topic-analysis/entity-sentiment-input.parquet"))
    parser.add_argument("--cache-output", type=Path, default=Path("topic-analysis/entity-sentiment.parquet"))
    parser.add_argument(
        "--delta-input", type=Path, default=Path("topic-analysis/final-sentiment-delta-input.parquet")
    )
    parser.add_argument("--delta-output", type=Path, default=Path("topic-analysis/final-sentiment-delta.parquet"))
    parser.add_argument("--output", type=Path, default=Path("topic-analysis/final-sentiment.parquet"))
    parser.add_argument("--metadata-output", type=Path, default=Path("topic-analysis/final-sentiment.json"))
    parser.add_argument("--prepare-only", action="store_true", help="Write novel-body delta input and stop")
    return parser.parse_args()


def require_columns(table: pa.Table, path: Path, columns: tuple[str, ...]) -> None:
    missing = set(columns) - set(table.column_names)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")


def read_score_rows(path: Path) -> pa.Table:
    table = pq.read_table(path)
    require_columns(table, path, ("id", *SCORE_COLUMNS))
    return table


def id_list(table: pa.Table) -> list[str]:
    values = table["id"].to_pylist()
    if any(value is None for value in values):
        raise ValueError("IDs must be non-null")
    return [str(value) for value in values]


def body_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def score_tuple(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
    values = tuple(float(row[name]) for name in SCORE_COLUMNS[:4])
    label = str(row["predicted_label"])
    return (*values, label)


def validate_score(score: tuple[float, float, float, float, str], context: str) -> None:
    probabilities = score[:3]
    if any(not math.isfinite(value) or value < -2e-5 or value > 1.00002 for value in probabilities):
        raise ValueError(f"probabilities out of [0,1] for {context}: {score}")
    if abs(sum(probabilities) - 1.0) > 2e-4:
        raise ValueError(f"probabilities do not sum to one for {context}: {score}")
    if abs(score[3] - (score[2] - score[0])) > 2e-4:
        raise ValueError(f"sentiment_score mismatch for {context}: {score}")
    if score[4] not in LABELS:
        raise ValueError(f"unexpected predicted_label for {context}: {score[4]!r}")


def body_score_map(input_table: pa.Table, output_table: pa.Table, name: str) -> dict[str, tuple[float, float, float, float, str]]:
    input_ids = id_list(input_table)
    output_ids = id_list(output_table)
    if input_ids != output_ids:
        raise ValueError(f"{name}: scorer changed ID order")
    bodies = [body_value(value) for value in input_table["body"].to_pylist()]
    output_rows = output_table.to_pylist()
    cache: dict[str, tuple[float, float, float, float, str]] = {}
    for index, (body, row) in enumerate(zip(bodies, output_rows)):
        if body is None or not body.strip():
            continue
        score = score_tuple(row)
        validate_score(score, f"{name} row {index}")
        old = cache.get(body)
        if old is not None and any(abs(a - b) > 2e-4 for a, b in zip(old[:4], score[:4])):
            raise ValueError(f"inconsistent predictions for duplicate body in {name}")
        cache[body] = score
    return cache


def write_delta(path: Path, rows: list[dict[str, Any]]) -> None:
    table = pa.table(
        {
            "id": pa.array([row["id"] for row in rows], type=pa.string()),
            "body": pa.array([row["body"] for row in rows], type=pa.string()),
            "lang": pa.array([row["lang"] for row in rows], type=pa.string()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def write_scores(path: Path, ids: list[str], scores: list[tuple[float, float, float, float, str] | None]) -> None:
    columns: dict[str, list[Any]] = {name: [] for name in SCORE_COLUMNS}
    for score in scores:
        if score is None:
            for name in SCORE_COLUMNS:
                columns[name].append(None)
            continue
        for name, value in zip(SCORE_COLUMNS, score):
            columns[name].append(value)
    table = pa.table(
        {
            "id": pa.array(ids, type=pa.string()),
            "negative": pa.array(columns["negative"], type=pa.float32()),
            "neutral": pa.array(columns["neutral"], type=pa.float32()),
            "positive": pa.array(columns["positive"], type=pa.float32()),
            "sentiment_score": pa.array(columns["sentiment_score"], type=pa.float32()),
            "predicted_label": pa.array(columns["predicted_label"], type=pa.string()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    final_input = pq.read_table(args.final_input)
    cache_input = pq.read_table(args.cache_input)
    require_columns(final_input, args.final_input, ("id", "body", "lang"))
    require_columns(cache_input, args.cache_input, ("id", "body", "lang"))
    final_ids = id_list(final_input)
    if len(set(final_ids)) != len(final_ids):
        raise ValueError("final input contains duplicate IDs")
    cache_ids = id_list(cache_input)
    cache_output = read_score_rows(args.cache_output)
    cached_scores = body_score_map(cache_input, cache_output, "entity cache")

    final_bodies = [body_value(value) for value in final_input["body"].to_pylist()]
    final_langs = [body_value(value) for value in final_input["lang"].to_pylist()]
    reused_rows = sum(body is not None and bool(body.strip()) and body in cached_scores for body in final_bodies)
    reused_bodies = {body for body in final_bodies if body is not None and body.strip() and body in cached_scores}
    novel_body_values = {
        body for body in final_bodies if body is not None and body.strip() and body not in cached_scores
    }
    # Keep one deterministic representative ID/language for each novel body.
    unique_delta: dict[str, dict[str, Any]] = {}
    for index, body in enumerate(final_bodies):
        if body in novel_body_values:
            unique_delta.setdefault(body, {"id": final_ids[index], "body": body, "lang": final_langs[index]})
    delta_rows = list(unique_delta.values())
    if args.prepare_only:
        write_delta(args.delta_input, delta_rows)
        print(
            json.dumps(
                {
                    "final_rows": len(final_ids),
                    "cache_rows": len(cache_ids),
                    "cached_unique_bodies": len(cached_scores),
                    "reused_rows": reused_rows,
                    "reused_unique_bodies": len(reused_bodies),
                    "novel_unique_bodies": len(delta_rows),
                    "delta_input": str(args.delta_input),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    if not args.delta_output.exists():
        raise FileNotFoundError(f"missing scored delta {args.delta_output}; run scorer on {args.delta_input} first")
    delta_input = pq.read_table(args.delta_input)
    require_columns(delta_input, args.delta_input, ("id", "body", "lang"))
    delta_output = read_score_rows(args.delta_output)
    delta_scores = body_score_map(delta_input, delta_output, "novel delta")
    if set(delta_scores) != set(novel_body_values):
        missing = novel_body_values - set(delta_scores)
        extra = set(delta_scores) - novel_body_values
        raise ValueError(f"delta body coverage mismatch; missing={len(missing)} extra={len(extra)}")
    all_scores: list[tuple[float, float, float, float, str] | None] = []
    for body in final_bodies:
        if body is None or not body.strip():
            all_scores.append(None)
        elif body in cached_scores:
            all_scores.append(cached_scores[body])
        else:
            all_scores.append(delta_scores[body])
    for index, score in enumerate(all_scores):
        if score is not None:
            validate_score(score, f"final row {index}")
    write_scores(args.output, final_ids, all_scores)

    cache_meta = load_json(args.cache_output.with_suffix(".json"))
    delta_meta = load_json(args.delta_output.with_suffix(".json"))
    model = (cache_meta or {}).get("model", {})
    if delta_meta and delta_meta.get("model", {}).get("revision") not in (None, model.get("revision")):
        raise ValueError("entity cache and novel delta model revisions differ")
    probabilities = [score for score in all_scores if score is not None]
    output_counts = {label: sum(score[4] == label for score in probabilities) for label in LABELS}
    metadata = {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": "Exact raw body cache reuse from entity sentiment; one novel prediction per body from delta; map body scores back to final unique IDs.",
        "inputs": {
            "final_input": str(args.final_input),
            "entity_cache_input": str(args.cache_input),
            "entity_cache_output": str(args.cache_output),
            "novel_delta_input": str(args.delta_input),
            "novel_delta_output": str(args.delta_output),
        },
        "cache": {
            "final_rows": len(final_ids),
            "final_unique_ids": len(set(final_ids)),
            "entity_cache_rows": len(cache_ids),
            "entity_cache_unique_bodies": len(cached_scores),
            "reused_final_rows": reused_rows,
            "reused_unique_bodies": len(reused_bodies),
            "novel_unique_bodies_scored": len(delta_scores),
            "final_rows_with_null_or_blank_body": sum(body is None or not body.strip() for body in final_bodies),
        },
        "model": model,
        "entity_cache_provenance": cache_meta,
        "novel_delta_provenance": delta_meta,
        "output": {
            "path": str(args.output),
            "rows_written": len(final_ids),
            "label_counts": output_counts,
            "score_range": [
                float(min(score[3] for score in probabilities)) if probabilities else None,
                float(max(score[3] for score in probabilities)) if probabilities else None,
            ],
        },
        "validation": {
            "duplicate_final_ids": 0,
            "missing_body_predictions": sum(score is None for score in all_scores),
            "all_probability_bounds_and_score_identities_checked": True,
            "output_ids_preserve_final_input_order": True,
        },
    }
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": len(final_ids),
                "reused_rows": reused_rows,
                "novel_unique_bodies": len(delta_scores),
                "labels": output_counts,
                "output": str(args.output),
                "metadata": str(args.metadata_output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
