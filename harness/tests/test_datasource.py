# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz", "jsonschema"]
# ///
"""Live tests for the DuckDB data source against the real parquet on this laptop.

Run: python -m uv run harness/tests/test_datasource.py
Windows are September days (a September day scans in ~1 s) plus one July congress month.
Connections are capped at 2 GB / 2 threads because several agents query at the same time.
"""
import datetime as dt
import sys
import threading
import time
import traceback
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource_duckdb
from datasource_duckdb import DOMINANT_TEXT_SHARE, DuckDBSource, hash_spec
from filter import compile_filter

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = DuckDBSource(ROOT, memory_limit="2GB", threads=2)
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def spec(source="twitter_firehose", window=("2026-09-09", "2026-09-10"), **filter_block):
    return {"spec_version": 1, "name": "live",
            "observation": {"intent": "i", "questions_to_answer": [], "source": source,
                            "window": {"from": window[0], "to": window[1]}},
            "filter": filter_block, "sampling": {"max_posts": 20000, "strategy": "stratified_by_day", "seed": 7}}


def validatable(source="twitter_firehose", window=("2026-09-09", "2026-09-10"), **filter_block):
    """A draft spec.py accepts (its schema requires every property of every object), so that the
    preview/validate handshake of DESIGN.md 5.1 can be exercised end to end."""
    block = {"any_terms": [], "all_terms": [], "none_terms": [], "hashtags": [], "languages": [],
             "min_likes": 0, "congress": None}
    block.update(filter_block)
    return {"spec_version": 1, "name": "live",
            "observation": {"intent": "what people say about it", "source": source,
                            "questions_to_answer": ["how do they feel about it?"],
                            "window": {"from": window[0], "to": window[1]}},
            "filter": block,
            "sampling": {"max_posts": 2000, "strategy": "stratified_by_day", "seed": 7},
            "classification": [{"name": "is_about_it", "type": "noul",
                                "instructions": "Is this post about the topic?", "options": None}],
            "relevance_gate": None,
            "sentiment": {"type": "score", "instructions": "How positive is the post?",
                          "criteria": ["tone", "anger"]},
            "budget": {"max_usd": 1.0}}


def scan(sql, params, timeout=120):
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET memory_limit='2GB'")
    con.execute("SET threads=2")
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        timer.cancel()
        con.close()


def count_matches(project_spec, *, originals_only):
    compiled = compile_filter(project_spec, originals_only=originals_only)
    window = project_spec["observation"]["window"]
    if compiled["columns"]["source"] == "congress":
        files = [str(ROOT / compiled["columns"]["path"]).replace("\\", "/")]
    else:
        files = SOURCE.files_for_window(window["from"], window["to"])
    started = time.monotonic()
    rows = scan(f"SELECT count(DISTINCT {compiled['columns']['id']}) FROM read_parquet($files) "
                f"WHERE {compiled['where']}", {"files": files, **compiled["params"]})
    return rows[0][0], len(files), time.monotonic() - started


@case
def files_for_window_pre_filters_396_files():
    one_day = SOURCE.files_for_window("2026-09-09", "2026-09-10")
    assert len(one_day) == 3, len(one_day)
    assert one_day == SOURCE.files_for_window(dt.date(2026, 9, 9), dt.date(2026, 9, 10)), "dates and strings must agree"
    whole = SOURCE.files_for_window("2026-08-17", "2026-09-18")
    assert len(whole) == 396, len(whole)
    assert SOURCE.files_for_window("2026-10-01", "2026-10-02") == []
    return f"1 day -> {len(one_day)} of {len(whole)} files"


@case
def live_anthropic_5396_distinct_ids():
    """The canary: 4,821 instead of 5,396 means SET TimeZone='UTC' is missing."""
    total, files, seconds = count_matches(spec(any_terms=["anthropic"]), originals_only=False)
    assert total == 5396, f"expected 5396 distinct ids, got {total} (4821 = missing timezone fix)"
    return f"{total} distinct ids over {files} files in {seconds:.1f}s"


