# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5", "jsonschema>=4.23,<5"]
# ///
"""Validate and compile neutral semantic automation configurations.

This command only validates configuration and builds request previews.  It never
opens a checkpoint, reads credentials, contacts a service, or runs a model.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import duckdb
import jsonschema

import filter_keywords
from jev_protocol import (
    classification_request,
    configuration_key,
    sentiment_request,
)


SCHEMA_FILENAME = "automation-config.schema.json"
_VALIDATION_TEXT = "configuration validation"
_NATIVE_FILENAMES = (
    "filter-config.json",
    "company-categories.json",
    "jev-policy.json",
)


class ConfigurationError(ValueError):
    """A user-facing configuration or materialization error."""


def _reject_constant(value: str) -> None:
    raise ConfigurationError(f"JSON must not contain non-finite constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_json_bytes(raw: bytes, source: Path) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{source}: must be UTF-8 JSON: {error}") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ConfigurationError:
        raise
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"{source}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}") from error
    return _parse_json_bytes(raw, path)


def _field_path(path: Sequence[Any]) -> str:
    rendered = ""
    for part in path:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += "." + str(part)
    return rendered[1:] if rendered.startswith(".") else rendered or "<root>"


def _schema_errors(instance: Any, schema: Any) -> list[str]:
    try:
        validator_type = jsonschema.validators.validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(schema, format_checker=jsonschema.FormatChecker())
    except jsonschema.SchemaError as error:
        raise ConfigurationError(
            f"{SCHEMA_FILENAME}: invalid JSON Schema: {error.message}"
        ) from error
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        f"schema validation failed at {_field_path(error.absolute_path)}: {error.message}"
        for error in errors
    ]


def _validate_config_schema(config: Any) -> dict[str, Any]:
    schema_path = Path(__file__).with_name(SCHEMA_FILENAME)
    schema = _read_json(schema_path)
    errors = _schema_errors(config, schema)
    if errors:
        raise ConfigurationError("; ".join(errors))
    return config


def _reject_nonfinite_values(value: Any, path: str = "<root>") -> None:
    """Reject non-finite values for callers passing an in-memory mapping."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationError(f"{path} must not contain a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_nonfinite_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonfinite_values(child, f"{path}[{index}]")


def _validate_semantic_references(config: Mapping[str, Any]) -> None:
    groups = config["keyword_filter"]["groups"]
    targets = config["targets"]
    group_ids = {group["id"] for group in groups}
    if len(group_ids) != len(groups):
        raise ConfigurationError("keyword_filter.groups contains duplicate IDs")
    if len({target["id"] for target in targets}) != len(targets):
        raise ConfigurationError("targets contains duplicate IDs")
    for index, target in enumerate(targets):
        for group_index, group_id in enumerate(target["keyword_groups"]):
            if group_id not in group_ids:
                raise ConfigurationError(
                    f"targets[{index}].keyword_groups[{group_index}] "
                    f"references unknown retrieval group {group_id!r}"
                )


def _compile_filter(keyword_filter: Mapping[str, Any]) -> dict[str, Any]:
    companies: list[dict[str, Any]] = []
    for group in keyword_filter["groups"]:
        native_group = {
            "id": group["id"],
            "label": group["label"],
            "direct": list(group["direct"]),
            "ambiguous": list(group["ambiguous"]),
            "contextual": list(group["contextual"]),
        }
        for optional in ("direct_patterns", "attribution_note"):
            if optional in group:
                value = group[optional]
                native_group[optional] = (
                    list(value) if optional == "direct_patterns" else value
                )
        companies.append(native_group)
    # The historical matcher consumes only these native fields.  The public
    # matching marker is a contract constraint, not an editable algorithm.
    return {
        "version": keyword_filter["version"],
        "purpose": keyword_filter["purpose"],
        "companies": companies,
        "ai_context_terms": list(keyword_filter["context_terms"]),
        "discovery_terms": list(keyword_filter["discovery_terms"]),
    }


