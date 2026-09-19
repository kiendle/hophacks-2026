# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=1.26,<3",
#   "pyarrow>=20,<23",
#   "protobuf>=4.25,<6",
#   "sentencepiece>=0.2,<1",
#   "torch>=2.3",
#   "transformers>=4.39,<5",
# ]
# ///
"""Score tweet sentiment with CardiffNLP's multilingual Twitter model.

The scorer deliberately uses a real sequence-classification model rather than a
word-list heuristic.  It reads a parquet file containing ``id``, ``body`` and
``lang`` columns, normalises tweet text using the model card's recipe, deduplicates
identical non-empty body strings before inference, and writes one result per input
row.  IDs are converted to strings so that large Twitter snowflakes are never
silently rounded by a numeric parquet type.

Example:
    uv run topic-analysis/score_sentiment.py \
      --input twitter-analysis/sentiment-pilot-input.parquet \
      --output topic-analysis/sentiment-pilot.parquet

The default revision is pinned to the model commit recorded on 2026-09-19.  Use
``--revision main`` only when intentionally accepting a moving model revision.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from transformers import AutoConfig, AutoModelForSequenceClassification, AutoTokenizer

DEFAULT_MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
# Hugging Face API ``sha`` for the default model as observed on 2026-09-19.
DEFAULT_REVISION = "f2f1202b1bdeb07342385c3f807f9c07cd8f5cf8"
SUPPORTED_FINETUNE_LANGUAGES = {
    "ar": "Arabic",
    "en": "English",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "it": "Italian",
    "pt": "Portuguese",
    "es": "Spanish",
}
LABELS = ("negative", "neutral", "positive")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Input parquet with id, body, lang")
    parser.add_argument("--output", required=True, type=Path, help="Output parquet path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Hugging Face model (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--revision",
        default=DEFAULT_REVISION,
        help="Hugging Face model revision/commit (default is pinned for reproducibility)",
    )
    parser.add_argument("--cache-dir", type=Path, default=None, help="Optional Hugging Face cache directory")
    parser.add_argument("--batch-size", type=int, default=16, help="Texts per model batch (default: 16)")
    parser.add_argument(
        "--max-length",
        type=int,
        default=128,
        help="Tokenizer sequence bound including special tokens (default: 128)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "mps", "cuda"),
        default="auto",
        help="Inference device; auto prefers MPS, then CUDA, then CPU",
    )
    parser.add_argument(
        "--read-batch-size",
        type=int,
        default=65536,
        help="Parquet rows read at once (default: 65536)",
    )
    parser.add_argument(
        "--metadata-output",
        type=Path,
        default=None,
        help="Optional JSON provenance path (default: output suffix .json)",
    )
    return parser.parse_args()


def choose_device(requested: str) -> torch.device:
    """Return a genuinely available accelerator, never a fake fallback."""
    mps_available = bool(torch.backends.mps.is_available())
    cuda_available = bool(torch.cuda.is_available())
    if requested == "mps":
        if not mps_available:
            raise RuntimeError("--device mps requested, but torch.backends.mps.is_available() is false")
        return torch.device("mps")
    if requested == "cuda":
        if not cuda_available:
            raise RuntimeError("--device cuda requested, but torch.cuda.is_available() is false")
        return torch.device("cuda")
    if requested == "cpu":
        return torch.device("cpu")
    if mps_available:
        return torch.device("mps")
    if cuda_available:
        return torch.device("cuda")
    return torch.device("cpu")


def normalize_tweet(text: str) -> str:
    """Apply CardiffNLP's documented Twitter preprocessing.

    The model card replaces user handles with ``@user`` and URLs with ``http``.
    We additionally collapse line-break/tab whitespace so parquet values with
    embedded formatting do not create accidental token-boundary differences.
    """
    text = re.sub(r"\s+", " ", text).strip()
    tokens = []
    for token in text.split(" "):
        if token.startswith("@") and len(token) > 1:
            token = "@user"
        if token.startswith("http"):
            token = "http"
        tokens.append(token)
    return " ".join(tokens)


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_rows(path: Path, read_batch_size: int) -> tuple[list[str], list[str | None], list[str | None], dict[str, int]]:
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    missing = {column for column in ("id", "body", "lang") if column not in names}
    if missing:
        raise ValueError(f"input parquet is missing required columns: {sorted(missing)}")

    ids: list[str] = []
    bodies: list[str | None] = []
    langs: list[str | None] = []
    for batch in parquet.iter_batches(batch_size=read_batch_size, columns=["id", "body", "lang"]):
        id_values = batch.column("id").to_pylist()
        body_values = batch.column("body").to_pylist()
        lang_values = batch.column("lang").to_pylist()
        if any(value is None for value in id_values):
            raise ValueError("input parquet contains a null id; IDs must be present and preserved as strings")
        ids.extend(str(value) for value in id_values)
        bodies.extend(_as_string(value) for value in body_values)
        langs.extend(_as_string(value) for value in lang_values)

    language_counts: collections.Counter[str] = collections.Counter(lang or "<NULL>" for lang in langs)
    return ids, bodies, langs, dict(sorted(language_counts.items(), key=lambda item: (-item[1], item[0])))


def unique_texts(
    bodies: Sequence[str | None],
) -> tuple[list[str], list[int | None], int, int]:
    """Return unique model inputs and each row's unique-text index.

    Identical raw body strings share one model call even if their language tags or
    IDs differ.  Null/blank rows remain unscorable and receive null predictions.
    """
    body_to_index: dict[str, int] = {}
    texts: list[str] = []
    row_indices: list[int | None] = []
    missing_or_blank = 0
    for body in bodies:
        if body is None or not body.strip():
            row_indices.append(None)
            missing_or_blank += 1
            continue
        index = body_to_index.get(body)
        if index is None:
            index = len(texts)
            body_to_index[body] = index
            texts.append(normalize_tweet(body))
        row_indices.append(index)
    return texts, row_indices, missing_or_blank, len(body_to_index)


def model_label_indices(config: Any) -> dict[str, int]:
    raw = getattr(config, "id2label", None) or {}
    indices: dict[str, int] = {}
    for key, value in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            index = int(key) if isinstance(key, int) else -1
        label = str(value).strip().lower()
        if label in LABELS and index >= 0:
            indices[label] = index
    if set(indices) != set(LABELS):
        raise RuntimeError(f"model config must expose exactly negative/neutral/positive labels; got {raw!r}")
    return indices


def untruncated_lengths(tokenizer: Any, texts: Sequence[str]) -> list[int]:
    if not texts:
        return []
    encoded = tokenizer(
        list(texts),
        add_special_tokens=True,
        truncation=False,
        padding=False,
        return_length=True,
    )
    lengths = encoded.get("length")
    if lengths is not None:
        return [int(length) for length in lengths]
    return [len(ids) for ids in encoded["input_ids"]]


def infer(
    texts: Sequence[str],
    tokenizer: Any,
    model: Any,
    device: torch.device,
    label_indices: dict[str, int],
    batch_size: int,
    max_length: int,
) -> tuple[np.ndarray, list[int], float]:
    if not texts:
        return np.empty((0, 3), dtype=np.float32), [], 0.0
    lengths = untruncated_lengths(tokenizer, texts)
    truncation_count = sum(length > max_length for length in lengths)
    started = time.monotonic()
    all_scores: list[np.ndarray] = []
    model.eval()
    for start in range(0, len(texts), batch_size):
        batch_texts = list(texts[start : start + batch_size])
        encoded = tokenizer(
            batch_texts,
            add_special_tokens=True,
            max_length=max_length,
            truncation=True,
            padding=True,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        with torch.inference_mode():
            logits = model(**encoded).logits
            probabilities = torch.softmax(logits.float(), dim=-1)
        scores = probabilities.detach().to("cpu").numpy()
        # Reorder model-config columns to the stable output order.
        all_scores.append(
            np.column_stack([scores[:, label_indices[label]] for label in LABELS]).astype(np.float32, copy=False)
        )
    return np.concatenate(all_scores, axis=0), [int(value) for value in lengths], time.monotonic() - started


def quantiles(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {"p50": None, "p90": None, "p99": None}
    return {key: float(np.quantile(values, quantile)) for key, quantile in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99))}


def write_output(
    path: Path,
    ids: Sequence[str],
    row_indices: Sequence[int | None],
    unique_scores: np.ndarray,
) -> dict[str, Any]:
    negative: list[float | None] = []
    neutral: list[float | None] = []
    positive: list[float | None] = []
    score: list[float | None] = []
    labels: list[str | None] = []
    output_label_counts: collections.Counter[str] = collections.Counter()
    for index in row_indices:
        if index is None:
            negative.append(None)
            neutral.append(None)
            positive.append(None)
            score.append(None)
            labels.append(None)
            continue
        probs = unique_scores[index]
        negative_value, neutral_value, positive_value = (float(value) for value in probs)
        predicted = LABELS[int(np.argmax(probs))]
        negative.append(negative_value)
        neutral.append(neutral_value)
        positive.append(positive_value)
        score.append(positive_value - negative_value)
        labels.append(predicted)
        output_label_counts[predicted] += 1

    table = pa.table(
        {
            "id": pa.array(list(ids), type=pa.string()),
            "negative": pa.array(negative, type=pa.float32()),
            "neutral": pa.array(neutral, type=pa.float32()),
            "positive": pa.array(positive, type=pa.float32()),
            "sentiment_score": pa.array(score, type=pa.float32()),
            "predicted_label": pa.array(labels, type=pa.string()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return {"counts": dict(sorted(output_label_counts.items())), "rows_written": len(ids)}


def provenance(
    *,
    args: argparse.Namespace,
    input_path: Path,
    output_path: Path,
    device: torch.device,
    ids: Sequence[str],
    language_counts: dict[str, int],
    texts: Sequence[str],
    missing_or_blank: int,
    unique_body_count: int,
    unique_scores: np.ndarray,
    lengths: Sequence[int],
    inference_seconds: float,
    elapsed_seconds: float,
    output_stats: dict[str, Any],
    tokenizer: Any,
    config: Any,
) -> dict[str, Any]:
    truncation_count = sum(length > args.max_length for length in lengths)
    all_confidences = unique_scores.max(axis=1) if unique_scores.size else np.empty(0, dtype=np.float32)
    supported_rows = sum(language_counts.get(code, 0) for code in SUPPORTED_FINETUNE_LANGUAGES)
    unsupported_counts = {
        language: count
        for language, count in language_counts.items()
        if language not in SUPPORTED_FINETUNE_LANGUAGES
    }
    model_max_length = getattr(tokenizer, "model_max_length", None)
    if isinstance(model_max_length, int) and model_max_length > 10**6:
        model_max_length = None
    return {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input": str(input_path),
        "output": str(output_path),
        "rows": {
            "input": len(ids),
            "nonempty_body": len(ids) - missing_or_blank,
            "missing_or_blank_body": missing_or_blank,
            "unique_nonempty_body_strings": unique_body_count,
            "deduplicated_inference_rows": len(ids) - missing_or_blank - unique_body_count,
            "deduplication_fraction_of_nonempty": (
                float((len(ids) - missing_or_blank - unique_body_count) / (len(ids) - missing_or_blank))
                if len(ids) != missing_or_blank
                else None
            ),
            "language_counts": language_counts,
            "supported_finetune_language_rows": supported_rows,
            "unsupported_or_unlabelled_language_rows": sum(unsupported_counts.values()),
            "unsupported_or_unlabelled_language_counts": unsupported_counts,
        },
        "model": {
            "id": args.model,
            "revision": args.revision,
            "card_url": f"https://huggingface.co/{args.model}",
            "paper_url": "https://arxiv.org/abs/2104.12250",
            "repository_url": "https://github.com/cardiffnlp/xlm-t",
            "description": "XLM-R base continued pretraining on Twitter and fine-tuned for 3-way sentiment.",
            "pretraining_corpus": "approximately 198M tweets; XLM-T language-model pretraining covers 30+ languages",
            "fine_tuning_languages": SUPPORTED_FINETUNE_LANGUAGES,
            "label_mapping": {str(index): label for label, index in model_label_indices(config).items()},
            "limitations": [
                "The sentiment head was fine-tuned on UMSAB benchmark data in eight languages, not this corpus.",
                "The card says it can be used for more languages, but unsupported-language predictions are not covered by the eight-language benchmark claim.",
                "Scores are text-level polarity and must not be interpreted as sentiment toward a discovered topic without target-aware validation.",
                "Twitter slang, code-switching, sarcasm, quoted text, and retweet templates can produce uncertain or mis-targeted predictions.",
                "Softmax probabilities are model confidence scores, not calibrated probabilities; compare strata with uncertainty intervals.",
            ],
        },
        "preprocessing": {
            "normalization": "Collapse whitespace; replace tokens beginning with @ (length > 1) by @user and tokens beginning with http by http, following the model card.",
            "max_length": args.max_length,
            "tokenizer_model_max_length": model_max_length,
            "model_config_max_position_embeddings": getattr(config, "max_position_embeddings", None),
            "truncation": {
                "unique_texts_checked": len(lengths),
                "unique_texts_truncated": truncation_count,
                "fraction_of_unique_texts_truncated": float(truncation_count / len(lengths)) if lengths else 0.0,
            },
            "deduplicate_key": "exact raw body string before normalization; one prediction mapped back to every matching ID",
        },
        "runtime": {
            "device_requested": args.device,
            "device_used": str(device),
            "torch_version": torch.__version__,
            "transformers_version": _package_version("transformers"),
            "python_version": platform.python_version(),
            "mps_built": bool(torch.backends.mps.is_built()),
            "mps_available": bool(torch.backends.mps.is_available()),
            "cuda_available": bool(torch.cuda.is_available()),
            "batch_size": args.batch_size,
            "read_batch_size": args.read_batch_size,
            "inference_seconds": inference_seconds,
            "elapsed_seconds": elapsed_seconds,
            "unique_texts_per_inference_second": float(len(texts) / inference_seconds) if inference_seconds else None,
            "input_rows_per_elapsed_second": float(len(ids) / elapsed_seconds) if elapsed_seconds else None,
        },
        "confidence": {
            "unique_text_max_probability": quantiles(all_confidences),
            "unique_text_mean_max_probability": float(all_confidences.mean()) if all_confidences.size else None,
        },
        "output": output_stats,
        "analysis_guidance": {
            "topic_targeting": "Do not call this output topic-targeted sentiment; aggregate only after topic assignment and inspect target relevance.",
            "recommended_shift_estimator": "For each topic/day, report mean sentiment_score and bootstrap or Wilson intervals on author-clustered samples; compare against daily language/RT composition baselines.",
            "retweets_and_templates": "Keep RT-prefixed rows separate or downweight them; collapse identical text for inference but retain ID-level prevalence and author counts so templates cannot masquerade as independent language evidence.",
            "author_concentration": "Use author-cluster bootstrap or effective sample size; report both tweet-weighted and author-weighted estimates.",
            "unsupported_languages": "Do not silently pool unsupported language codes with supported ones; report them separately or exclude with counts and sensitivity bounds.",
        },
    }


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def main() -> int:
    args = parse_args()
    if args.batch_size < 1 or args.read_batch_size < 1:
        raise SystemExit("--batch-size and --read-batch-size must be positive")
    if args.max_length < 4:
        raise SystemExit("--max-length must leave room for special tokens (use >= 4)")
    if not args.input.exists():
        raise SystemExit(f"input does not exist: {args.input}")

    started = time.monotonic()
    device = choose_device(args.device)
    print(f"Loading {args.model}@{args.revision} on {device}...", flush=True)
    load_kwargs: dict[str, Any] = {"revision": args.revision}
    if args.cache_dir is not None:
        load_kwargs["cache_dir"] = str(args.cache_dir)
    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=False, **load_kwargs)
    config = AutoConfig.from_pretrained(args.model, **load_kwargs)
    model = AutoModelForSequenceClassification.from_pretrained(args.model, config=config, **load_kwargs)
    label_indices = model_label_indices(config)
    model_max_length = getattr(tokenizer, "model_max_length", None)
    if isinstance(model_max_length, int) and model_max_length < 10**6 and args.max_length > model_max_length:
        raise SystemExit(f"--max-length {args.max_length} exceeds tokenizer model_max_length {model_max_length}")
    model.to(device)

    print(f"Reading {args.input}...", flush=True)
    ids, bodies, langs, language_counts = read_rows(args.input, args.read_batch_size)
    texts, row_indices, missing_or_blank, unique_body_count = unique_texts(bodies)
    print(
        f"Scoring {len(texts):,} unique non-empty bodies mapped to {len(ids):,} rows "
        f"({missing_or_blank:,} blank/null rows)...",
        flush=True,
    )
    unique_scores, lengths, inference_seconds = infer(
        texts,
        tokenizer,
        model,
        device,
        label_indices,
        args.batch_size,
        args.max_length,
    )
    metadata_path = args.metadata_output or args.output.with_suffix(".json")
    output_stats = write_output(args.output, ids, row_indices, unique_scores)
    metadata = provenance(
        args=args,
        input_path=args.input,
        output_path=args.output,
        device=device,
        ids=ids,
        language_counts=language_counts,
        texts=texts,
        missing_or_blank=missing_or_blank,
        unique_body_count=unique_body_count,
        unique_scores=unique_scores,
        lengths=lengths,
        inference_seconds=inference_seconds,
        elapsed_seconds=time.monotonic() - started,
        output_stats=output_stats,
        tokenizer=tokenizer,
        config=config,
    )
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} and {metadata_path}", flush=True)
    print(json.dumps({"rows": len(ids), "unique_texts": len(texts), "seconds": metadata["runtime"]["elapsed_seconds"], "labels": output_stats["counts"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