@case
def live_anthropic_originals_only():
    total, _, seconds = count_matches(spec(any_terms=["anthropic"]), originals_only=True)
    assert 0 < total < 5396, total
    return f"originals only: {total} of 5396 distinct ids ({total / 5396:.1%}) in {seconds:.1f}s"


@case
def live_word_match_survives_non_word_edges():
    """'C++' with match 'word' used to compile to '\\bC\\+\\+\\b' and match nothing at all, while
    spec.py's TERM_SHORT_AMBIGUOUS hint asks for exactly that match for a short term."""
    counts = {}
    for label, term in (("C++ substring", "C++"),
                        ("C++ word", {"term": "C++", "match": "word"}),
                        ("C++ word cs", {"term": "C++", "match": "word", "case_sensitive": True}),
                        (".NET substring", ".NET"), (".NET word", {"term": ".NET", "match": "word"}),
                        ("$AAPL substring", "$AAPL"), ("$AAPL word", {"term": "$AAPL", "match": "word"})):
        counts[label] = count_matches(spec(any_terms=[term]), originals_only=True)[0]
    assert counts["C++ substring"] == 5, counts          # measured on 2026-09-09 originals
    assert counts["C++ word"] == 5, counts               # was 0 while both \b were forced on
    assert counts["C++ word cs"] == 4, counts            # the dropped one is a lower-case 'c++'
    assert counts[".NET word"] == 16 and counts[".NET substring"] == 17, counts   # was 11
    assert counts["$AAPL word"] == 97 and counts["$AAPL substring"] == 99, counts  # was 0
    matched = scan("SELECT list(regexp_matches(b, $p)) FROM (VALUES ('i love C++ a lot'), ('C++'), "
                   "('x C++!'), ('ABCC++'), ('c++')) t(b)",
                   {"p": compile_filter(spec(any_terms=[{"term": "C++", "match": "word",
                                                         "case_sensitive": True}]))["params"]["any_0"]})
    assert matched[0][0] == [True, True, True, False, False], matched
    return ", ".join(f"{k}={v}" for k, v in counts.items())


@case
def live_hashtag_filter():
    # the alternation stands in for a look-ahead: '#ai' must not match '#airplane'
    rows = scan("SELECT body, regexp_matches(body, $p) FROM (VALUES ('love #ai!'), ('#airplane'), "
                "('ends with #ai'), ('#AI rocks')) AS t(body)",
                {"p": compile_filter(spec(hashtags=["#ai"]))["params"]["tag_0"]})
    assert [matched for _, matched in rows] == [True, False, True, True], rows
    total, _, seconds = count_matches(spec(hashtags=["#ai"]), originals_only=True)
    assert total > 0, total
    return f"#ai originals on 2026-09-09: {total} in {seconds:.1f}s"


@case
def live_congress_chamber_normalization():
    window = ("2026-07-01", "2026-08-01")
    normalized, _, seconds = count_matches(
        spec("congress", window, congress={"party": "Democrat", "chamber": "House"}), originals_only=True)
    congress_file = [str(ROOT / "congress-tweets" / "congress-tweets-unified.parquet").replace("\\", "/")]
    raw = dict(scan("SELECT chamber, count(*) FROM read_parquet($files) WHERE created_at IS NOT NULL "
                    "AND created_at >= $a::TIMESTAMP AND created_at < $b::TIMESTAMP "
                    "AND lower(party) = 'democrat' AND NOT starts_with(text, 'RT @') GROUP BY 1",
                    {"files": congress_file, "a": dt.datetime(2026, 7, 1), "b": dt.datetime(2026, 8, 1)}))
    spellings = {"House": raw.get("House", 0), "representative": raw.get("representative", 0)}
    assert normalized > 0, normalized
    assert spellings["representative"] > 0, "the month must contain the 'representative' spelling to prove anything"
    assert normalized >= spellings["representative"], f"{normalized} < {spellings}"
    assert normalized == spellings["House"] + spellings["representative"], f"{normalized} != {spellings}"
    return f"Democrat House Jul 2026: {normalized} = House {spellings['House']} + representative {spellings['representative']} ({seconds:.1f}s)"