def _compile_taxonomy(
    config: Mapping[str, Any],
) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    for target in config["targets"]:
        category = {
            "id": target["id"],
            "label": target["label"],
            "keyword_groups": list(target["keyword_groups"]),
        }
        reference = target.get("reference")
        if isinstance(reference, Mapping):
            for source, native in (
                ("definition", "definition"),
                ("related_terms", "products"),
                ("disambiguation", "disambiguation"),
                ("sources", "sources"),
            ):
                if source in reference:
                    value = reference[source]
                    category[native] = (
                        list(value) if source in {"related_terms", "sources"} else value
                    )
        categories.append(category)
    return {
        "version": "target-taxonomy-v1",
        "classification": {
            "question_type": "noul",
            "rules": list(config["categorization"]["rules"]),
        },
        "categories": categories,
    }


def _compile_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    categorization = config["categorization"]
    semantics = config["semantics"]
    return {
        "version": "jev-policy-v1",
        "model": config["model"],
        "company_ids": [target["id"] for target in config["targets"]],
        "accept_probability": categorization["accept_probability"],
        "threshold_status": categorization["threshold_status"],
        "categorization": {
            "prompt_version": "company-relevance-v2",
            "question_type": "noul",
            "probability_semantics": (
                "independent probability that the text is substantively relevant to "
                "the named target; probabilities do not form a shared distribution"
            ),
        },
        "sentiment": {
            "prompt_version": semantics["profile"],
            "question_type": semantics["question_type"],
            "criteria": dict(semantics["criteria"]),
            "instruction_template": semantics["instruction_template"].replace(
                "{target_label}", "{company_label}"
            ),
        },
    }


