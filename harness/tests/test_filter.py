# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Golden tests for the filter compiler: exact SQL text, exact bound params, refusals.

Run: python -m uv run harness/tests/test_filter.py
"""
import datetime as dt
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filter import (CHAMBER_NORM_SQL, COLUMNS, compile_filter, escape_regex, hashtag_pattern,
                    normalize_chamber, term_pattern)

UTC = dt.timezone.utc
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def spec(**filter_block):
    window = filter_block.pop("window", ("2026-09-08", "2026-09-10"))
    source = filter_block.pop("source", "twitter_firehose")
    return {"spec_version": 1, "name": "t",
            "observation": {"intent": "i", "questions_to_answer": [], "source": source,
                            "window": {"from": window[0], "to": window[1]}},
            "filter": filter_block}


def refuses(message_fragment, **kwargs):
    try:
        compile_filter(spec(**kwargs))
    except ValueError as error:
        assert message_fragment in str(error), f"expected {message_fragment!r} in {error!r}"
        return str(error)
    raise AssertionError(f"expected a ValueError mentioning {message_fragment!r}, got none")


@case
def firehose_golden():
    compiled = compile_filter(spec(
        any_terms=["anthropic", {"term": "AI safety", "match": "word"}],
        none_terms=["giveaway"], languages=["en"], min_likes=5))
    assert compiled["where"] == (
        "created_at >= $date_from::TIMESTAMPTZ"
        "\n  AND created_at < $date_to::TIMESTAMPTZ"
        "\n  AND (regexp_matches(body, $any_0) OR regexp_matches(body, $any_1))"
        "\n  AND NOT regexp_matches(body, $none_0)"
        "\n  AND list_contains($langs, lang)"
        "\n  AND like_count >= $min_likes"
        "\n  AND NOT starts_with(body, 'RT @')"
        "\n  AND coalesce(reply_to_status_id, '') = ''"
        "\n  AND coalesce(quoting_id, '') = ''"), compiled["where"]
    assert compiled["params"] == {
        "date_from": dt.datetime(2026, 9, 8, tzinfo=UTC), "date_to": dt.datetime(2026, 9, 10, tzinfo=UTC),
        "any_0": "(?i)anthropic", "any_1": r"(?i)\bAI safety\b", "none_0": "(?i)giveaway",
        "langs": ["en"], "min_likes": 5}, compiled["params"]


@case
def all_terms_hashtags_and_retweets_included():
    compiled = compile_filter(spec(any_terms=["anthropic"], all_terms=["safety"],
                                   hashtags=["#AISafety", "claude"]), originals_only=False)
    assert compiled["where"] == (
        "created_at >= $date_from::TIMESTAMPTZ"
        "\n  AND created_at < $date_to::TIMESTAMPTZ"
        "\n  AND regexp_matches(body, $any_0)"
        "\n  AND regexp_matches(body, $all_0)"
        "\n  AND (regexp_matches(body, $tag_0) OR regexp_matches(body, $tag_1))"), compiled["where"]
    assert compiled["params"]["tag_0"] == r"(?i)#AISafety(?:[^\w]|$)", compiled["params"]["tag_0"]
    assert compiled["params"]["tag_1"] == r"(?i)#claude(?:[^\w]|$)", compiled["params"]["tag_1"]
    assert "starts_with" not in compiled["where"]


@case
def regex_escaping():
    assert escape_regex("c++ (beta)") == r"c\+\+ \(beta\)"
    compiled = compile_filter(spec(any_terms=["c++ (beta)", "it's \"great\" 100% $$", "a.b|c[d]"]))
    params = compiled["params"]
    assert params["any_0"] == r"(?i)c\+\+ \(beta\)", params["any_0"]
    # quotes and % are not RE2 metacharacters and are never concatenated into SQL, so they stay literal
    assert params["any_1"] == "(?i)it's \"great\" 100% \\$\\$", params["any_1"]
    assert params["any_2"] == r"(?i)a\.b\|c\[d\]", params["any_2"]
    assert "it's" not in compiled["where"] and "$any_1" in compiled["where"]


@case
def word_match_rules():
    assert compile_filter(spec(any_terms=[{"term": "UN", "match": "word", "case_sensitive": True}]))["params"]["any_0"] == r"\bUN\b"
    message = refuses("not ASCII", any_terms=[{"term": "café", "match": "word"}])
    assert "substring" in message, message
    refuses("match must be", any_terms=[{"term": "x", "match": "fuzzy"}])
    refuses("non-empty", any_terms=["  "])


@case
def word_boundaries_only_next_to_word_characters():
    """A \\b next to '+' or '$' can never match: 'C++' with match 'word' used to find literally nothing."""
    assert term_pattern({"term": "C++", "match": "word"}) == r"(?i)\bC\+\+", \
        term_pattern({"term": "C++", "match": "word"})
    assert term_pattern({"term": "$AAPL", "match": "word"}) == r"(?i)\$AAPL\b"
    assert term_pattern({"term": "AI:", "match": "word"}) == r"(?i)\bAI:"
    assert term_pattern({"term": ".NET", "match": "word"}) == r"(?i)\.NET\b"
    assert term_pattern({"term": "AI", "match": "word", "case_sensitive": True}) == r"\bAI\b"
    assert term_pattern({"term": "_x_", "match": "word"}) == r"(?i)\b_x_\b"
    # no word character on either side: both boundaries are dead, so refuse instead of matching nothing
    message = refuses("letter, digit or '_'", any_terms=[{"term": "++", "match": "word"}])
    assert "substring" in message, message
    refuses("letter, digit or '_'", any_terms=[{"term": ":)", "match": "word"}])


@case
def term_lists_must_be_lists():
    """A bare string would be iterated character by character: any_terms 'anthropic' -> 9 patterns
    matching almost every tweet, languages 'en' -> codes 'e' and 'n' matching none."""
    for field in ("any_terms", "all_terms", "none_terms", "hashtags", "languages"):
        message = refuses(f"filter.{field} must be a list", **{field: "anthropic"})
        assert "'anthropic'" in message, message
    assert compile_filter(spec(any_terms=("anthropic", "claude")))["params"]["any_1"] == "(?i)claude"  # tuples are fine
    refuses("filter.congress.handles must be a list", source="congress", window=("2026-07-01", "2026-08-01"),
            congress={"handles": "RepFoo"})
    refuses("filter.congress.handles[1] must be a non-empty string", source="congress",
            window=("2026-07-01", "2026-08-01"), congress={"handles": ["RepFoo", None]})
    refuses("filter.languages[0] must be a non-empty string", any_terms=["x"], languages=[""])
    refuses("filter.any_terms must be a list", any_terms={"term": "anthropic"})


@case
def malformed_drafts_raise_value_error_not_attribute_error():
    """preview() runs before validate_project, so the compiler sees unvalidated model output."""
    draft = spec(any_terms=["anthropic"])
    draft["observation"]["window"] = None
    for label, broken in (("window", draft),
                          ("filter", {**spec(any_terms=["x"]), "filter": "anthropic"}),
                          ("observation", {**spec(any_terms=["x"]), "observation": "twitter"})):
        try:
            compile_filter(broken)
        except ValueError as error:
            assert "must be" in str(error), (label, error)
        else:
            raise AssertionError(f"{label}: expected a ValueError")
    refuses("filter.congress must be an object", source="congress", window=("2026-07-01", "2026-08-01"),
            congress="Democrat")
    refuses("filter.min_likes must be a whole number", any_terms=["x"], min_likes="5")
    assert compile_filter(spec(any_terms=["x"], min_likes=5.0))["params"]["min_likes"] == 5
    # a spec that is not even a dict still gets a field-named refusal, never an AttributeError
    try:
        compile_filter(None)
    except ValueError as error:
        assert "observation.window.from" in str(error), error


@case
def hashtags_accept_the_term_object_form():
    """spec.py types filter.hashtags as terms (string or object), so the compiler must take both."""
    assert hashtag_pattern({"term": "#ai"}) == hashtag_pattern("#ai") == r"(?i)#ai(?:[^\w]|$)"
    assert hashtag_pattern({"term": "#AI", "match": "substring", "case_sensitive": True}) == r"#AI(?:[^\w]|$)"
    compiled = compile_filter(spec(hashtags=[{"term": "#ai", "match": "substring", "case_sensitive": False}]))
    assert compiled["params"]["tag_0"] == r"(?i)#ai(?:[^\w]|$)", compiled["params"]["tag_0"]
    message = refuses("not allowed", hashtags=[{"term": "#ai", "match": "word", "case_sensitive": False}])
    assert "any_terms" in message, message
    refuses("a hashtag must be a non-empty string", hashtags=[{"term": "#", "match": "substring"}])
    refuses("a hashtag must be a string or an object", hashtags=[7])


@case
def originals_only_is_named_per_source():
    """Congress has no reply/quote columns, so 'originals only' can only drop retweets there."""
    assert compile_filter(spec(any_terms=["x"]))["originals_only"] is True
    assert compile_filter(spec(any_terms=["x"]), originals_only=False)["originals_only"] is False
    congress = compile_filter(spec(source="congress", window=("2026-07-01", "2026-08-01"), any_terms=["x"]))
    assert congress["originals_only"] == "retweets_only", congress["originals_only"]
    assert "reply_to_status_id" not in congress["where"] and "quoting_id" not in congress["where"]


@case
def congress_golden():
    compiled = compile_filter(spec(
        source="congress", window=("2026-07-01", "2026-08-01"), any_terms=["medicaid"],
        congress={"party": "Democrat", "chamber": "representative", "state": "ca",
                  "handles": ["@RepFoo", "Bar"]}))
    assert compiled["where"] == (
        "created_at IS NOT NULL"
        "\n  AND created_at >= $date_from::TIMESTAMP"
        "\n  AND created_at < $date_to::TIMESTAMP"
        "\n  AND regexp_matches(text, $any_0)"
        "\n  AND lower(party) = lower($party)"
        f"\n  AND {CHAMBER_NORM_SQL} = $chamber"
        "\n  AND upper(state) = upper($state)"
        "\n  AND list_contains($handles, lower(author_handle))"
        "\n  AND NOT starts_with(text, 'RT @')"), compiled["where"]
    assert compiled["params"]["date_from"] == dt.datetime(2026, 7, 1), compiled["params"]["date_from"]
    assert compiled["params"]["date_from"].tzinfo is None, "congress created_at is a naive TIMESTAMP"
    assert compiled["params"]["chamber"] == "House", compiled["params"]["chamber"]
    assert compiled["params"]["handles"] == ["repfoo", "bar"], compiled["params"]["handles"]


@case
def congress_refusals_and_normalization():
    assert [normalize_chamber(v) for v in ("house", "representative", "Senate", "senator", "Executive")] == \
        ["House", "House", "Senate", "Senate", "Executive"]
    refuses("chamber must be", source="congress", window=("2026-07-01", "2026-08-01"),
            congress={"chamber": "Governor"})
    refuses("no lang column", source="congress", window=("2026-07-01", "2026-08-01"), languages=["en"])
    refuses("no engagement columns", source="congress", window=("2026-07-01", "2026-08-01"), min_likes=1)
    refuses("only valid when", congress={"party": "Democrat"})


@case
def window_rules():
    refuses("must be after", window=("2026-09-10", "2026-09-10"))
    refuses("YYYY-MM-DD", window=("09/10/2026", "2026-09-11"))
    refuses("unknown source", source="bluesky_live")


@case
def hashtag_and_column_map():
    assert hashtag_pattern("ai") == hashtag_pattern("#ai") == r"(?i)#ai(?:[^\w]|$)"
    firehose, congress = COLUMNS["twitter_firehose"], COLUMNS["congress"]
    assert (firehose["text"], firehose["id"]) == ("body", "id")
    assert all(firehose[flag] for flag in ("has_lang", "has_likes", "has_version", "dedupe"))
    assert (congress["text"], congress["id"]) == ("text", "tweet_id")
    assert not any(congress[flag] for flag in ("has_lang", "has_likes", "has_version", "dedupe"))
    assert compile_filter(spec())["columns"]["path"] == "twitter-firehose/tweets-*.parquet"


failed = 0
for test in CASES:
    try:
        test()
        print(f"PASS {test.__name__}")
    except Exception:
        failed += 1
        print(f"FAIL {test.__name__}\n{traceback.format_exc()}")
print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