@case
def preview_two_september_days():
    preview = SOURCE.preview(spec(window=("2026-09-08", "2026-09-10"), any_terms=["anthropic"], languages=["en"]))
    assert "error" not in preview, preview.get("error")
    for field in ("exact", "tier", "total", "interval95", "per_day", "language_mix", "duplicate_text_share",
                  "examples", "warnings", "seconds", "spec_hash", "denominator"):
        assert field in preview, f"missing {field}"
    assert preview["tier"] == "exact" and preview["exact"] is True, preview["tier"]
    assert preview["ok"] is True and preview["originals_only"] is True, preview["originals_only"]
    assert preview["interval95"] is None, preview["interval95"]
    assert preview["total"] > 100, preview["total"]
    assert len(preview["spec_hash"]) == 64
    assert preview["denominator"] == "originals, lang=en", preview["denominator"]
    days = {row["day"]: row for row in preview["per_day"]}
    assert set(days) <= {"2026-09-08", "2026-09-09"}, list(days)
    ninth = days["2026-09-09"]
    assert ninth["denominator"] == 226135, ninth  # originals for 2026-09-09 x lang=en
    assert abs(ninth["per_100k"] - ninth["count"] / 226135 * 100_000) < 0.01, ninth
    assert 0.0 <= preview["duplicate_text_share"] <= 1.0, preview["duplicate_text_share"]
    assert 0 < len(preview["examples"]) <= 10, len(preview["examples"])
    assert {e["pick"] for e in preview["examples"]} == {"top", "random"}
    assert all(len(e["body"]) <= 280 and e["lang"] == "en" for e in preview["examples"])
    assert preview["examples"][0]["like_count"] >= preview["examples"][4]["like_count"]
    return (f"total {preview['total']} in {preview['seconds']}s, per_100k {ninth['per_100k']}, "
            f"dup share {preview['duplicate_text_share']}, {len(preview['warnings'])} warnings")


@case
def preview_flags_a_manufactured_campaign():
    preview = SOURCE.preview(spec(hashtags=["#MyXAnniversary"]))
    assert preview["duplicate_text_share"] > DOMINANT_TEXT_SHARE, preview["duplicate_text_share"]
    campaign = [w for w in preview["warnings"] if "manufactured campaign" in w]
    assert campaign, preview["warnings"]
    assert any(f"only {preview['total']} matches" in w for w in preview["warnings"]), preview["warnings"]
    return f"dup share {preview['duplicate_text_share']} of {preview['total']} matches -> {campaign[0][:60]}"


@case
def duplicate_share_ignores_textless_posts():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    try:
        # 4 copies of one template, 3 link-only posts that normalize to nothing, 3 distinct posts
        con.execute("""CREATE TEMP TABLE matched AS SELECT * FROM (VALUES
            ('a', 'buy now'), ('b', 'buy now'), ('c', 'buy now'), ('d', 'buy now'),
            ('e', ''), ('f', ''), ('g', ''), ('h', 'one'), ('i', 'two'), ('j', 'three'))
            AS t(id, norm_text)""")
        warnings = []
        share = SOURCE._duplicates(con, 10, warnings)
        assert share == 0.4, share                       # the 3 empty ones are not duplicates
        assert not warnings, warnings                    # 4 of 10 is 40% but under the 5-copy floor
        con.execute("INSERT INTO matched VALUES ('k', 'buy now')")
        warnings = []
        assert SOURCE._duplicates(con, 11, warnings) == round(5 / 11, 4)
        assert len(warnings) == 1 and "45% of matches (5 posts)" in warnings[0], warnings
    finally:
        con.close()
    return "4/10 without a warning, 5/11 with one"


