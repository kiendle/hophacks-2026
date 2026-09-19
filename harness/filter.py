# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Compile a project spec's filter block into parameterized DuckDB SQL (DESIGN.md 5.2).

    from filter import compile_filter
    c = compile_filter(spec)                  # {"where": sql, "params": {...}, "columns": {...}}
    con.execute(f"SELECT count(DISTINCT id) FROM read_parquet($files) WHERE {c['where']}",
                {"files": files, **c["params"]})

Every user string is regex-escaped and bound as a parameter; nothing is concatenated into SQL.
Groups are AND-ed together: (any_terms OR-ed) AND (all_terms) AND NOT (none_terms)
AND (hashtags OR-ed) AND languages AND min_likes AND congress AND originals-only.
Put a hashtag in any_terms instead if you want it OR-ed with the terms.

Anything malformed raises ValueError naming the field: preview() turns that into a structured
invalid_filter error the model can fix. A filter must never *silently* mean something else, so a
bare string where a list belongs, or a word match that cannot match, is a refusal and not a guess.
"""
import datetime as dt
import string

# RE2 metacharacters. Everything else (space, '#', '-', '&', quotes) is a literal and is left alone
# so that bound patterns stay readable in logs.
RE2_META = frozenset(r"\.+*?()[]{}|^$")
# What RE2's \b considers a word character. \b next to anything else can never match (RE2 is
# ASCII-only for \b), which is why term_pattern places the boundaries one side at a time.
WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_")

CHAMBER_NORM_SQL = (
    "CASE WHEN lower(chamber) IN ('house', 'representative') THEN 'House'"
    " WHEN lower(chamber) IN ('senate', 'senator') THEN 'Senate'"
    " WHEN chamber IS NOT NULL THEN 'Executive' END"
)
_CHAMBERS = {"house": "House", "representative": "House", "senate": "Senate",
             "senator": "Senate", "executive": "Executive"}

COLUMNS = {
    "twitter_firehose": {
        "source": "twitter_firehose",
        "path": "twitter-firehose/tweets-*.parquet",
        "text": "body",
        "id": "id",
        "time": "created_at",
        "time_type": "TIMESTAMPTZ",
        "has_lang": True,
        "has_likes": True,
        "has_version": True,
        "has_thread": True,
        "dedupe": True,
        "select": ["id", "author_id", "body", "created_at", "lang", "like_count", "retweet_count",
                   "views_count", "reply_to_status_id", "quoting_id", "version", "added_at"],
    },
    "congress": {
        "source": "congress",
        "path": "congress-tweets/congress-tweets-unified.parquet",
        "text": "text",
        "id": "tweet_id",
        "time": "created_at",
        "time_type": "TIMESTAMP",
        "has_lang": False,
        "has_likes": False,
        "has_version": False,
        "has_thread": False,
        "dedupe": False,
        "select": ["tweet_id", "author_handle", "author_name", "party", "chamber", "state",
                   "created_at", "text", "topic", "source_corpus"],
    },
}

# The congress file has no reply_to_status_id and no quoting_id, so "original posts only" can only
# drop 'RT @' there. Callers must say so instead of claiming originals (DESIGN.md 2).
ORIGINALS_ONLY = {"twitter_firehose": True, "congress": "retweets_only"}


def escape_regex(text):
    return "".join("\\" + c if c in RE2_META else c for c in text)


def _unwrap(term, what):
    """A spec term is a string or {term, match, case_sensitive} -> (text, match, case_sensitive)."""
    if isinstance(term, str):
        return term, "substring", False
    if isinstance(term, dict):
        return term.get("term"), term.get("match") or "substring", bool(term.get("case_sensitive"))
    raise ValueError(f"{what} must be a string or an object, got {type(term).__name__}")


def term_pattern(term):
    """A spec term (string or {term, match, case_sensitive}) -> one RE2 pattern."""
    text, match, case_sensitive = _unwrap(term, "a term")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"a term must be a non-empty string, got {text!r}")
    if match not in ("substring", "word"):
        raise ValueError(f"match must be 'substring' or 'word', got {match!r}")
    pattern = escape_regex(text)
    if match == "word":
        if not text.isascii():
            raise ValueError(
                f"match 'word' needs an ASCII term; {text!r} is not ASCII and RE2's \\b only works on "
                "ASCII word characters, so the pattern would silently match nothing. Use match 'substring'.")
        # A boundary belongs only next to a word character: '\bC\+\+\b' asks for a word character
        # after '+' and matches nothing at all, so "C++" keeps the left \b only.
        left, right = text[0] in WORD_CHARS, text[-1] in WORD_CHARS
        if not (left or right):
            raise ValueError(
                f"match 'word' needs a letter, digit or '_' at the start or the end of the term; "
                f"{text!r} starts with {text[0]!r} and ends with {text[-1]!r}, so both \\b boundaries "
                "would match nothing. Use match 'substring'.")
        pattern = (r"\b" if left else "") + pattern + (r"\b" if right else "")
    return ("" if case_sensitive else "(?i)") + pattern


def hashtag_pattern(tag):
    """#tag followed by a non-word character or end of text. RE2 has no look-ahead, hence the alternation.

    Accepts the same string-or-object form as the other term lists (spec.py types hashtags as terms),
    but not match 'word': the alternation already ends the tag, and a leading \\b before '#' is dead."""
    text, match, case_sensitive = _unwrap(tag, "a hashtag")
    if not isinstance(text, str) or not text.lstrip("#").strip():
        raise ValueError(f"a hashtag must be a non-empty string, got {text!r}")
    if match != "substring":
        raise ValueError(
            f"filter.hashtags is always matched to the end of the tag, so match {match!r} is not allowed "
            "there ('#ai' never matches '#airplane'). Drop match, or move the term to any_terms.")
    prefix = "" if case_sensitive else "(?i)"
    return prefix + r"#" + escape_regex(text.lstrip("#").strip()) + r"(?:[^\w]|$)"


def normalize_chamber(value):
    """31 spellings in the data collapse to 3 chambers (DESIGN.md 2)."""
    if not isinstance(value, str) or value.strip().lower() not in _CHAMBERS:
        raise ValueError(f"chamber must be House, Senate or Executive, got {value!r}")
    return _CHAMBERS[value.strip().lower()]


def compile_filter(spec, *, originals_only=True):
    spec = _mapping(spec, "the spec")
    observation = _mapping(spec.get("observation"), "observation")
    source = observation.get("source") or "twitter_firehose"
    if source not in COLUMNS:
        raise ValueError(f"unknown source {source!r}; expected one of {sorted(COLUMNS)}")
    columns = COLUMNS[source]
    text, time_type = columns["text"], columns["time_type"]
    block = _mapping(spec.get("filter"), "filter")
    conditions, params = [], {}

    window = _mapping(observation.get("window"), "observation.window")
    date_from = _midnight(window.get("from"), "observation.window.from")
    date_to = _midnight(window.get("to"), "observation.window.to")
    if date_to <= date_from:
        raise ValueError(f"window.to must be after window.from ({date_from} .. {date_to})")
    if time_type == "TIMESTAMPTZ":
        utc = dt.timezone.utc
        params["date_from"] = dt.datetime.combine(date_from, dt.time(), utc)
        params["date_to"] = dt.datetime.combine(date_to, dt.time(), utc)
    else:
        params["date_from"] = dt.datetime.combine(date_from, dt.time())
        params["date_to"] = dt.datetime.combine(date_to, dt.time())
        conditions.append("created_at IS NOT NULL")  # 28 congress rows have no date
    conditions.append(f"created_at >= $date_from::{time_type}")
    conditions.append(f"created_at < $date_to::{time_type}")

    any_terms = _list(block.get("any_terms"), "any_terms")
    if any_terms:
        matches = []
        for i, term in enumerate(any_terms):
            params[f"any_{i}"] = term_pattern(term)
            matches.append(f"regexp_matches({text}, $any_{i})")
        conditions.append(_group(matches, " OR "))
    for i, term in enumerate(_list(block.get("all_terms"), "all_terms")):
        params[f"all_{i}"] = term_pattern(term)
        conditions.append(f"regexp_matches({text}, $all_{i})")
    for i, term in enumerate(_list(block.get("none_terms"), "none_terms")):
        params[f"none_{i}"] = term_pattern(term)
        conditions.append(f"NOT regexp_matches({text}, $none_{i})")
    hashtags = _list(block.get("hashtags"), "hashtags")
    if hashtags:
        matches = []
        for i, tag in enumerate(hashtags):
            params[f"tag_{i}"] = hashtag_pattern(tag)
            matches.append(f"regexp_matches({text}, $tag_{i})")
        conditions.append(_group(matches, " OR "))

    languages = _codes(_list(block.get("languages"), "languages"), "languages")
    if languages:
        if not columns["has_lang"]:
            raise ValueError(f"source {source!r} has no lang column, so filter.languages cannot be applied")
        params["langs"] = languages  # a list cannot be bound to IN (...)
        conditions.append("list_contains($langs, lang)")
    min_likes = _count(block.get("min_likes"), "min_likes")
    if min_likes > 0:
        if not columns["has_likes"]:
            raise ValueError(f"source {source!r} has no engagement columns, so filter.min_likes cannot be applied")
        params["min_likes"] = min_likes
        conditions.append("like_count >= $min_likes")

    congress = block.get("congress")
    if congress:
        if source != "congress":
            raise ValueError("filter.congress is only valid when observation.source is 'congress'")
        congress = _mapping(congress, "filter.congress")
        if congress.get("party"):
            params["party"] = congress["party"]
            conditions.append("lower(party) = lower($party)")
        if congress.get("chamber"):
            params["chamber"] = normalize_chamber(congress["chamber"])
            conditions.append(f"{CHAMBER_NORM_SQL} = $chamber")
        if congress.get("state"):
            params["state"] = congress["state"]
            conditions.append("upper(state) = upper($state)")
        handles = [h.lstrip("@").strip().lower()
                   for h in _codes(_list(congress.get("handles"), "congress.handles"), "congress.handles")]
        if handles:
            params["handles"] = handles
            conditions.append("list_contains($handles, lower(author_handle))")

    if originals_only:
        conditions.append(f"NOT starts_with({text}, 'RT @')")
        if columns["has_thread"]:
            conditions.append("coalesce(reply_to_status_id, '') = ''")
            conditions.append("coalesce(quoting_id, '') = ''")
    return {"where": "\n  AND ".join(conditions), "params": params, "columns": dict(columns),
            "originals_only": ORIGINALS_ONLY[source] if originals_only else False}


def _group(matches, joiner):
    return matches[0] if len(matches) == 1 else "(" + joiner.join(matches) + ")"


def _mapping(value, field):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError(f"{field} must be an object, got {type(value).__name__} ({value!r})")


def _list(value, field):
    """A bare string here would be iterated character by character: 'anthropic' as any_terms would
    match nearly every tweet and 'en' as languages nothing at all. Refuse instead."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        raise ValueError(f"filter.{field} must be a list, got the string {value!r}; write it as "
                         f"[{value!r}] (a string would be read one character at a time)")
    raise ValueError(f"filter.{field} must be a list, got {type(value).__name__} ({value!r})")


def _codes(values, field):
    for i, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"filter.{field}[{i}] must be a non-empty string, got {value!r}")
    return [value.strip() for value in values]


def _count(value, field):
    if value is None or value is False:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError(f"filter.{field} must be a whole number, got {value!r}")
    return int(value)


def _midnight(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a 'YYYY-MM-DD' string, got {value!r}")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a 'YYYY-MM-DD' date, got {value!r}") from None
