# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2.0", "pyarrow>=20"]
# ///
"""Stream the ordered Twitter firehose and rank repeated-observation trajectories.

The source shards are globally nondecreasing by numeric tweet ID.  This script
uses that invariant rather than an all-ID hash table: only the current ID group
is carried across batch/file boundaries.  All completed groups are reduced into
small counters, while a bounded heap retains auditable outlier trajectories.

Usage:
    uv run topic-analysis/analyze_trajectories.py [dataset] [output]

The output directory receives:
  trajectory-outliers.parquet / .csv  bounded, auditable trajectory rows
  trajectory-summary.json       exact scan and ranking metadata
  trajectory-coverage-by-day-lang.csv
  trajectory-duration-bins.csv
  trajectory-topic-candidates.csv
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


METRICS = (
    "like_count",
    "reply_count",
    "retweet_count",
    "quote_count",
    "views_count",
    "bookmarks_count",
)
METRIC_SHORT = {
    "like_count": "like",
    "reply_count": "reply",
    "retweet_count": "retweet",
    "quote_count": "quote",
    "views_count": "views",
    "bookmarks_count": "bookmarks",
}
# Fields needed to classify posts, preserve audit text, and compute trajectories.
COLUMNS = (
    "id",
    "author_id",
    "body",
    "created_at",
    *METRICS,
    "lang",
    "reply_to_status_id",
    "quoting_id",
    "version",
    "added_at",
)
METRIC_INDEX = {name: COLUMNS.index(name) for name in METRICS}
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

POST_TYPES = ("original", "quote", "reply", "rt_prefixed")
POST_TYPE_INDEX = {name: i for i, name in enumerate(POST_TYPES)}

# A lexical hashtag is deliberately only a candidate topic signal.  It is not
# a semantic event label and no external event names are inferred here.
HASHTAG_RE = re.compile(r"(?<!\w)#[\w]+", re.UNICODE)


@dataclass
class GroupSummary:
    """One completed or carried ID group.

    Values are Python scalars or small tuples.  Numeric arrays are used for the
    bulk of each batch; this object exists only for a single boundary group or
    an outlier retained by a heap.
    """

    id: str
    author_id: str | None
    body: str | None
    lang: str | None
    created_ms: int | None
    first_version_ms: int
    last_version_ms: int
    first_metrics: tuple[float, ...]
    last_metrics: tuple[float, ...]
    snapshots: int
    post_type: str
    is_rt_prefixed: bool
    negative_counts: tuple[int, ...]
    positive_counts: tuple[int, ...]
    same_version_transitions: int
    version_descents: int
    interval_min_seconds: float | None
    interval_max_seconds: float | None
    interval_sum_seconds: float
    interval_count: int
    body_first_row: int | None = None
    trajectory_rows: tuple[tuple[int, int, tuple[float, ...]], ...] | None = None


@dataclass
class BatchData:
    """Vectorized arrays for one Arrow batch."""

    arrays: list[pa.Array]
    ids_numeric: np.ndarray
    starts: np.ndarray
    ends: np.ndarray
    lengths: np.ndarray
    created_ms: np.ndarray
    version_ms: np.ndarray
    added_ms: np.ndarray
    metrics: dict[str, np.ndarray]
    rt_mask: np.ndarray
    reply_mask: np.ndarray
    quote_mask: np.ndarray
    lang_codes: np.ndarray
    lang_labels: list[str]
    group_negative: dict[str, np.ndarray]
    group_positive: dict[str, np.ndarray]
    group_same_version: np.ndarray
    group_version_descents: np.ndarray
    group_interval_min: np.ndarray
    group_interval_max: np.ndarray
    group_interval_sum: np.ndarray
    group_interval_count: np.ndarray


class TopRanks:
    """Bounded top-K heaps, creating full records only at heap admission."""

    def __init__(self, top_k: int):
        self.top_k = top_k
        self.heaps: dict[str, list[tuple[float, int, str, dict[str, Any]]]] = defaultdict(list)
        self.sequence = 0
        self.records: dict[str, dict[str, Any]] = {}
        self.reasons: dict[str, list[str]] = defaultdict(list)

    def maybe_add(self, name: str, score: float | None, group: GroupSummary, reason: str) -> None:
        if score is None or not math.isfinite(score):
            return
        heap = self.heaps[name]
        current_id = group.id
        if len(heap) >= self.top_k and score <= heap[0][0]:
            return
        record = self.records.get(current_id)
        if record is None:
            record = make_record(group)
            self.records[current_id] = record
        self.sequence += 1
        item = (float(score), self.sequence, current_id, record)
        if len(heap) >= self.top_k:
            heapq.heapreplace(heap, item)
        else:
            heapq.heappush(heap, item)
        self.reasons[current_id].append(reason)

    def finalize(self) -> list[dict[str, Any]]:
        ranks: dict[str, list[str]] = defaultdict(list)
        # Sort descending and assign a rank within each independently ranked list.
        for name, heap in self.heaps.items():
            ranked = sorted(heap, key=lambda item: (-item[0], item[1]))
            for rank, (_, _, tweet_id, _) in enumerate(ranked, 1):
                ranks[tweet_id].append(f"{name}:{rank}")
        rows: list[dict[str, Any]] = []
        # A heap admission can later be evicted.  Emit only IDs still present
        # in at least one finalized heap, so each output row is auditable as a
        # true top-K selection rather than a stale admission.
        for tweet_id, rank_names in ranks.items():
            record = self.records[tweet_id]
            row = dict(record)
            row["selection_reasons"] = ";".join(sorted(rank_names))
            row["selection_count"] = len(rank_names)
            rows.append(row)
        rows.sort(key=lambda row: (-row["selection_count"], row["id"]))
        return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", default="twitter-firehose", type=Path)
    parser.add_argument("output", nargs="?", default="topic-analysis", type=Path)
    parser.add_argument("--batch-size", type=int, default=262_144)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--expected-observations", type=int, default=395_352_258)
    parser.add_argument("--sample", type=Path, default=Path("twitter-analysis/tweet-id-sample.parquet"))
    return parser.parse_args()


def as_int_ms(values: pa.Array) -> np.ndarray:
    """Convert Arrow timestamp[ms] to int64 milliseconds; NaT becomes INT64_MIN."""
    result = values.to_numpy(zero_copy_only=False).astype("datetime64[ms]").astype(np.int64, copy=False)
    return np.asarray(result, dtype=np.int64)


def as_float(values: pa.Array) -> np.ndarray:
    """Convert nullable integer metrics to float64, where NaN represents null."""
    return np.asarray(values.to_numpy(zero_copy_only=False), dtype=np.float64)


def bool_array(values: pa.Array) -> np.ndarray:
    return np.asarray(values.to_numpy(zero_copy_only=False), dtype=bool)


def string_present_mask(values: pa.Array) -> np.ndarray:
    return np.asarray(values.is_valid().to_numpy(zero_copy_only=False), dtype=bool)


def dictionary_codes(values: pa.Array, global_labels: dict[str, int], labels: list[str]) -> np.ndarray:
    """Dictionary-encode one batch and remap to stable global language codes."""
    encoded = pc.dictionary_encode(values)
    local_labels = encoded.dictionary.to_pylist()
    local_to_global: list[int] = []
    for label in local_labels:
        text = "<NULL>" if label is None else str(label)
        if text not in global_labels:
            global_labels[text] = len(labels)
            labels.append(text)
        local_to_global.append(global_labels[text])
    local_indices = np.asarray(encoded.indices.to_numpy(zero_copy_only=False), dtype=np.float64)
    valid = np.asarray(encoded.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
    result = np.full(len(values), -1, dtype=np.int64)
    if local_to_global:
        safe = np.nan_to_num(local_indices, nan=0.0).astype(np.int64, copy=False)
        result[valid] = np.take(np.asarray(local_to_global, dtype=np.int64), safe[valid])
    null_code = global_labels.setdefault("<NULL>", len(labels))
    if len(labels) == null_code:
        labels.append("<NULL>")
    result[~valid] = null_code
    return result


def group_boundaries(ids: pa.Array) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(ids)
    if n == 0:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty, empty
    if n == 1:
        one = np.array([0], dtype=np.int64)
        return one, one, one
    equal = np.asarray(pc.equal(ids.slice(1), ids.slice(0, n - 1)).to_numpy(zero_copy_only=False), dtype=bool)
    boundary = np.flatnonzero(~equal).astype(np.int64, copy=False)
    starts = np.concatenate((np.array([0], dtype=np.int64), boundary + 1))
    ends = np.concatenate((boundary, np.array([n - 1], dtype=np.int64)))
    lengths = ends - starts + 1
    return starts, ends, lengths


def classify_post_type(rt: bool, reply: bool, quote: bool) -> str:
    if rt:
        return "rt_prefixed"
    if reply:
        return "reply"
    if quote:
        return "quote"
    return "original"


def group_transition_stats(
    lengths: np.ndarray,
    version_ms: np.ndarray,
    metrics: dict[str, np.ndarray],
) -> tuple[
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """Reduce adjacent transitions by local group without assuming metric monotonicity."""
    groups = len(lengths)
    if groups == 0:
        empty_i = np.empty(0, dtype=np.int64)
        empty_f = np.empty(0, dtype=np.float64)
        return ({}, {}, empty_i, empty_i, empty_f, empty_f, empty_f, empty_i)
    # A batch is at most 262k rows by default.  This index vector is bounded and
    # lets np.bincount reduce all groups without a Python loop over singleton IDs.
    row_group = np.repeat(np.arange(groups, dtype=np.int64), lengths)
    same_group = row_group[1:] == row_group[:-1]
    transition_group = row_group[:-1][same_group]
    vdiff = version_ms[1:] - version_ms[:-1]
    vdiff = vdiff[same_group]
    same_version = np.bincount(
        transition_group,
        weights=(vdiff == 0).astype(np.int64),
        minlength=groups,
    ).astype(np.int64, copy=False)
    version_descents = np.bincount(
        transition_group,
        weights=(vdiff < 0).astype(np.int64),
        minlength=groups,
    ).astype(np.int64, copy=False)
    nonnegative = vdiff >= 0
    positive_intervals = vdiff[nonnegative].astype(np.float64, copy=False) / 1000.0
    positive_groups = transition_group[nonnegative]
    interval_count = np.bincount(positive_groups, minlength=groups).astype(np.int64, copy=False)
    interval_sum = np.bincount(positive_groups, weights=positive_intervals, minlength=groups).astype(np.float64, copy=False)
    interval_min = np.full(groups, np.nan, dtype=np.float64)
    interval_max = np.full(groups, np.nan, dtype=np.float64)
    if len(positive_intervals):
        np.minimum.at(interval_min, positive_groups, positive_intervals)
        np.maximum.at(interval_max, positive_groups, positive_intervals)
    negative: dict[str, np.ndarray] = {}
    positive: dict[str, np.ndarray] = {}
    for name, values in metrics.items():
        diff = values[1:] - values[:-1]
        diff = diff[same_group]
        valid = np.isfinite(values[1:][same_group]) & np.isfinite(values[:-1][same_group])
        negative[name] = np.bincount(
            transition_group[valid],
            weights=(diff[valid] < 0).astype(np.int64),
            minlength=groups,
        ).astype(np.int64, copy=False)
        positive[name] = np.bincount(
            transition_group[valid],
            weights=(diff[valid] > 0).astype(np.int64),
            minlength=groups,
        ).astype(np.int64, copy=False)
    return negative, positive, same_version, version_descents, interval_min, interval_max, interval_sum, interval_count


def prepare_batch(
    batch: pa.RecordBatch,
    global_lang_codes: dict[str, int],
    lang_labels: list[str],
) -> BatchData:
    arrays = list(batch.columns)
    ids = arrays[0]
    starts, ends, lengths = group_boundaries(ids)
    ids_numeric = np.asarray(pc.cast(ids, pa.uint64(), safe=False).to_numpy(zero_copy_only=False), dtype=np.uint64)
    created_ms = as_int_ms(arrays[3])
    version_ms = as_int_ms(arrays[-2])
    added_ms = as_int_ms(arrays[-1])
    metrics = {name: as_float(arrays[METRIC_INDEX[name]]) for name in METRICS}
    body = arrays[2]
    rt_result = pc.starts_with(body, "RT @")
    rt_mask = np.asarray(pc.fill_null(rt_result, False).to_numpy(zero_copy_only=False), dtype=bool)
    reply_values = arrays[COLUMNS.index("reply_to_status_id")]
    quote_values = arrays[COLUMNS.index("quoting_id")]
    reply_mask = np.asarray(
        pc.fill_null(pc.not_equal(reply_values, ""), False).to_numpy(zero_copy_only=False), dtype=bool
    )
    quote_mask = np.asarray(
        pc.fill_null(pc.not_equal(quote_values, ""), False).to_numpy(zero_copy_only=False), dtype=bool
    )
    lang_codes = dictionary_codes(arrays[COLUMNS.index("lang")], global_lang_codes, lang_labels)
    neg, pos, same, descents, interval_min, interval_max, interval_sum, interval_count = group_transition_stats(
        lengths, version_ms, metrics
    )
    return BatchData(
        arrays=arrays,
        ids_numeric=ids_numeric,
        starts=starts,
        ends=ends,
        lengths=lengths,
        created_ms=created_ms,
        version_ms=version_ms,
        added_ms=added_ms,
        metrics=metrics,
        rt_mask=rt_mask,
        reply_mask=reply_mask,
        quote_mask=quote_mask,
        lang_codes=lang_codes,
        lang_labels=lang_labels,
        group_negative=neg,
        group_positive=pos,
        group_same_version=same,
        group_version_descents=descents,
        group_interval_min=interval_min,
        group_interval_max=interval_max,
        group_interval_sum=interval_sum,
        group_interval_count=interval_count,
    )


def scalar_from_array(values: pa.Array, index: int) -> Any:
    return values[index].as_py()


def maybe_int(value: float | int | None) -> int | None:
    if value is None:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
    except (TypeError, ValueError):
        return None
    return int(value)


def maybe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def datetime_from_ms(value: int | None) -> datetime | None:
    if value is None or value <= np.iinfo(np.int64).min // 2:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=UTC)


def body_excerpt(body: str | None, limit: int = 320) -> str | None:
    if body is None:
        return None
    return body[:limit]


def ordered_summary(
    base: GroupSummary,
    raw_rows: tuple[tuple[int, int, tuple[float, ...]], ...],
    keep_rows: bool,
) -> GroupSummary:
    """Recompute endpoints and transitions in (version, added_at) order.

    The firehose is ordered by tweet ID, not version.  Repeated IDs are small
    enough to sort locally; singleton IDs stay on the vectorized fast path.
    """
    if not raw_rows:
        return base
    source_descents = sum(
        int(raw_rows[i][0] < raw_rows[i - 1][0]) for i in range(1, len(raw_rows))
    )
    order = sorted(range(len(raw_rows)), key=lambda i: (raw_rows[i][0], raw_rows[i][1], i))
    ordered = tuple(raw_rows[i] for i in order)
    versions = np.asarray([row[0] for row in ordered], dtype=np.int64)
    metric_arrays = [
        np.asarray([row[2][metric_index] for row in ordered], dtype=np.float64)
        for metric_index in range(len(METRICS))
    ]
    negative_counts = tuple(
        sum(
            int(math.isfinite(float(values[i - 1])))
            * int(math.isfinite(float(values[i])))
            * int(values[i] < values[i - 1])
            for i in range(1, len(values))
        )
        for values in metric_arrays
    )
    positive_counts = tuple(
        sum(
            int(math.isfinite(float(values[i - 1])))
            * int(math.isfinite(float(values[i])))
            * int(values[i] > values[i - 1])
            for i in range(1, len(values))
        )
        for values in metric_arrays
    )
    intervals = np.diff(versions).astype(np.float64) / 1000.0
    return replace(
        base,
        first_version_ms=int(ordered[0][0]),
        last_version_ms=int(ordered[-1][0]),
        first_metrics=tuple(float(value) for value in ordered[0][2]),
        last_metrics=tuple(float(value) for value in ordered[-1][2]),
        snapshots=len(ordered),
        negative_counts=negative_counts,
        positive_counts=positive_counts,
        same_version_transitions=int(np.count_nonzero(intervals == 0)),
        version_descents=source_descents,
        interval_min_seconds=float(np.min(intervals)) if len(intervals) else None,
        interval_max_seconds=float(np.max(intervals)) if len(intervals) else None,
        interval_sum_seconds=float(np.sum(intervals)) if len(intervals) else 0.0,
        interval_count=len(intervals),
        trajectory_rows=raw_rows if keep_rows else None,
    )


def group_from_batch(
    data: BatchData,
    index: int,
    lang_labels: list[str],
    *,
    retain_rows: bool = False,
) -> GroupSummary:
    start = int(data.starts[index])
    end = int(data.ends[index])
    first_metrics = tuple(float(data.metrics[name][start]) for name in METRICS)
    last_metrics = tuple(float(data.metrics[name][end]) for name in METRICS)
    created_value = int(data.created_ms[start])
    created_ms = None if created_value <= np.iinfo(np.int64).min // 2 else created_value
    body = scalar_from_array(data.arrays[2], start)
    author = scalar_from_array(data.arrays[1], start)
    lang_code = int(data.lang_codes[start])
    lang = lang_labels[lang_code] if 0 <= lang_code < len(lang_labels) else "<NULL>"
    post_type = classify_post_type(bool(data.rt_mask[start]), bool(data.reply_mask[start]), bool(data.quote_mask[start]))
    base = GroupSummary(
        id=str(scalar_from_array(data.arrays[0], start)),
        author_id=None if author is None else str(author),
        body=None if body is None else str(body),
        lang=lang,
        created_ms=created_ms,
        first_version_ms=int(data.version_ms[start]),
        last_version_ms=int(data.version_ms[end]),
        first_metrics=first_metrics,
        last_metrics=last_metrics,
        snapshots=int(data.lengths[index]),
        post_type=post_type,
        is_rt_prefixed=post_type == "rt_prefixed",
        negative_counts=tuple(int(data.group_negative[name][index]) for name in METRICS),
        positive_counts=tuple(int(data.group_positive[name][index]) for name in METRICS),
        same_version_transitions=int(data.group_same_version[index]),
        version_descents=int(data.group_version_descents[index]),
        interval_min_seconds=maybe_float(data.group_interval_min[index]),
        interval_max_seconds=maybe_float(data.group_interval_max[index]),
        interval_sum_seconds=float(data.group_interval_sum[index]),
        interval_count=int(data.group_interval_count[index]),
        body_first_row=start,
    )
    source_order_needs_sort = (
        int(data.lengths[index]) > 1
        and (int(data.group_version_descents[index]) > 0 or int(data.group_same_version[index]) > 0)
    )
    if not (retain_rows or source_order_needs_sort):
        return base
    raw_rows = tuple(
        (
            int(data.version_ms[row_index]),
            int(data.added_ms[row_index]),
            tuple(float(data.metrics[name][row_index]) for name in METRICS),
        )
        for row_index in range(start, end + 1)
    )
    return ordered_summary(base, raw_rows, retain_rows)


def transition_negative(previous: float, current: float) -> int:
    return int(math.isfinite(previous) and math.isfinite(current) and current - previous < 0)


def transition_positive(previous: float, current: float) -> int:
    return int(math.isfinite(previous) and math.isfinite(current) and current - previous > 0)


def merge_groups(previous: GroupSummary, current: GroupSummary) -> GroupSummary:
    """Merge a boundary ID group, sorting all retained rows by version."""
    previous_rows = previous.trajectory_rows
    current_rows = current.trajectory_rows
    if previous_rows is None:
        previous_rows = ((previous.first_version_ms, 0, previous.first_metrics),)
    if current_rows is None:
        current_rows = ((current.first_version_ms, 0, current.first_metrics),)
    base = GroupSummary(
        id=previous.id,
        author_id=previous.author_id,
        body=previous.body,
        lang=previous.lang,
        created_ms=previous.created_ms,
        first_version_ms=previous.first_version_ms,
        last_version_ms=current.last_version_ms,
        first_metrics=previous.first_metrics,
        last_metrics=current.last_metrics,
        snapshots=previous.snapshots + current.snapshots,
        post_type=previous.post_type,
        is_rt_prefixed=previous.is_rt_prefixed,
        negative_counts=previous.negative_counts,
        positive_counts=previous.positive_counts,
        same_version_transitions=0,
        version_descents=0,
        interval_min_seconds=None,
        interval_max_seconds=None,
        interval_sum_seconds=0.0,
        interval_count=0,
        body_first_row=previous.body_first_row,
    )
    return ordered_summary(base, previous_rows + current_rows, True)


def metric_delta(group: GroupSummary, metric_index: int) -> float | None:
    first, last = group.first_metrics[metric_index], group.last_metrics[metric_index]
    if not math.isfinite(first) or not math.isfinite(last):
        return None
    return last - first


def duration_hours(group: GroupSummary) -> float | None:
    delta_ms = group.last_version_ms - group.first_version_ms
    if delta_ms < 0:
        return None
    return delta_ms / HOUR_MS


def per_hour(group: GroupSummary, metric_index: int) -> float | None:
    duration = duration_hours(group)
    delta = metric_delta(group, metric_index)
    if duration is None or duration < 1.0 or delta is None:
        return None
    return delta / duration


def cadence_ratio(group: GroupSummary) -> float | None:
    if group.interval_min_seconds is None or group.interval_max_seconds is None:
        return None
    if group.interval_min_seconds <= 0:
        return None
    return group.interval_max_seconds / group.interval_min_seconds


def make_record(group: GroupSummary) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": group.id,
        "author_id": group.author_id,
        "body_excerpt": body_excerpt(group.body),
        "lang": group.lang,
        "created_at": datetime_from_ms(group.created_ms),
        "post_type": group.post_type,
        "is_rt_prefixed": group.is_rt_prefixed,
        "snapshot_count": group.snapshots,
        "first_version": datetime_from_ms(group.first_version_ms),
        "last_version": datetime_from_ms(group.last_version_ms),
        "duration_hours": duration_hours(group),
        "interval_min_seconds": group.interval_min_seconds,
        "interval_max_seconds": group.interval_max_seconds,
        "interval_mean_seconds": (
            group.interval_sum_seconds / group.interval_count if group.interval_count else None
        ),
        "cadence_ratio_max_min": cadence_ratio(group),
        "same_version_transitions": group.same_version_transitions,
        "version_descents": group.version_descents,
        "selection_reasons": "",
        "selection_count": 0,
    }
    for i, metric in enumerate(METRICS):
        short = METRIC_SHORT[metric]
        row[f"first_{metric}"] = maybe_int(group.first_metrics[i])
        row[f"last_{metric}"] = maybe_int(group.last_metrics[i])
        row[f"{short}_delta"] = metric_delta(group, i)
        row[f"{short}_per_hour"] = per_hour(group, i)
        row[f"negative_{short}_transitions"] = group.negative_counts[i]
        row[f"positive_{short}_transitions"] = group.positive_counts[i]
    return row


def add_counter(counter: Counter, key: Any, value: int = 1) -> None:
    counter[key] += value


def day_label(day_code: int) -> str:
    if day_code < 0:
        return "<NULL>"
    return (date(1970, 1, 1) + timedelta(days=int(day_code))).isoformat()


def coverage_update_vectorized(
    coverage: dict[tuple[int, int], list[int]],
    day_codes: np.ndarray,
    lang_codes: np.ndarray,
    lengths: np.ndarray,
    global_indices: np.ndarray,
    duration_ge_1h: np.ndarray,
) -> None:
    if len(global_indices) == 0:
        return
    days = day_codes[global_indices]
    langs = lang_codes[global_indices]
    lens = lengths[global_indices].astype(np.int64, copy=False)
    # Duration is recomputed from version-sorted GroupSummary objects below;
    # source-order endpoints are not valid for this field.
    long = np.zeros(len(global_indices), dtype=np.int64)
    # 1,000,000 is safely above expected language codes and keeps tuple keys
    # compact while preserving vectorized grouping.
    keys = days.astype(np.int64) * 1_000_000 + langs.astype(np.int64)
    unique, inverse = np.unique(keys, return_inverse=True)
    tweets = np.bincount(inverse, minlength=len(unique)).astype(np.int64)
    observations = np.bincount(inverse, weights=lens, minlength=len(unique)).astype(np.int64)
    repeated = (lens >= 2).astype(np.int64)
    repeated_tweets = np.bincount(inverse, weights=repeated, minlength=len(unique)).astype(np.int64)
    repeated_observations = np.bincount(
        inverse, weights=np.where(repeated, lens, 0), minlength=len(unique)
    ).astype(np.int64)
    long_tweets = np.bincount(inverse, weights=long, minlength=len(unique)).astype(np.int64)
    for key, n_tweets, n_obs, n_rep_tweets, n_rep_obs, n_long in zip(
        unique.tolist(), tweets, observations, repeated_tweets, repeated_observations, long_tweets
    ):
        day = int(key // 1_000_000)
        lang = int(key % 1_000_000)
        current = coverage.setdefault((day, lang), [0, 0, 0, 0, 0, 0])
        current[0] += int(n_tweets)
        current[1] += int(n_obs)
        current[2] += int(n_rep_tweets)
        current[3] += int(n_rep_obs)
        current[4] += int(n_rep_obs - n_rep_tweets)
        # tweets_duration_ge_1h is updated after exact per-ID version sorting.


def coverage_update_one(
    coverage: dict[tuple[int, int], list[int]],
    group: GroupSummary,
    lang_labels: list[str],
) -> None:
    """Account for a carried boundary group exactly once."""
    day_code = -1 if group.created_ms is None else group.created_ms // DAY_MS
    try:
        lang_code = lang_labels.index(group.lang or "<NULL>")
    except ValueError:
        lang_code = len(lang_labels)
        lang_labels.append(group.lang or "<NULL>")
    key = (int(day_code), int(lang_code))
    current = coverage.setdefault(key, [0, 0, 0, 0, 0, 0])
    current[0] += 1
    current[1] += group.snapshots
    if group.snapshots >= 2:
        current[2] += 1
        current[3] += group.snapshots
        current[4] += group.snapshots - 1


def coverage_update_duration(
    coverage: dict[tuple[int, int], list[int]],
    group: GroupSummary,
    lang_labels: list[str],
) -> None:
    if group.snapshots < 2 or (duration_hours(group) or 0.0) < 1.0:
        return
    day_code = -1 if group.created_ms is None else group.created_ms // DAY_MS
    try:
        lang_code = lang_labels.index(group.lang or "<NULL>")
    except ValueError:
        lang_code = len(lang_labels)
        lang_labels.append(group.lang or "<NULL>")
    coverage.setdefault((int(day_code), int(lang_code)), [0, 0, 0, 0, 0, 0])[5] += 1


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            converted = {}
            for field in fieldnames:
                value = row.get(field)
                if isinstance(value, datetime):
                    value = value.isoformat().replace("+00:00", "Z")
                converted[field] = value
            writer.writerow(converted)


def write_outlier_outputs(out: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "id",
        "author_id",
        "body_excerpt",
        "lang",
        "created_at",
        "post_type",
        "is_rt_prefixed",
        "snapshot_count",
        "first_version",
        "last_version",
        "duration_hours",
        "interval_min_seconds",
        "interval_max_seconds",
        "interval_mean_seconds",
        "cadence_ratio_max_min",
        "same_version_transitions",
        "version_descents",
        *[field for metric in METRICS for field in (
            f"first_{metric}",
            f"last_{metric}",
            f"{METRIC_SHORT[metric]}_delta",
            f"{METRIC_SHORT[metric]}_per_hour",
            f"negative_{METRIC_SHORT[metric]}_transitions",
            f"positive_{METRIC_SHORT[metric]}_transitions",
        )],
        "selection_reasons",
        "selection_count",
    ]
    parquet_rows = []
    for row in rows:
        parquet_rows.append({field: row.get(field) for field in fieldnames})
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("author_id", pa.string()),
            pa.field("body_excerpt", pa.string()),
            pa.field("lang", pa.string()),
            pa.field("created_at", pa.timestamp("ms", tz="UTC")),
            pa.field("post_type", pa.string()),
            pa.field("is_rt_prefixed", pa.bool_()),
            pa.field("snapshot_count", pa.int64()),
            pa.field("first_version", pa.timestamp("ms", tz="UTC")),
            pa.field("last_version", pa.timestamp("ms", tz="UTC")),
            pa.field("duration_hours", pa.float64()),
            pa.field("interval_min_seconds", pa.float64()),
            pa.field("interval_max_seconds", pa.float64()),
            pa.field("interval_mean_seconds", pa.float64()),
            pa.field("cadence_ratio_max_min", pa.float64()),
            pa.field("same_version_transitions", pa.int64()),
            pa.field("version_descents", pa.int64()),
            *[
                pa.field(field, pa.int64() if field.startswith(("first_", "last_", "negative_", "positive_")) else pa.float64())
                for metric in METRICS
                for field in (
                    f"first_{metric}",
                    f"last_{metric}",
                    f"{METRIC_SHORT[metric]}_delta",
                    f"{METRIC_SHORT[metric]}_per_hour",
                    f"negative_{METRIC_SHORT[metric]}_transitions",
                    f"positive_{METRIC_SHORT[metric]}_transitions",
                )
            ],
            pa.field("selection_reasons", pa.string()),
            pa.field("selection_count", pa.int64()),
        ]
    )
    table = pa.Table.from_pylist(parquet_rows, schema=schema)
    pq.write_table(table, out / "trajectory-outliers.parquet", compression="zstd")
    csv_rows = []
    for row in rows:
        csv_row = dict(row)
        for key, value in list(csv_row.items()):
            if isinstance(value, datetime):
                csv_row[key] = value.isoformat().replace("+00:00", "Z")
        csv_rows.append(csv_row)
    write_csv(out / "trajectory-outliers.csv", csv_rows, fieldnames)


def extract_latest_sample_hashtags(sample_path: Path) -> Counter[str]:
    """Read the deterministic sample and reduce it to latest text topics."""
    counts: Counter[str] = Counter()
    if not sample_path.exists():
        return counts
    latest: dict[str, tuple[int, str]] = {}
    sample_file = pq.ParquetFile(sample_path)
    for batch in sample_file.iter_batches(batch_size=262_144, columns=["id", "version", "body"]):
        ids = batch.column(0).to_pylist()
        versions = as_int_ms(batch.column(1))
        bodies = batch.column(2).to_pylist()
        for tweet_id, version, body in zip(ids, versions, bodies):
            if body is None:
                continue
            current = latest.get(str(tweet_id))
            if current is None or int(version) > current[0]:
                latest[str(tweet_id)] = (int(version), str(body))
    for _, body in latest.values():
        counts.update(tag.casefold() for tag in HASHTAG_RE.findall(body))
    return counts


def write_topic_candidates(out: Path, rows: list[dict[str, Any]], sample_counts: Counter[str]) -> None:
    outlier_counts: Counter[str] = Counter()
    outlier_like_delta: Counter[str] = Counter()
    outlier_views_delta: Counter[str] = Counter()
    for row in rows:
        body = row.get("body_excerpt") or ""
        tags = {tag.casefold() for tag in HASHTAG_RE.findall(body)}
        for tag in tags:
            outlier_counts[tag] += 1
            like_delta = row.get("like_delta")
            views_delta = row.get("views_delta")
            if isinstance(like_delta, (int, float)) and math.isfinite(float(like_delta)):
                outlier_like_delta[tag] += int(like_delta)
            if isinstance(views_delta, (int, float)) and math.isfinite(float(views_delta)):
                outlier_views_delta[tag] += int(views_delta)
    tags = sorted(set(outlier_counts) | set(sample_counts), key=lambda tag: (-outlier_counts[tag], -sample_counts[tag], tag))
    records = []
    for tag in tags[:200]:
        out_n = outlier_counts[tag]
        sample_n = sample_counts[tag]
        records.append(
            {
                "hashtag": tag,
                "outlier_tweets": out_n,
                "sample_latest_tweets": sample_n,
                "sample_supported": bool(sample_n),
                "comparison": "both" if out_n and sample_n else ("outlier_only" if out_n else "sample_only"),
                "outlier_like_delta_sum": outlier_like_delta[tag],
                "outlier_views_delta_sum": outlier_views_delta[tag],
                "note": "Lexical hashtag overlap only; not a verified semantic event or external cause.",
            }
        )
    write_csv(
        out / "trajectory-topic-candidates.csv",
        records,
        [
            "hashtag",
            "outlier_tweets",
            "sample_latest_tweets",
            "sample_supported",
            "comparison",
            "outlier_like_delta_sum",
            "outlier_views_delta_sum",
            "note",
        ],
    )


def scan(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]], Counter[str], dict[tuple[int, int], list[int]], list[str]]:
    args.output.mkdir(parents=True, exist_ok=True)
    files = sorted(args.dataset.glob("tweets-*.parquet"), key=lambda path: int(path.stem.split("-")[-1]))
    if not files:
        raise FileNotFoundError(f"No tweets-*.parquet files under {args.dataset}")
    expected_metadata_rows = sum(pq.ParquetFile(path).metadata.num_rows for path in files)
    print(f"Scanning {len(files)} shards ({expected_metadata_rows:,} metadata rows) with {args.threads} threads", flush=True)
    global_lang_codes: dict[str, int] = {}
    lang_labels: list[str] = []
    coverage: dict[tuple[int, int], list[int]] = {}
    duration_bins: Counter[str] = Counter()
    snapshots_distribution: Counter[int] = Counter()
    post_type_counts: dict[str, list[int]] = {name: [0, 0, 0] for name in POST_TYPES}
    metric_negative_totals: Counter[str] = Counter()
    metric_positive_totals: Counter[str] = Counter()
    cadence_totals: Counter[str] = Counter()
    ranks = TopRanks(args.top_k)
    carry: GroupSummary | None = None
    previous_numeric_id: int | None = None
    total_rows = 0
    distinct_tweets = 0
    repeated_tweets = 0
    repeated_observations = 0
    first_creation_ms: int | None = None
    last_creation_ms: int | None = None
    first_version_ms: int | None = None
    last_version_ms: int | None = None
    id_descents = 0
    version_descents = 0
    same_version_transitions = 0
    def register_boundary_group(group: GroupSummary) -> None:
        # The last group of every batch is intentionally withheld for possible
        # cross-batch continuation, so its coverage is not in the vectorized
        # reduction.  Add it when the group is finally complete.
        coverage_update_one(coverage, group, lang_labels)
        register_group(group)

    started = time.monotonic()
    last_report = started

    def register_group(group: GroupSummary) -> None:
        nonlocal distinct_tweets, repeated_tweets, repeated_observations, first_creation_ms, last_creation_ms
        distinct_tweets += 1
        snapshots_distribution[group.snapshots] += 1
        if group.snapshots >= 2:
            repeated_tweets += 1
            repeated_observations += group.snapshots
        if group.created_ms is not None:
            first_creation_ms = group.created_ms if first_creation_ms is None else min(first_creation_ms, group.created_ms)
            last_creation_ms = group.created_ms if last_creation_ms is None else max(last_creation_ms, group.created_ms)
        post_counts = post_type_counts[group.post_type]
        post_counts[0] += 1
        post_counts[1] += group.snapshots
        post_counts[2] += int(group.snapshots >= 2)
        register_repeated_details(group)

    def register_repeated_details(group: GroupSummary) -> None:
        """Process only repeated IDs; singleton groups stay vectorized."""
        nonlocal version_descents, same_version_transitions
        coverage_update_duration(coverage, group, lang_labels)
        version_descents += group.version_descents
        same_version_transitions += group.same_version_transitions
        if group.snapshots < 2:
            return
        for metric, neg, pos in zip(METRICS, group.negative_counts, group.positive_counts):
            metric_negative_totals[metric] += neg
            metric_positive_totals[metric] += pos
        hours = duration_hours(group)
        if hours is not None:
            if hours < 1:
                duration_bins["<1h"] += 1
            elif hours < 6:
                duration_bins["1-6h"] += 1
            elif hours < 24:
                duration_bins["6-24h"] += 1
            elif hours < 72:
                duration_bins["1-3d"] += 1
            elif hours < 168:
                duration_bins["3-7d"] += 1
            else:
                duration_bins[">=7d"] += 1
        if group.same_version_transitions:
            cadence_totals["groups_with_same_version_transition"] += 1
        if group.version_descents:
            cadence_totals["groups_with_version_descent"] += 1
        ratio = cadence_ratio(group)
        if ratio is not None and ratio >= 10:
            cadence_totals["groups_cadence_ratio_ge_10"] += 1
        if group.interval_max_seconds is not None and group.interval_max_seconds >= 24 * 3600:
            cadence_totals["groups_with_gap_ge_24h"] += 1
        # Rank only after exact version-sorted reduction and with bounded heaps.
        rank_specs: list[tuple[str, float | None, str]] = []
        if group.post_type == "original":
            rank_specs.extend(
                [
                    ("original_absolute_like_delta", metric_delta(group, 0), "original:abs_like"),
                    ("original_absolute_views_delta", metric_delta(group, 4), "original:abs_views"),
                    ("original_absolute_retweet_delta", metric_delta(group, 2), "original:abs_retweet"),
                    ("original_per_hour_like_delta", per_hour(group, 0), "original:hour_like"),
                    ("original_per_hour_views_delta", per_hour(group, 4), "original:hour_views"),
                    ("original_per_hour_retweet_delta", per_hour(group, 2), "original:hour_retweet"),
                ]
            )
        else:
            rank_specs.extend(
                [
                    (f"{group.post_type}_absolute_like_delta", metric_delta(group, 0), f"{group.post_type}:abs_like"),
                    (f"{group.post_type}_absolute_views_delta", metric_delta(group, 4), f"{group.post_type}:abs_views"),
                    (f"{group.post_type}_per_hour_like_delta", per_hour(group, 0), f"{group.post_type}:hour_like"),
                    (f"{group.post_type}_per_hour_views_delta", per_hour(group, 4), f"{group.post_type}:hour_views"),
                ]
            )
        for metric_index, metric in enumerate(METRICS):
            delta = metric_delta(group, metric_index)
            if delta is not None and delta < 0:
                if metric != "retweet_count" or not group.is_rt_prefixed:
                    rank_specs.append((f"negative_{METRIC_SHORT[metric]}_delta", -delta, f"anomaly:negative_{METRIC_SHORT[metric]}"))
        if group.snapshots >= 3:
            rank_specs.append(("cadence_ratio_max_min", cadence_ratio(group), "cadence:ratio"))
            rank_specs.append(("cadence_max_gap_seconds", group.interval_max_seconds, "cadence:max_gap"))
        for name, score, reason in rank_specs:
            ranks.maybe_add(name, score, group, reason)

    def register_batch_groups(data: BatchData, group_indices: np.ndarray) -> None:
        """Reduce all completed groups with NumPy; inspect only repeated IDs."""
        nonlocal distinct_tweets, repeated_tweets, repeated_observations
        if len(group_indices) == 0:
            return
        lengths = data.lengths[group_indices]
        starts = data.starts[group_indices]
        # Exact creation-day/language coverage is reduced over every unique ID.
        day_codes = np.full(len(data.created_ms), -1, dtype=np.int64)
        valid_created = data.created_ms > np.iinfo(np.int64).min // 2
        day_codes[valid_created] = np.floor_divide(data.created_ms[valid_created], DAY_MS)
        coverage_update_vectorized(
            coverage,
            day_codes[data.starts],
            data.lang_codes[data.starts],
            data.lengths,
            group_indices,
            np.zeros(len(data.lengths), dtype=np.int64),
        )
        group_count = len(group_indices)
        distinct_tweets += group_count
        repeated_mask = lengths >= 2
        repeated_tweets += int(np.count_nonzero(repeated_mask))
        repeated_observations += int(np.sum(lengths[repeated_mask], dtype=np.int64))
        snapshot_values, snapshot_counts = np.unique(lengths, return_counts=True)
        for value, count in zip(snapshot_values.tolist(), snapshot_counts.tolist()):
            snapshots_distribution[int(value)] += int(count)
        post_codes = np.where(
            data.rt_mask[starts],
            POST_TYPE_INDEX["rt_prefixed"],
            np.where(
                data.reply_mask[starts],
                POST_TYPE_INDEX["reply"],
                np.where(data.quote_mask[starts], POST_TYPE_INDEX["quote"], POST_TYPE_INDEX["original"]),
            ),
        ).astype(np.int64, copy=False)
        for code, post_type in enumerate(POST_TYPES):
            mask = post_codes == code
            post_counts = post_type_counts[post_type]
            post_counts[0] += int(np.count_nonzero(mask))
            post_counts[1] += int(np.sum(lengths[mask], dtype=np.int64))
            post_counts[2] += int(np.count_nonzero(mask & repeated_mask))
        # Metric transition ordering and rank fields need only repeated IDs.
        for index in group_indices[repeated_mask].tolist():
            register_repeated_details(group_from_batch(data, int(index), lang_labels))

    for file_number, path in enumerate(files, 1):
        parquet_file = pq.ParquetFile(path)
        for batch in parquet_file.iter_batches(batch_size=args.batch_size, columns=list(COLUMNS), use_threads=True):
            if not batch.num_rows:
                continue
            total_rows += batch.num_rows
            data = prepare_batch(batch, global_lang_codes, lang_labels)
            valid_created = data.created_ms > np.iinfo(np.int64).min // 2
            if np.any(valid_created):
                batch_min_created = int(np.min(data.created_ms[valid_created]))
                batch_max_created = int(np.max(data.created_ms[valid_created]))
                first_creation_ms = batch_min_created if first_creation_ms is None else min(first_creation_ms, batch_min_created)
                last_creation_ms = batch_max_created if last_creation_ms is None else max(last_creation_ms, batch_max_created)
            batch_min_version = int(np.min(data.version_ms))
            batch_max_version = int(np.max(data.version_ms))
            first_version_ms = batch_min_version if first_version_ms is None else min(first_version_ms, batch_min_version)
            last_version_ms = batch_max_version if last_version_ms is None else max(last_version_ms, batch_max_version)
            if previous_numeric_id is not None and int(data.ids_numeric[0]) < previous_numeric_id:
                id_descents += 1
            if len(data.ids_numeric) > 1:
                id_descents += int(np.count_nonzero(data.ids_numeric[1:] < data.ids_numeric[:-1]))
            previous_numeric_id = int(data.ids_numeric[-1])
            # Group-level version descents are counted exactly when a group is
            # finalized (including a cross-batch merge); do not count raw
            # adjacent rows here because those include different IDs.
            # The first run can continue the carried ID.  Everything except the
            # final local run is safe to finalize in this batch.
            group_count = len(data.starts)
            if carry is not None:
                first_id = str(scalar_from_array(data.arrays[0], 0))
                if first_id == carry.id:
                    merged = merge_groups(carry, group_from_batch(data, 0, lang_labels, retain_rows=True))
                    if group_count == 1:
                        carry = merged
                        continue
                    register_boundary_group(merged)
                    start_index = 1
                else:
                    register_boundary_group(carry)
                    start_index = 0
                carry = None
            else:
                start_index = 0
            if group_count - start_index > 1:
                register_batch_groups(data, np.arange(start_index, group_count - 1, dtype=np.int64))
            carry = group_from_batch(data, group_count - 1, lang_labels, retain_rows=True)
            now = time.monotonic()
            if now - last_report >= 30:
                rate = total_rows / max(now - started, 1e-9)
                print(
                    f"progress file {file_number}/{len(files)} rows={total_rows:,} "
                    f"groups={distinct_tweets:,} repeated={repeated_tweets:,} rate={rate:,.0f} rows/s",
                    flush=True,
                )
    if carry is not None:
        register_boundary_group(carry)
        carry = None
    elapsed = time.monotonic() - started
    rows = ranks.finalize()
    sample_counts = extract_latest_sample_hashtags(args.sample)
    summary: dict[str, Any] = {
        "method": {
            "source_order": "Numeric tweet IDs are streamed in shard filename order; only one boundary ID group is carried across batches/files.",
            "first_last_axis": "Each ID trajectory is sorted by version ascending, then added_at ascending for ties; raw source version descents are counted separately.",
            "metrics": "Last minus first by metric after version sorting; no monotonicity assumption. Null endpoints produce null deltas/rates.",
            "per_hour_support": "Only trajectories with elapsed version duration >= 1 hour are ranked by per-hour velocity.",
            "rt_handling": "RT-prefixed rows are a separate post_type; retweet_count changes on RT-prefixed rows are retained as evidence but excluded from primary retweet-growth ranking.",
            "topic_handling": "Hashtags from selected outlier excerpts are lexical candidate topics and only compared with latest deterministic sample hashtags; no external event causes are asserted.",
            "time_zone": "UTC",
        },
        "input": {
            "dataset": str(args.dataset),
            "files": len(files),
            "metadata_observations": expected_metadata_rows,
            "expected_observations": args.expected_observations,
        },
        "scan": {
            "streamed_observations": total_rows,
            "reconciles_metadata_observations": total_rows == expected_metadata_rows,
            "reconciles_expected_observations": total_rows == args.expected_observations,
            "distinct_tweets": distinct_tweets,
            "singleton_tweets": snapshots_distribution.get(1, 0),
            "repeated_tweets": repeated_tweets,
            "repeated_observations": repeated_observations,
            "extra_observations_beyond_one_per_tweet": repeated_observations - repeated_tweets,
            "repeat_tweet_rate": repeated_tweets / distinct_tweets if distinct_tweets else None,
            "repeat_observation_rate": repeated_observations / total_rows if total_rows else None,
            "id_descents": id_descents,
            "within_id_version_descents": version_descents,
            "same_version_transitions": same_version_transitions,
            "first_creation_at": datetime_from_ms(first_creation_ms),
            "last_creation_at": datetime_from_ms(last_creation_ms),
            "first_version": datetime_from_ms(first_version_ms),
            "last_version": datetime_from_ms(last_version_ms),
            "elapsed_seconds": elapsed,
        },
        "snapshot_distribution_exact": {str(key): value for key, value in sorted(snapshots_distribution.items())},
        "duration_bins_exact_repeated_tweets": dict(sorted(duration_bins.items())),
        "post_type_counts": {
            name: {"tweets": values[0], "observations": values[1], "repeated_tweets": values[2]}
            for name, values in post_type_counts.items()
        },
        "metric_transition_totals_repeated_tweets": {
            "negative": dict(metric_negative_totals),
            "positive": dict(metric_positive_totals),
        },
        "cadence_totals": dict(cadence_totals),
        "language_labels": lang_labels,
        "ranking": {
            "top_k_per_rank": args.top_k,
            "output_rows_after_id_deduplication": len(rows),
            "rank_names": sorted(ranks.heaps),
            "sample_latest_hashtag_occurrences": sum(sample_counts.values()),
        },
    }
    return summary, rows, sample_counts, coverage, lang_labels


def main() -> None:
    args = parse_args()
    if args.threads > 4:
        raise ValueError("--threads must be <= 4 under the corpus resource contract")
    try:
        import pyarrow as _pa

        _pa.set_cpu_count(args.threads)
        _pa.set_io_thread_count(args.threads)
    except Exception:
        pass
    summary, rows, sample_counts, coverage, lang_labels = scan(args)
    write_outlier_outputs(args.output, rows)
    write_topic_candidates(args.output, rows, sample_counts)
    coverage_rows = []
    for (day_code, lang_code), values in sorted(coverage.items()):
        tweets, observations, repeated, repeated_obs, extra, long_tweets = values
        coverage_rows.append(
            {
                "day": day_label(day_code),
                "lang": lang_labels[lang_code] if 0 <= lang_code < len(lang_labels) else "<NULL>",
                "distinct_tweets": tweets,
                "observations": observations,
                "repeated_tweets": repeated,
                "repeated_observations": repeated_obs,
                "extra_observations": extra,
                "tweets_duration_ge_1h": long_tweets,
                "repeat_tweet_rate": repeated / tweets if tweets else None,
                "repeat_observation_rate": repeated_obs / observations if observations else None,
            }
        )
    write_csv(
        args.output / "trajectory-coverage-by-day-lang.csv",
        coverage_rows,
        [
            "day",
            "lang",
            "distinct_tweets",
            "observations",
            "repeated_tweets",
            "repeated_observations",
            "extra_observations",
            "tweets_duration_ge_1h",
            "repeat_tweet_rate",
            "repeat_observation_rate",
        ],
    )
    duration_rows = [{"duration_bin": key, "repeated_tweets": value} for key, value in sorted(summary["duration_bins_exact_repeated_tweets"].items())]
    write_csv(args.output / "trajectory-duration-bins.csv", duration_rows, ["duration_bin", "repeated_tweets"])
    def json_safe(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, dict):
            return {str(k): json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [json_safe(v) for v in value]
        return value
    (args.output / "trajectory-summary.json").write_text(json.dumps(json_safe(summary), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Finished: observations={summary['scan']['streamed_observations']:,} "
        f"distinct={summary['scan']['distinct_tweets']:,} repeated={summary['scan']['repeated_tweets']:,} "
        f"outlier_rows={len(rows):,}",
        flush=True,
    )


if __name__ == "__main__":
    main()