@case
def preview_timeout_is_structured():
    preview = SOURCE.preview(spec(any_terms=["a"]), timeout_s=0.001)
    assert "error" in preview, preview
    assert preview["error"]["code"] == "timeout", preview["error"]
    assert preview["error"]["hint"] and preview["request_spec_hash"], preview
    assert len(preview["request_spec_hash"]) == 64, preview["request_spec_hash"]
    # a failed preview must not be readable as "previewed, found nothing": no spec_hash, no counts
    assert "spec_hash" not in preview, "a failed preview must not carry the field that proves a preview"
    assert preview["ok"] is False and preview["exact"] is None and preview["total"] is None, preview
    assert any("failed" in w for w in preview["warnings"]), preview["warnings"]
    return f"code={preview['error']['code']} after {preview['seconds']}s, keys {sorted(preview)}"


@case
def a_failed_preview_does_not_validate_as_previewed():
    """DESIGN.md 5.1: the confirm/submit gate rests on 'this spec was actually previewed'. A timed-out
    preview used to match the hash and read as a sampled zero, i.e. a warning instead of an error."""
    try:
        import spec as spec_module
    except ImportError as error:
        return f"SKIP: spec.py not importable ({error})"
    draft = validatable(any_terms=["anthropic"])
    clean = [(i["severity"], i["code"]) for i in spec_module.validate_project(draft, today="2026-09-19")]
    assert clean == [("error", "PREVIEW_MISSING")], clean  # the draft itself is otherwise valid
    failed = SOURCE.preview(draft, timeout_s=0.001)
    issues = spec_module.validate_project(draft, preview=failed, today="2026-09-19")
    blocking = [i["code"] for i in issues if i["severity"] == "error"]
    assert blocking, f"a failed preview must block validation, got {issues}"
    good = SOURCE.preview(draft, timeout_s=60)
    assert good["ok"] is True and good["spec_hash"] == spec_module.canonical_hash(draft)
    assert not spec_module.validate_project(draft, preview=good, today="2026-09-19"), "the real preview validates"
    return f"failed -> blocking {blocking}, successful -> clean ({good['total']} matches)"


@case
def preview_of_a_malformed_draft_is_structured():
    """preview runs before validate_project (DESIGN.md 10), so it sees raw model output first."""
    window_none = validatable(any_terms=["anthropic"])
    window_none["observation"]["window"] = None
    cases = {
        "window null": window_none,
        "congress as a string": validatable("congress", ("2026-07-01", "2026-08-01"),
                                            any_terms=["medicaid"], congress="Democrat"),
        "any_terms as a string": validatable(any_terms="anthropic"),
        "min_likes as a string": validatable(any_terms=["anthropic"], min_likes="5"),
        "filter as a string": {**validatable(any_terms=["anthropic"]), "filter": "anthropic"},
    }
    codes = {}
    for label, draft in cases.items():
        preview = SOURCE.preview(draft, timeout_s=5)          # must not raise
        assert "error" in preview, (label, preview)
        assert preview["error"]["code"] == "invalid_filter", (label, preview["error"])
        assert preview["ok"] is False and "spec_hash" not in preview, (label, sorted(preview))
        assert preview["error"]["message"] and preview["error"]["hint"], (label, preview["error"])
        codes[label] = preview["error"]["message"][:40]
    return "; ".join(f"{k}: {v}" for k, v in codes.items())


@case
def preview_whole_month_uses_the_sample_tier():
    if not (ROOT / "harness" / "data" / "prepared" / "sample.parquet").exists():
        return "SKIP: harness/data/prepared/sample.parquet not built yet"
    preview = SOURCE.preview(spec(window=("2026-08-17", "2026-09-18"), any_terms=["anthropic"]))
    assert "error" not in preview, preview.get("error")
    assert preview["tier"] == "sample" and preview["exact"] is False, preview["tier"]
    assert preview["seconds"] < 5, preview["seconds"]
    low, high = preview["interval95"]
    assert low <= preview["total"] <= high, preview["interval95"]
    assert preview["duplicate_text_share"] is None, "duplicate share is exact-tier only"
    assert any("1%" in w or "sample" in w for w in preview["warnings"]), preview["warnings"]
    assert any("2026-09-01" in w for w in preview["warnings"]), preview["warnings"]
    assert any("2026-09-17" in w for w in preview["warnings"]), preview["warnings"]
    assert len(preview["per_day"]) > 20, len(preview["per_day"])
    return f"~{preview['total']:,} [{low:,}..{high:,}] from {preview['sample_hits']} sample hits in {preview['seconds']}s"


