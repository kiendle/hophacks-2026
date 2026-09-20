"""Pure request, response-validation, and classification helpers for Jev.

This module deliberately has no HTTP, credential, filesystem, or corpus access
other than loading the explicitly requested policy JSON.  The engine owns
transport, retries, persistence, and budgeting.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from numbers import Real
from pathlib import Path
from typing import Any

from build_jev_request import MODEL as BUILDER_MODEL
from build_jev_request import PROMPT_VERSION as BUILDER_PROMPT_VERSION
from build_jev_request import build_request


# The builder is intentionally the source of truth for the existing v2
# categorization payload.  This module only validates policy around it.


DEFAULT_POLICY_PATH = Path(__file__).with_name("jev-policy.json")
SENTIMENT_LABELS = (
    "positive",
    "negative",
    "neutral",
    "mixed",
    "insufficient_evidence",
)
# Observed provider probabilities are rounded to hundredths. Each label can
# contribute up to half a percentage point of rounding error to the sum.
_CHOICE_ROUNDING_ERROR = 0.005


def _is_number(value: Any) -> bool:
    """Return true for finite JSON-like real numbers, excluding bool."""
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _accept_probability(policy: Mapping[str, Any]) -> float:
    if "accept_probability" not in policy:
        raise ValueError("policy must define accept_probability")
    accept = policy["accept_probability"]
    if not _is_number(accept):
        raise ValueError("accept_probability must be a finite number")
    accept = float(accept)
    if not 0.0 <= accept <= 1.0:
        raise ValueError("accept_probability must be in [0, 1]")
    return accept


def _policy_model(policy: Mapping[str, Any]) -> str:
    model = _nonempty_text(policy.get("model"), "policy.model")
    if model != BUILDER_MODEL:
        raise ValueError(f"policy.model must be pinned to {BUILDER_MODEL}")
    return model


def _categorization_settings(policy: Mapping[str, Any]) -> tuple[str, str]:
    categorization = _mapping(policy.get("categorization"), "policy.categorization")
    expected_keys = {"prompt_version", "question_type", "probability_semantics"}
    if set(categorization) != expected_keys:
        raise ValueError("policy.categorization has an unsupported schema")
    prompt_version = _nonempty_text(
        categorization["prompt_version"], "categorization prompt_version"
    )
    question_type = categorization["question_type"]
    if question_type != "noul":
        raise ValueError("categorization question_type must be 'noul'")
    _nonempty_text(
        categorization["probability_semantics"], "categorization probability_semantics"
    )
    if prompt_version != BUILDER_PROMPT_VERSION:
        raise ValueError(
            f"categorization prompt_version must be {BUILDER_PROMPT_VERSION}"
        )
    return prompt_version, question_type


def _sentiment_settings(
    policy: Mapping[str, Any],
) -> tuple[str, str, dict[str, str], str]:
    sentiment = _mapping(policy.get("sentiment"), "policy.sentiment")
    expected_keys = {
        "prompt_version",
        "question_type",
        "criteria",
        "instruction_template",
    }
    if set(sentiment) != expected_keys:
        raise ValueError("policy.sentiment has an unsupported schema")
    prompt_version = _nonempty_text(
        sentiment["prompt_version"], "sentiment prompt_version"
    )
    question_type = sentiment["question_type"]
    if question_type != "choice":
        raise ValueError("sentiment question_type must be 'choice'")
    criteria = _mapping(sentiment["criteria"], "sentiment criteria")
    if set(criteria) != set(SENTIMENT_LABELS):
        raise ValueError(
            "sentiment criteria keys must be positive, negative, neutral, mixed, and insufficient_evidence"
        )
    normalized_criteria: dict[str, str] = {}
    for label in SENTIMENT_LABELS:
        normalized_criteria[label] = _nonempty_text(
            criteria[label], f"sentiment criterion {label}"
        )
    template = _nonempty_text(
        sentiment["instruction_template"], "sentiment instruction_template"
    )
    if "{company_label}" not in template:
        raise ValueError("sentiment instruction_template must name {company_label}")
    return prompt_version, question_type, normalized_criteria, template


def _policy_company_ids(policy: Mapping[str, Any]) -> set[str]:
    value = policy.get("company_ids")
    if not isinstance(value, list) or not value:
        raise ValueError("policy.company_ids must be a nonempty list of IDs")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(
            "policy.company_ids must be a nonempty list of nonempty strings"
        )
    if len(value) != len(set(value)):
        raise ValueError("policy.company_ids must be unique")
    if "others" in value:
        raise ValueError("'others' is not a company ID")
    return set(value)


def _validate_policy(policy: Any) -> Mapping[str, Any]:
    policy = _mapping(policy, "policy")
    expected_keys = {
        "version",
        "model",
        "company_ids",
        "accept_probability",
        "threshold_status",
        "categorization",
        "sentiment",
    }
    if set(policy) != expected_keys:
        raise ValueError("policy has an unsupported schema")
    _nonempty_text(policy["version"], "policy.version")
    if policy["threshold_status"] != "UNCALIBRATED":
        raise ValueError("policy.threshold_status must be 'UNCALIBRATED'")
    _policy_model(policy)
    _accept_probability(policy)
    _categorization_settings(policy)
    _sentiment_settings(policy)
    _policy_company_ids(policy)
    return policy


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate an inspectable, versioned policy JSON document."""
    policy_path = DEFAULT_POLICY_PATH if path is None else Path(path)
    try:
        with policy_path.open("r", encoding="utf-8") as stream:
            policy = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load policy {policy_path}: {error}") from error
    _validate_policy(policy)
    if not isinstance(
        policy, dict
    ):  # _mapping accepts custom mappings; JSON is a dict.
        raise ValueError("policy root must be a JSON object")
    version = policy.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("policy.version must be a nonempty string")
    return policy