def _compile_keyword_patterns(keyword_filter: Mapping[str, Any]) -> None:
    """Compile every native retrieval pattern with DuckDB's RE2 engine."""
    try:
        with duckdb.connect(":memory:") as connection:
            for company_index, company in enumerate(keyword_filter["companies"]):
                for pattern_index, pattern in enumerate(
                    company.get("direct_patterns", [])
                ):
                    try:
                        connection.execute(
                            "SELECT regexp_matches('', ?)", [pattern]
                        ).fetchone()
                    except Exception as error:
                        raise ConfigurationError(
                            "DuckDB RE2 rejected "
                            f"filter-config.json.companies[{company_index}].direct_patterns[{pattern_index}]: "
                            f"{error}"
                        ) from error
            # Execute the complete SQL emitted by the native compiler as well,
            # so term escaping and all assembled unions are compiled by RE2.
            sql = filter_keywords.screen_sql(
                "(SELECT CAST('' AS VARCHAR) AS body)", keyword_filter
            )
            connection.execute(sql).fetchall()
    except ConfigurationError:
        raise
    except Exception as error:
        raise ConfigurationError(
            f"DuckDB RE2 rejected keyword_filter rules: {error}"
        ) from error


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"value is not canonical JSON: {error}") from error


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def compile_configuration(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate a v2 configuration and compile its three native artifacts.

    The returned mapping is keyed by the filenames consumed by the existing
    filter and Jev runner.  No historical native artifact is read or consulted.
    """
    if not isinstance(config, Mapping):
        raise ConfigurationError("configuration must be an object")
    _reject_nonfinite_values(config)
    validated = _validate_config_schema(config)
    _validate_semantic_references(validated)

    native_filter = _compile_filter(validated["keyword_filter"])
    native_taxonomy = _compile_taxonomy(validated)
    native_policy = _compile_policy(validated)
    _compile_keyword_patterns(native_filter)

    try:
        target_ids = [target["id"] for target in validated["targets"]]
        classification_request(
            _VALIDATION_TEXT,
            native_taxonomy,
            native_policy,
        )
        sentiment_request(
            _VALIDATION_TEXT,
            target_ids,
            native_taxonomy,
            native_policy,
        )
        configuration_key(native_taxonomy, native_policy)
    except Exception as error:
        raise ConfigurationError(
            f"Jev taxonomy/policy validation failed: {error}"
        ) from error

    return {
        "filter-config.json": native_filter,
        "company-categories.json": native_taxonomy,
        "jev-policy.json": native_policy,
    }


def _materialize(
    compiled: Mapping[str, Mapping[str, Any]], output_dir: Path
) -> dict[str, str]:
    target = Path(output_dir)
    if os.path.lexists(target):
        raise ConfigurationError(
            f"output directory already exists; refusing overwrite: {target}"
        )
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot create output parent {parent}: {error}"
        ) from error

    try:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(parent))
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot create staging directory beside {target}: {error}"
        ) from error

    created_target = False
    moved: list[Path] = []
    try:
        for filename in _NATIVE_FILENAMES:
            destination = staging / filename
            try:
                with destination.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        compiled[filename],
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    stream.write("\n")
            except (OSError, TypeError, ValueError) as error:
                raise ConfigurationError(
                    f"cannot write staged {filename}: {error}"
                ) from error

        try:
            # mkdir is exclusive, so a target created after the initial check
            # still cannot be overwritten by this command.
            target.mkdir()
            created_target = True
            for filename in _NATIVE_FILENAMES:
                source = staging / filename
                destination = target / filename
                os.replace(source, destination)
                moved.append(destination)
        except OSError as error:
            raise ConfigurationError(
                f"cannot commit materialized configuration {target}: {error}"
            ) from error
    except Exception:
        for path in moved:
            try:
                path.unlink()
            except OSError:
                pass
        if created_target:
            try:
                target.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {filename: str(target / filename) for filename in _NATIVE_FILENAMES}


def _build_result(
    config: Mapping[str, Any],
    *,
    output_dir: Path | None = None,
    preview_text: str | None = None,
    sentiment_targets: Iterable[str] | None = None,
) -> dict[str, Any]:
    compiled = compile_configuration(config)
    native_filter = compiled["filter-config.json"]
    native_taxonomy = compiled["company-categories.json"]
    native_policy = compiled["jev-policy.json"]
    try:
        configuration = configuration_key(native_taxonomy, native_policy)
    except Exception as error:
        raise ConfigurationError(
            f"Jev taxonomy/policy validation failed: {error}"
        ) from error
    result: dict[str, Any] = {
        "ok": True,
        "configuration_key": configuration,
        "filter_sha256": _canonical_sha256(native_filter),
        "filter_sha256_kind": "canonical-json-utf8",
    }

    has_preview_text = preview_text is not None
    has_sentiment_targets = sentiment_targets is not None
    if has_preview_text != has_sentiment_targets:
        raise ConfigurationError(
            "--preview-text and --sentiment-targets must be supplied together"
        )
    if has_preview_text:
        selected = list(sentiment_targets or [])
        if not selected:
            raise ConfigurationError("--sentiment-targets must contain at least one ID")
        try:
            # Use the exact native builders and the exact compiled objects for
            # both previews and materialization.
            categorization = classification_request(
                preview_text, native_taxonomy, native_policy
            )
            sentiment = sentiment_request(
                preview_text,
                selected,
                native_taxonomy,
                native_policy,
            )
        except Exception as error:
            raise ConfigurationError(
                f"preview request validation failed: {error}"
            ) from error
        result["preview"] = {
            "categorization": categorization,
            "sentiment": sentiment,
        }

    if output_dir is not None:
        result["materialized_paths"] = _materialize(compiled, output_dir)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and optionally materialize the offline semantic/Jev automation configuration."
    )
    parser.add_argument(
        "config", type=Path, help="semantic automation configuration JSON"
    )
    parser.add_argument(
        "--output-dir", type=Path, help="new directory for native configuration files"
    )
    parser.add_argument(
        "--preview-text",
        help="exact hypothetical post text for offline request previews",
    )
    parser.add_argument(
        "--sentiment-targets",
        nargs="+",
        metavar="ID",
        help="target IDs for the hypothetical sentiment request",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = _read_json(args.config)
        result = _build_result(
            config,
            output_dir=args.output_dir,
            preview_text=args.preview_text,
            sentiment_targets=args.sentiment_targets,
        )
    except (ConfigurationError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