@case
def sample_tier_examples_are_labelled_and_caveated():
    """The agent quotes examples as evidence, so the top of a 1% sample must never read as THE top post."""
    if not (ROOT / "harness" / "data" / "prepared" / "sample.parquet").exists():
        return "SKIP: sample.parquet not built yet"
    sampled = SOURCE.preview(spec(window=("2026-08-17", "2026-09-18"), any_terms=["anthropic"]))
    assert sampled["tier"] == "sample", sampled["tier"]
    assert {e["pick"] for e in sampled["examples"]} == {"top_in_sample", "random_in_sample"}, \
        [e["pick"] for e in sampled["examples"]]
    caveat = [w for w in sampled["warnings"] if "example" in w.lower()]
    assert caveat, sampled["warnings"]
    assert "NOT" in caveat[0] and "sample" in caveat[0], caveat
    exact = SOURCE.preview(spec(window=("2026-09-09", "2026-09-10"), any_terms=["anthropic"]))
    assert {e["pick"] for e in exact["examples"]} == {"top", "random"}, [e["pick"] for e in exact["examples"]]
    assert not any("example" in w.lower() for w in exact["warnings"]), exact["warnings"]
    sample_top, exact_top = sampled["examples"][0]["like_count"], exact["examples"][0]["like_count"]
    assert exact_top > 10 * sample_top, (sample_top, exact_top)  # why the label matters: ~76x here
    return (f"sample top {sample_top:,} likes vs the exact top of one day {exact_top:,} "
            f"({exact_top / sample_top:.0f}x), labelled {sampled['examples'][0]['pick']}")


@case
def preview_never_reports_a_sample_zero_as_no_matches():
    if not (ROOT / "harness" / "data" / "prepared" / "sample.parquet").exists():
        return "SKIP: sample.parquet not built yet"
    preview = SOURCE.preview(spec(window=("2026-08-17", "2026-09-18"), any_terms=["zzqxjwvkplt"]))
    assert preview["tier"] == "sample" and preview["total"] == 0, preview["tier"]
    assert preview["interval95"] == [0, 300], preview["interval95"]
    zero = [w for w in preview["warnings"] if "NOT zero" in w]
    assert zero, preview["warnings"]
    assert not any("no matches" in w for w in preview["warnings"]), preview["warnings"]
    return f"interval {preview['interval95']}, said: {zero[0][:70]}"


@case
def preview_falls_back_to_exact_without_the_sample():
    budget = datasource_duckdb.EXACT_TWEET_BUDGET
    source = DuckDBSource(ROOT, memory_limit="2GB", threads=2)
    source.prepared = ROOT / "harness" / "data" / "prepared-does-not-exist"
    datasource_duckdb.EXACT_TWEET_BUDGET = 1_000  # make a cheap 1-day window "too big"
    try:
        preview = source.preview(spec(any_terms=["anthropic"]))
    finally:
        datasource_duckdb.EXACT_TWEET_BUDGET = budget
    assert preview["tier"] == "exact" and preview["total"] == 484, (preview["tier"], preview["total"])
    fallback = [w for w in preview["warnings"] if "prepare_data.py" in w]
    assert fallback, preview["warnings"]
    return fallback[0][:90]


