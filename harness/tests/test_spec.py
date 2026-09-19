# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4"]
# ///
"""Run: python -m uv run harness/tests/test_spec.py

Every bad-spec case below asserts one expected code, that warnings never come
with errors, and that every issue carries the five documented fields.
"""
import copy
import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spec as S  # noqa: E402

EXAMPLE = json.loads((Path(__file__).parent / "fixtures" / "example_spec.json").read_text(encoding="utf-8"))
CONGRESS_WINDOW = {"from": "2026-08-01", "to": "2026-08-10"}
BANNED_KEYWORDS = ("minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                   "minItems", "maxItems", "pattern", "patternProperties", "propertyNames", "format",
                   "oneOf", "allOf", "not", "if", "dependentSchemas")
CASES = []


def case(function):
    CASES.append(function)
    return function


def preview_for(draft, mode="match", total=4821, exact=True):
    if mode == "none":
        return None
    return {"spec_hash": S.canonical_hash(draft) if mode == "match" else "0" * 64, "total": total, "exact": exact}


def bad(code, severity="error", preview="match", total=4821, exact=True, **validate_kwargs):
    """Registers a case: mutate the example, expect `code`, and check the issue contract."""
    def register(mutate):
        def check():
            draft = mutate(copy.deepcopy(EXAMPLE))
            issues = S.validate_project(draft, preview=preview_for(draft, preview, total, exact), **validate_kwargs)
            for issue in issues:
                assert set(issue) == {"severity", "code", "path", "message", "hint"}, issue
                assert issue["code"] in S.CODES, f"{issue['code']} is not listed in CODES"
                assert issue["severity"] in ("error", "warning"), issue
                assert issue["path"] == "" or issue["path"].startswith("/"), issue
            found = {(i["severity"], i["code"]) for i in issues}
            assert (severity, code) in found, f"expected {severity} {code}, got {sorted(found)}"
            if severity == "warning":
                errors = [i["code"] for i in issues if i["severity"] == "error"]
                assert not errors, f"expected warnings only, got errors {errors}"
        name = f"bad__{code.lower()}"
        seen = sum(1 for registered in CASES if registered.__name__.split("#")[0] == name)
        check.__name__ = name if not seen else f"{name}#{seen + 1}"
        CASES.append(check)
        return mutate
    return register


@case
def example_validates_clean():
    issues = S.validate_project(EXAMPLE, preview=preview_for(EXAMPLE))
    assert issues == [], issues


@case
def schema_is_strict_tool_compatible():
    def walk(node, where):
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for keyword in BANNED_KEYWORDS:
            assert keyword not in node, f"{where} uses {keyword}, which strict tool schemas reject"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{where} allows extra properties"
            assert sorted(node.get("required", [])) == sorted(node.get("properties", {})), f"{where} required != properties"
        for key, value in node.items():
            walk(value, f"{where}.{key}")
    walk(S.SPEC_SCHEMA, "$")
    assert S.SPEC_SCHEMA["properties"]["spec_version"] == {"const": 1}