def _taxonomy_categories(taxonomy: Any) -> list[tuple[str, str]]:
    taxonomy = _mapping(taxonomy, "taxonomy")
    categories = taxonomy.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("taxonomy.categories must be a nonempty list")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, category in enumerate(categories):
        category = _mapping(category, f"taxonomy.categories[{index}]")
        company_id = _nonempty_text(
            category.get("id"), f"taxonomy.categories[{index}].id"
        )
        label = _nonempty_text(
            category.get("label"), f"taxonomy.categories[{index}].label"
        )
        if company_id == "others":
            raise ValueError("taxonomy cannot expose 'others' as a company ID")
        if company_id in seen:
            raise ValueError(f"duplicate company ID {company_id!r} in taxonomy")
        seen.add(company_id)
        result.append((company_id, label))
    return result


def _taxonomy_for_policy(
    taxonomy: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[tuple[str, str]]:
    categories = _taxonomy_categories(taxonomy)
    taxonomy_ids = {company_id for company_id, _ in categories}
    policy_ids = _policy_company_ids(policy)
    if taxonomy_ids != policy_ids:
        missing = sorted(policy_ids - taxonomy_ids)
        extra = sorted(taxonomy_ids - policy_ids)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unknown: {', '.join(extra)}")
        raise ValueError(
            f"taxonomy company IDs do not match policy ({'; '.join(details)})"
        )
    return categories


def classification_request(
    text: str, taxonomy: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the unchanged company-relevance-v2 request from the existing builder."""
    policy = _validate_policy(policy)
    _taxonomy_for_policy(taxonomy, policy)
    request = build_request(text, taxonomy)
    if request.get("model") != _policy_model(policy):
        raise ValueError("request builder model and policy model differ")
    return request


def sentiment_request(
    text: str,
    company_ids: Iterable[str],
    taxonomy: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one company-targeted Choice request for all selected companies."""
    text = _nonempty_text(text, "Post text")
    policy = _validate_policy(policy)
    categories = dict(_taxonomy_for_policy(taxonomy, policy))
    if isinstance(company_ids, (str, bytes)) or not isinstance(company_ids, Iterable):
        raise ValueError("company_ids must be a nonempty iterable of IDs")
    selected = list(company_ids)
    if not selected:
        raise ValueError("company_ids must not be empty")
    if any(
        not isinstance(company_id, str) or not company_id.strip()
        for company_id in selected
    ):
        raise ValueError("company_ids must contain nonempty strings")
    if len(selected) != len(set(selected)):
        raise ValueError("company_ids must not contain duplicates")
    unknown = [company_id for company_id in selected if company_id not in categories]
    if unknown:
        raise ValueError(f"unknown company ID(s): {', '.join(unknown)}")
    _, _, criteria, template = _sentiment_settings(policy)
    questions = {
        company_id: {
            "type": "choice",
            "instructions": template.format(company_label=categories[company_id]),
            "criteria": dict(criteria),
        }
        for company_id in selected
    }
    return {
        "model": _policy_model(policy),
        "state": {"post": text},
        "questions": questions,
    }


def request_key(payload: Mapping[str, Any]) -> str:
    """Return a canonical SHA-256 key for an API payload."""
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"payload is not canonicalizable JSON: {error}") from error
    return hashlib.sha256(encoded).hexdigest()


def configuration_key(taxonomy: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    """Hash only semantic inputs that affect categorization or sentiment."""
    policy = _validate_policy(policy)
    categories = _taxonomy_for_policy(taxonomy, policy)
    taxonomy_mapping = _mapping(taxonomy, "taxonomy")
    classification = taxonomy_mapping.get("classification", {})
    classification = _mapping(classification, "taxonomy.classification")
    prompt_version, question_type = _categorization_settings(policy)
    sentiment_prompt, sentiment_type, criteria, template = _sentiment_settings(policy)
    accept = _accept_probability(policy)
    semantic_configuration = {
        "model": _policy_model(policy),
        "taxonomy_version": taxonomy_mapping.get("version"),
        "company_categories": [
            {"id": company_id, "label": label} for company_id, label in categories
        ],
        "categorization": {
            "prompt_version": prompt_version,
            "question_type": question_type,
            "rules": classification.get("rules"),
        },
        "thresholds": {
            "accept_probability": accept,
        },
        "sentiment": {
            "prompt_version": sentiment_prompt,
            "question_type": sentiment_type,
            "criteria": criteria,
            "instruction_template": template,
        },
    }
    return request_key(semantic_configuration)


def _validate_usage(response: Mapping[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    usage = _mapping(usage, "response.usage")
    result: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"response.usage.{name} must be a nonnegative integer")
        result[name] = value
    return result


def _validate_probability(value: Any, name: str) -> float:
    if not _is_number(value):
        raise ValueError(f"{name} must be a finite number")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return numeric


def _validate_noul_answer(answer: Any, name: str) -> float:
    answer = _mapping(answer, name)
    if answer.get("type") != "noul":
        raise ValueError(f"{name}.type must be 'noul'")
    if "noul" not in answer:
        raise ValueError(f"{name}.noul is missing")
    return _validate_probability(answer["noul"], f"{name}.noul")


def _validate_choice_answer(
    answer: Any, question: Mapping[str, Any], name: str
) -> None:
    answer = _mapping(answer, name)
    if set(answer) != {"type", "choice", "confidence", "probabilities"}:
        raise ValueError(
            f"{name} must contain exactly type, choice, confidence, and probabilities"
        )
    if answer.get("type") != "choice":
        raise ValueError(f"{name}.type must be 'choice'")
    criteria = question.get("criteria")
    criteria = _mapping(criteria, f"{name} question.criteria")
    if not criteria:
        raise ValueError(f"{name} question.criteria must not be empty")
    choice = answer["choice"]
    if not isinstance(choice, str) or choice not in criteria:
        raise ValueError(f"{name}.choice must be one of the criteria")
    _validate_probability(answer["confidence"], f"{name}.confidence")
    probabilities = answer["probabilities"]
    if not isinstance(probabilities, Mapping):
        raise ValueError(f"{name}.probabilities must be an object")
    if set(probabilities) != set(criteria):
        raise ValueError(f"{name}.probabilities keys must exactly match criteria")
    total = 0.0
    for label, value in probabilities.items():
        total += _validate_probability(value, f"{name}.probabilities[{label!r}]")
    tolerance = len(probabilities) * _CHOICE_ROUNDING_ERROR + 1e-12
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"{name}.probabilities must sum approximately to one")


def validate_response(
    request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, int]:
    """Validate a complete Jev response and return its metering usage.

    The response object is intentionally not normalized or rewritten: callers
    can persist it verbatim to retain provider confidence/distribution fields.
    """
    request = _mapping(request, "request")
    response = _mapping(response, "response")
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("request.model must be a nonempty string")
    if response.get("model") != model:
        raise ValueError("response.model does not match request.model")
    questions = _mapping(request.get("questions"), "request.questions")
    answers = _mapping(response.get("answers"), "response.answers")
    if not questions:
        raise ValueError("request.questions must not be empty")
    if any(not isinstance(key, str) or not key for key in questions):
        raise ValueError("request question IDs must be nonempty strings")
    if any(not isinstance(key, str) or not key for key in answers):
        raise ValueError("response answer IDs must be nonempty strings")
    question_ids = set(questions)
    answer_ids = set(answers)
    missing = question_ids - answer_ids
    extra = answer_ids - question_ids
    if missing:
        raise ValueError(
            f"response is missing answer ID(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise ValueError(
            f"response has unknown answer ID(s): {', '.join(sorted(extra))}"
        )
    for question_id, question in questions.items():
        question = _mapping(question, f"request.questions[{question_id!r}]")
        question_type = question.get("type")
        if question_type == "noul":
            _validate_noul_answer(
                answers[question_id], f"response.answers[{question_id!r}]"
            )
        elif question_type == "choice":
            _validate_choice_answer(
                answers[question_id], question, f"response.answers[{question_id!r}]"
            )
        else:
            raise ValueError(
                f"unsupported question type for {question_id!r}: {question_type!r}"
            )
    return _validate_usage(response)


def classify(response: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the acceptance threshold to a complete categorization response."""
    response = _mapping(response, "response")
    policy = _validate_policy(policy)
    if response.get("model") != _policy_model(policy):
        raise ValueError("response.model does not match policy.model")
    answers = _mapping(response.get("answers"), "response.answers")
    known_ids = _policy_company_ids(policy)
    if not answers:
        raise ValueError("response.answers must not be empty")
    if any(
        not isinstance(company_id, str) or not company_id.strip()
        for company_id in answers
    ):
        raise ValueError("response answer IDs must be nonempty strings")
    answer_ids = set(answers)
    missing = known_ids - answer_ids
    extra = answer_ids - known_ids
    if missing:
        raise ValueError(
            f"response is missing answer ID(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise ValueError(
            f"response has unknown answer ID(s): {', '.join(sorted(extra))}"
        )
    accept = _accept_probability(policy)
    probabilities: dict[str, float] = {}
    companies: list[str] = []
    for company_id, answer in answers.items():
        probability = _validate_noul_answer(answer, f"response.answers[{company_id!r}]")
        probabilities[company_id] = probability
        if probability >= accept:
            companies.append(company_id)
    return {
        "status": "accepted" if companies else "others",
        "companies": companies,
        "probabilities": probabilities,
    }


__all__ = [
    "classification_request",
    "sentiment_request",
    "request_key",
    "configuration_key",
    "validate_response",
    "classify",
    "load_policy",
]