@case
def preview_congress():
    preview = SOURCE.preview(spec("congress", ("2026-07-01", "2026-08-01"), any_terms=["medicaid"],
                                  congress={"party": "Democrat", "chamber": "House"}))
    assert "error" not in preview, preview.get("error")
    assert preview["tier"] == "exact" and preview["total"] > 0, preview
    assert preview["language_mix"] is None, preview["language_mix"]
    assert preview["denominator"].startswith("none:"), preview["denominator"]
    assert all(row["per_100k"] is None and row["denominator"] is None for row in preview["per_day"])
    assert all(example["like_count"] is None and example["lang"] is None for example in preview["examples"])
    assert any("no engagement or language" in w for w in preview["warnings"]), preview["warnings"]
    # the congress file has no reply_to_status_id and no quoting_id, so "originals" can only mean
    # "not a retweet" there: say so in the field and in a warning instead of claiming originals
    assert preview["originals_only"] == "retweets_only", preview["originals_only"]
    replies = [w for w in preview["warnings"] if "quote-tweets cannot be identified" in w]
    assert replies, preview["warnings"]
    assert "RT @" in replies[0] and "replies" in replies[0], replies
    return f"{preview['total']} posts over {len(preview['per_day'])} days in {preview['seconds']}s"


@case
def congress_counts_include_replies():
    """Proves the warning is not hypothetical: reply-shaped posts really are inside the count."""
    project = spec("congress", ("2026-07-01", "2026-08-01"),
                   congress={"party": "Democrat", "chamber": "House"})
    compiled = compile_filter(project, originals_only=True)
    files = [str(ROOT / compiled["columns"]["path"]).replace("\\", "/")]
    counted, reply_shaped = scan(
        "SELECT count(*), count(*) FILTER (WHERE starts_with(text, '@')) FROM read_parquet($files) "
        f"WHERE {compiled['where']}", {"files": files, **compiled["params"]})[0]
    assert counted == 11705 and reply_shaped == 213, (counted, reply_shaped)
    assert compiled["originals_only"] == "retweets_only", compiled["originals_only"]
    return f"{reply_shaped} of {counted} 'original' Democrat House posts start with '@' (replies)"


@case
def describe_reports_coverage_and_caveats():
    described = SOURCE.describe()
    names = [source["name"] for source in described["sources"]]
    assert names == ["twitter_firehose", "congress"], names
    firehose, congress = described["sources"]
    assert firehose["coverage"] == {"from": "2026-08-17", "to": "2026-09-17"}, firehose["coverage"]
    assert firehose["rows"] > 390_000_000, firehose["rows"]
    assert congress["coverage"]["to"] == "2026-08-24", congress["coverage"]
    assert congress["rows"] == 5_095_245 and congress["undated_rows"] == 28, congress
    assert not congress["has_language"] and not congress["has_engagement"]
    assert len(firehose["columns"]) == 23 and len(set(firehose["columns"])) == 23, firehose["columns"]
    assert "views_count" in firehose["columns"] and "version" in firehose["columns"]
    caveats = " ".join(described["caveats"])
    for fragment in ("2026-09-01", "Replies are under-collected", "2026-09-17", "no engagement and no language"):
        assert fragment in caveats, fragment
    assert described["partial_days"] == ["2026-09-17"], described["partial_days"]
    return f"{firehose['rows']:,} firehose rows, congress {congress['rows']:,}, {len(described['caveats'])} caveats"


@case
def spec_hash_is_canonical():
    first = hash_spec({"b": 1, "a": [1, {"d": 2, "c": 3}]})
    second = hash_spec({"a": [1, {"c": 3, "d": 2}], "b": 1})
    assert first == second == hash_spec({"b": 1, "a": [1, {"d": 2, "c": 3}]}), (first, second)
    assert first != hash_spec({"b": 2, "a": [1, {"d": 2, "c": 3}]})
    return first[:12]


failed = 0
for test in CASES:
    started = time.monotonic()
    try:
        note = test()
        print(f"PASS {test.__name__} ({time.monotonic() - started:.1f}s): {note or ''}")
    except Exception:
        failed += 1
        print(f"FAIL {test.__name__}\n{traceback.format_exc()}")
print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