@case
def hash_is_canonical_sha256():
    text = json.dumps(EXAMPLE, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert S.canonical_hash(EXAMPLE) == sha256(text.encode("utf-8")).hexdigest()


@case
def hash_ignores_key_order():
    def shuffle(value):
        if isinstance(value, dict):
            return {key: shuffle(value[key]) for key in reversed(list(value))}
        return [shuffle(item) for item in value] if isinstance(value, list) else value
    reordered = shuffle(EXAMPLE)
    assert list(reordered) != list(EXAMPLE)
    assert S.canonical_hash(reordered) == S.canonical_hash(EXAMPLE)


@case
def hash_changes_with_every_value():
    for mutate in (lambda d: d["observation"]["window"].__setitem__("to", "2026-09-16"),
                   lambda d: d["filter"]["any_terms"].append("claude"),
                   lambda d: d["filter"].__setitem__("min_likes", 1),
                   lambda d: d["sampling"].__setitem__("seed", 8),
                   lambda d: d["classification"][1]["options"][0].__setitem__("description", "x"),
                   lambda d: d["budget"].__setitem__("max_usd", 1.99),
                   lambda d: d.__setitem__("name", "AI safety backlash ")):
        draft = copy.deepcopy(EXAMPLE)
        mutate(draft)
        assert S.canonical_hash(draft) != S.canonical_hash(EXAMPLE), draft


@case
def jev_questions_shape():
    questions = S.to_jev_questions(EXAMPLE)
    assert list(questions) == ["relevant", "stance", "sentiment"]
    assert questions["relevant"] == {"type": "noul",
                                     "instructions": EXAMPLE["classification"][0]["instructions"]}
    assert "criteria" not in questions["relevant"]
    assert questions["stance"]["type"] == "choice"
    assert questions["stance"]["criteria"] == {"critical": "Blames, distrusts or mocks the company",
                                               "supportive": "Defends or praises the company",
                                               "neutral_news": "Reports or shares without taking a side",
                                               "unclear": "Cannot tell, off-topic, or too short"}
    assert questions["sentiment"]["type"] == "score"
    assert questions["sentiment"]["criteria"] == EXAMPLE["sentiment"]["criteria"]


@case
def cost_estimate_scales_and_carries_the_retry_margin():
    cheap = S.estimate_cost_usd(EXAMPLE)
    doubled = copy.deepcopy(EXAMPLE)
    doubled["sampling"]["max_posts"] *= 2
    assert abs(S.estimate_cost_usd(doubled) - 2 * cheap) < 1e-9
    bare = copy.deepcopy(EXAMPLE)
    bare["sampling"]["max_posts"] = 1_000_000
    expected = 1_000_000 * (80 + S.question_tokens(bare)) * 0.042 / 1e6 * 3
    assert abs(S.estimate_cost_usd(bare) - expected) < 1e-9
    assert 0.4 < cheap < 0.8, cheap  # 20k posts: ~$0.18 of Jev, times the 3x margin


@case
def never_raises_on_junk():
    for junk in (None, [], "spec", 7, {}, {"spec_version": 2}, {"filter": {"any_terms": [{"term": 1}]}}):
        issues = S.validate_project(junk, preview=None)
        assert issues and all(i["severity"] == "error" for i in issues), (junk, issues)
        assert all(i["code"] in S.CODES for i in issues), issues


@bad("WINDOW_OUT_OF_COVERAGE")
def _(d):
    d["observation"]["window"] = {"from": "2026-07-01", "to": "2026-07-05"}
    return d


@bad("WINDOW_DATE_INVALID")
def _(d):
    d["observation"]["window"]["from"] = "08/09/2026"
    return d


@bad("WINDOW_EMPTY")
def _(d):
    d["observation"]["window"] = {"from": "2026-09-10", "to": "2026-09-10"}
    return d


@bad("WINDOW_IN_FUTURE", today="2026-09-01")
def _(d):
    d["observation"]["window"] = {"from": "2026-09-08", "to": "2026-09-17"}
    return d


@bad("WINDOW_CROSSES_COLLECTION_CHANGE", severity="warning")
def _(d):
    d["observation"]["window"] = {"from": "2026-08-28", "to": "2026-09-03"}
    return d


@bad("WINDOW_PARTIAL_DAY", severity="warning")
def _(d):
    d["observation"]["window"]["to"] = "2026-09-18"
    return d


@bad("NO_TERMS")
def _(d):
    d["filter"].update(any_terms=[], all_terms=[], hashtags=[])
    return d


@bad("TOO_MANY_TERMS")
def _(d):
    d["filter"]["any_terms"] = [f"term{i:02d}" for i in range(41)]
    return d


@bad("TERM_LENGTH")
def _(d):
    d["filter"]["any_terms"].append("x")
    return d


@bad("TERM_WORD_MATCH_NON_ASCII")
def _(d):
    d["filter"]["any_terms"].append({"term": "café", "match": "word", "case_sensitive": False})
    return d


@bad("TERM_SHORT_AMBIGUOUS", severity="warning")
def _(d):
    d["filter"]["any_terms"].append("AI")
    return d


@bad("TERM_DUPLICATE", severity="warning")
def _(d):
    d["filter"]["any_terms"].append("Anthropic")
    return d


@bad("HASHTAG_MISSING_HASH")
def _(d):
    d["filter"]["hashtags"] = ["aisafety"]
    return d


@bad("LANGUAGE_CODE_INVALID")
def _(d):
    d["filter"]["languages"] = ["english"]
    return d


@bad("LANGUAGES_NOT_SUPPORTED")
def _(d):
    d["observation"].update(source="congress", window=dict(CONGRESS_WINDOW))
    return d


@bad("MIN_LIKES_NOT_SUPPORTED")
def _(d):
    d["observation"].update(source="congress", window=dict(CONGRESS_WINDOW))
    d["filter"].update(languages=[], min_likes=5)
    return d


@bad("MIN_LIKES_NEGATIVE")
def _(d):
    d["filter"]["min_likes"] = -1
    return d


@bad("CONGRESS_BLOCK_NOT_ALLOWED")
def _(d):
    d["filter"]["congress"] = {"party": None, "chamber": None, "state": None, "handles": []}
    return d


@bad("CONGRESS_CHAMBER_INVALID")
def _(d):
    d["observation"].update(source="congress", window=dict(CONGRESS_WINDOW))
    d["filter"].update(languages=[], congress={"party": "Democrat", "chamber": "representative",
                                               "state": "MD", "handles": []})
    return d


@bad("CONGRESS_PARTY_INVALID")
def _(d):
    d["observation"].update(source="congress", window=dict(CONGRESS_WINDOW))
    d["filter"].update(languages=[], congress={"party": "Dem", "chamber": "House", "state": "MD", "handles": []})
    return d


@bad("CONGRESS_STATE_INVALID")
def _(d):
    d["observation"].update(source="congress", window=dict(CONGRESS_WINDOW))
    d["filter"].update(languages=[], congress={"party": None, "chamber": None, "state": "Maryland", "handles": []})
    return d


@bad("MAX_POSTS_RANGE")
def _(d):
    d["sampling"]["max_posts"] = 50
    return d


@bad("SAMPLING_NOT_REPRESENTATIVE", severity="warning")
def _(d):
    d["sampling"]["strategy"] = "top_engagement"
    return d


@bad("TOO_MANY_QUESTIONS")
def _(d):
    d["classification"] = [{"name": f"question_{i}", "type": "noul", "options": None,
                            "instructions": "Is this post about the conduct of an AI company?"} for i in range(8)]
    d["relevance_gate"] = None
    return d


@bad("QUESTION_NAME_INVALID")
def _(d):
    d["classification"][1]["name"] = "Stance Of Author"
    return d


@bad("QUESTION_NAME_DUPLICATE")
def _(d):
    d["classification"][1]["name"] = "relevant"
    return d


@bad("QUESTION_NAME_RESERVED")
def _(d):
    d["classification"][1]["name"] = "sentiment"
    return d


@bad("INSTRUCTIONS_LENGTH")
def _(d):
    d["classification"][1]["instructions"] = "stance?"
    return d


@bad("NOUL_HAS_OPTIONS")
def _(d):
    d["classification"][0]["options"] = [{"name": "yes", "description": "It is"}]
    return d


@bad("CHOICE_OPTION_COUNT")
def _(d):
    d["classification"][1]["options"] = [{"name": "critical", "description": "Blames the company"}]
    return d


@bad("CHOICE_OPTIONS_MANY", severity="warning")
def _(d):
    d["classification"][1]["options"] = ([{"name": f"bucket_{i}", "description": f"Bucket {i}"} for i in range(12)]
                                         + [{"name": "unclear", "description": "Cannot tell"}])
    return d


@bad("CHOICE_OPTION_DUPLICATE")
def _(d):
    d["classification"][1]["options"].append({"name": "critical", "description": "Also critical"})
    return d


@bad("CHOICE_NO_FALLBACK_OPTION", severity="warning")
def _(d):
    d["classification"][1]["options"] = [o for o in d["classification"][1]["options"] if o["name"] != "unclear"]
    return d


@bad("GATE_QUESTION_UNKNOWN")
def _(d):
    d["relevance_gate"]["question"] = "is_relevant"
    return d


@bad("GATE_QUESTION_NOT_NOUL")
def _(d):
    d["relevance_gate"]["question"] = "stance"
    return d


@bad("GATE_PROBABILITY_RANGE")
def _(d):
    d["relevance_gate"]["min_probability"] = 0
    return d


@bad("SENTIMENT_CRITERIA_COUNT")
def _(d):
    d["sentiment"]["criteria"] = ["Negative"]
    return d


@bad("SENTIMENT_CRITERION_EMPTY")
def _(d):
    d["sentiment"]["criteria"][2] = "   "
    return d


@bad("BUDGET_NOT_POSITIVE")
def _(d):
    d["budget"]["max_usd"] = 0
    return d


@bad("BUDGET_ABOVE_CEILING")
def _(d):
    d["budget"]["max_usd"] = 5.0
    return d


@bad("BUDGET_BELOW_ESTIMATE")
def _(d):
    d["sampling"]["max_posts"] = 200_000
    d["budget"]["max_usd"] = 0.5
    return d


@bad("SCHEMA_INVALID")
def _(d):
    d["exclude_retweets"] = True  # a v2 field: the pipeline scores only original posts now
    return d


@bad("SCHEMA_INVALID")
def _(d):
    del d["classification"][0]["options"]  # strict schemas require every property
    return d


@bad("PREVIEW_MISSING", preview="none")
def _(d):
    return d


@bad("PREVIEW_STALE", preview="stale")
def _(d):
    return d


@bad("PREVIEW_ZERO_MATCHES", total=0, exact=True)
def _(d):
    return d


@bad("PREVIEW_SAMPLED_ZERO", severity="warning", total=0, exact=False)
def _(d):
    return d


def main():
    failures = 0
    for check in CASES:
        try:
            check()
            print(f"PASS {check.__name__}")
        except Exception as error:
            failures += 1
            print(f"FAIL {check.__name__}: {type(error).__name__}: {error}")
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
