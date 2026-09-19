"""The project specification: strict wire schema, canonical hash, cost estimate, validation.

Two schemas on purpose (DESIGN.md §5.1). SPEC_SCHEMA is an Anthropic/MCP *strict*
tool schema, so it carries no length or range keywords and no free-form maps:
questions and choice options are lists of named objects, the string-or-object term
and the nullable blocks are `anyOf`, and every bound lives in validate_project.

v3 (§0): the pipeline scores only original posts, so post_types, exclude_retweets
and collapse_duplicate_text are no longer choices and are absent from the spec.
"""
import json
import re
from datetime import date
from hashlib import sha256

import jsonschema

JEV_USD_PER_MTOK = 0.042
RETRY_MARGIN = 3  # the pipeline retries failed posts; the budget must survive it
MAX_TERMS = 40
MAX_QUESTIONS = 8  # including sentiment
COVERAGE = {  # (first day, first day *after* the data): `to` is exclusive
    "twitter_firehose": ("2026-08-17", "2026-09-18"),
    "congress": ("1999-11-29", "2026-08-25"),
}
COLLECTION_CHANGE = date(2026, 9, 1)  # Aug days hold 22-29M tweets, Sep days 0.8-4.8M
PARTIAL_DAY = date(2026, 9, 17)  # collection stopped mid-day
FALLBACK_OPTIONS = ("unclear", "other", "none")
CHAMBERS = ("House", "Senate", "Executive")
PARTIES = ("Democrat", "Republican", "Independent")
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}$")  # as they appear in the data, including zxx and legacy in/iw
STATE_RE = re.compile(r"^[A-Za-z]{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CODES = {
    "SCHEMA_INVALID": "the draft does not match SPEC_SCHEMA (wrong type, missing or unknown field)",
    "VALIDATOR_CRASH": "the validator itself failed on this draft",
    "WINDOW_DATE_INVALID": "a window bound is not an ISO YYYY-MM-DD date",
    "WINDOW_EMPTY": "window.from is not before window.to (`to` is exclusive)",
    "WINDOW_OUT_OF_COVERAGE": "the window reaches outside the source's coverage",
    "WINDOW_IN_FUTURE": "the window starts after today",
    "WINDOW_CROSSES_COLLECTION_CHANGE": "the window spans the 2026-09-01 collection change (compare shares, not counts)",
    "WINDOW_PARTIAL_DAY": "the window includes 2026-09-17, a partially collected day",
    "NO_TERMS": "no term in any_terms, all_terms or hashtags",
    "TOO_MANY_TERMS": f"more than {MAX_TERMS} terms in total",
    "TERM_LENGTH": "a term is shorter than 2 or longer than 80 characters",
    "TERM_WORD_MATCH_NON_ASCII": "match:'word' on a non-ASCII term (\\b silently fails there)",
    "TERM_SHORT_AMBIGUOUS": "a term of 3 characters or fewer without word + case-sensitive matching",
    "HASHTAG_MISSING_HASH": "a hashtag does not start with '#'",
    "TERM_DUPLICATE": "the same term (ignoring case) appears more than once",
    "LANGUAGE_CODE_INVALID": "a language code is not 2-3 letters",
    "LANGUAGES_NOT_SUPPORTED": "languages set on the Congress source, which has no lang column",
    "MIN_LIKES_NEGATIVE": "min_likes is negative",
    "MIN_LIKES_NOT_SUPPORTED": "min_likes set on the Congress source, which has no engagement data",
    "CONGRESS_BLOCK_NOT_ALLOWED": "a congress filter block on a non-Congress source",
    "CONGRESS_CHAMBER_INVALID": "chamber is not House, Senate or Executive",
    "CONGRESS_PARTY_INVALID": "party is not Democrat, Republican or Independent",
    "CONGRESS_STATE_INVALID": "state is not a two-letter code",
    "MAX_POSTS_RANGE": "max_posts is outside 100-200,000",
    "SAMPLING_NOT_REPRESENTATIVE": "strategy 'top_engagement' makes the results unrepresentative",
    "TOO_MANY_QUESTIONS": f"more than {MAX_QUESTIONS} questions including sentiment",
    "QUESTION_NAME_INVALID": "a question name is not snake_case",
    "QUESTION_NAME_DUPLICATE": "two questions share a name",
    "QUESTION_NAME_RESERVED": "a question is named 'sentiment', which is reserved",
    "INSTRUCTIONS_LENGTH": "question instructions are shorter than 10 or longer than 600 characters",
    "NOUL_HAS_OPTIONS": "a noul (yes/no) question carries options",
    "CHOICE_OPTION_COUNT": "a choice question does not have 2-255 options",
    "CHOICE_OPTIONS_MANY": "a choice question has more than 12 options",
    "CHOICE_OPTION_DUPLICATE": "two options of one question share a name",
    "CHOICE_NO_FALLBACK_OPTION": "a choice question has no unclear/other/none bucket",
    "GATE_QUESTION_UNKNOWN": "relevance_gate names a question that does not exist",
    "GATE_QUESTION_NOT_NOUL": "relevance_gate names a question that is not a noul",
    "GATE_PROBABILITY_RANGE": "relevance_gate.min_probability is not strictly between 0 and 1",
    "SENTIMENT_CRITERIA_COUNT": "sentiment does not have 2-10 criteria",
    "SENTIMENT_CRITERION_EMPTY": "a sentiment criterion is empty",
    "BUDGET_NOT_POSITIVE": "budget.max_usd is not greater than 0",
    "BUDGET_ABOVE_CEILING": "budget.max_usd is above the server-side ceiling",
    "BUDGET_BELOW_ESTIMATE": "the estimated cost exceeds budget.max_usd",
    "PREVIEW_MISSING": "the draft has not been previewed",
    "PREVIEW_STALE": "the preview was taken on a different draft",
    "PREVIEW_ZERO_MATCHES": "an exact preview found no posts",
    "PREVIEW_SAMPLED_ZERO": "the 1% sample found no posts (rare terms show zero 37% of the time)",
}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_TERM = {"anyOf": [
    {"type": "string"},
    _obj({
        "term": {"type": "string"},
        "match": _nullable({"enum": ["substring", "word"]}),
        "case_sensitive": _nullable({"type": "boolean"}),
    }),
]}
_TERMS = {"type": "array", "items": _TERM}

SPEC_SCHEMA = _obj({
    "spec_version": {"const": 1},
    "name": {"type": "string"},
    "observation": _obj({
        "intent": {"type": "string"},
        "questions_to_answer": {"type": "array", "items": {"type": "string"}},
        "source": {"enum": ["twitter_firehose", "congress"]},
        "window": _obj({"from": {"type": "string"}, "to": {"type": "string"}}),
    }),
    "filter": _obj({
        "any_terms": _TERMS,
        "all_terms": _TERMS,
        "none_terms": _TERMS,
        "hashtags": _TERMS,
        "languages": {"type": "array", "items": {"type": "string"}},
        "min_likes": {"type": "integer"},
        "congress": _nullable(_obj({
            "party": _nullable({"type": "string"}),
            "chamber": _nullable({"type": "string"}),
            "state": _nullable({"type": "string"}),
            "handles": {"type": "array", "items": {"type": "string"}},
        })),
    }),
    "sampling": _obj({
        "max_posts": {"type": "integer"},
        "strategy": {"enum": ["stratified_by_day", "top_engagement"]},
        "seed": {"type": "integer"},
    }),
    "classification": {"type": "array", "items": _obj({
        "name": {"type": "string"},
        "type": {"enum": ["noul", "choice"]},
        "instructions": {"type": "string"},
        "options": _nullable({"type": "array", "items": _obj({
            "name": {"type": "string"},
            "description": {"type": "string"},
        })}),
    })},
    "relevance_gate": _nullable(_obj({
        "question": {"type": "string"},
        "min_probability": {"type": "number"},
    })),
    "sentiment": _obj({
        "type": {"const": "score"},
        "instructions": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
    }),
    "budget": _obj({"max_usd": {"type": "number"}}),
})


def canonical_json(spec):
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(spec):
    """Ties preview, validation, confirmation and submission together (§10)."""
    return sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def to_jev_questions(spec):
    """Jev's wire format: one map of questions, criteria as a map for choice and a list for score."""
    questions = {}
    for question in spec.get("classification") or []:
        entry = {"type": question.get("type"), "instructions": question.get("instructions", "")}
        if question.get("type") == "choice":
            entry["criteria"] = {o.get("name"): o.get("description", "") for o in question.get("options") or []}
        questions[question.get("name")] = entry
    sentiment = spec.get("sentiment") or {}
    questions["sentiment"] = {
        "type": sentiment.get("type", "score"),
        "instructions": sentiment.get("instructions", ""),
        "criteria": list(sentiment.get("criteria") or []),
    }
    return questions


def question_tokens(spec):
    """Every character Jev is sent per post besides the post itself, at ~4 characters per token."""
    chars = 0
    for question in spec.get("classification") or []:
        chars += len(str(question.get("instructions") or ""))
        for option in question.get("options") or []:
            chars += len(str(option.get("name") or "")) + len(str(option.get("description") or ""))
    sentiment = spec.get("sentiment") or {}
    chars += len(str(sentiment.get("instructions") or ""))
    chars += sum(len(str(c)) for c in sentiment.get("criteria") or [])
    return chars / 4


def estimate_cost_usd(spec, avg_post_tokens=80):
    """One Jev call per post carrying all questions, times a 3x retry margin (§11)."""
    max_posts = (spec.get("sampling") or {}).get("max_posts") or 0
    tokens = max_posts * (avg_post_tokens + question_tokens(spec))
    return tokens * JEV_USD_PER_MTOK / 1e6 * RETRY_MARGIN


def _issue(severity, code, path, message, hint):
    return {"severity": severity, "code": code, "path": path, "message": message, "hint": hint}


def _pointer(parts):
    return "".join(f"/{part}" for part in parts)


def _iter_terms(filter_block):
    """(pointer, text, match, case_sensitive, field) for every term in every term list."""
    for field in ("any_terms", "all_terms", "none_terms", "hashtags"):
        for index, raw in enumerate(filter_block.get(field) or []):
            pointer = f"/filter/{field}/{index}"
            if isinstance(raw, str):
                yield pointer, raw, "substring", False, field
            else:
                yield pointer, raw.get("term") or "", raw.get("match") or "substring", bool(raw.get("case_sensitive")), field


def _check_window(spec, issues, today):
    source = spec["observation"]["source"]
    window = spec["observation"]["window"]
    bounds = {}
    for key in ("from", "to"):
        text = window[key]
        try:
            if not DATE_RE.match(text):
                raise ValueError(text)
            bounds[key] = date.fromisoformat(text)
        except ValueError:
            issues.append(_issue("error", "WINDOW_DATE_INVALID", f"/observation/window/{key}",
                                 f"window.{key} is {text!r}, which is not an ISO date.",
                                 "write the date as YYYY-MM-DD; it is read as a UTC midnight."))
    if len(bounds) < 2:
        return
    start, end = bounds["from"], bounds["to"]
    coverage_from, coverage_to = (date.fromisoformat(d) for d in COVERAGE[source])
    if start >= end:
        issues.append(_issue("error", "WINDOW_EMPTY", "/observation/window",
                             f"the window from {start} to {end} is empty.",
                             "`to` is exclusive, so it must be at least one day after `from`."))
    if start < coverage_from or end > coverage_to:
        issues.append(_issue("error", "WINDOW_OUT_OF_COVERAGE", "/observation/window",
                             f"{source} covers {coverage_from} up to but not including {coverage_to}, "
                             f"and the window asks for {start} to {end}.",
                             f"move the window inside {coverage_from}..{coverage_to}."))
    if today and start > today:
        issues.append(_issue("error", "WINDOW_IN_FUTURE", "/observation/window/from",
                             f"the window starts on {start}, which is after today ({today}).",
                             "pick a start date in the past."))
    if start < COLLECTION_CHANGE < end:
        issues.append(_issue("warning", "WINDOW_CROSSES_COLLECTION_CHANGE", "/observation/window",
                             f"the window crosses {COLLECTION_CHANGE}, when collection changed: August days hold "
                             "22-29M tweets and September days 0.8-4.8M.",
                             "keep it, but read the series as shares per 100k tweets that day, never as counts."))
    if source == "twitter_firehose" and start <= PARTIAL_DAY < end:
        issues.append(_issue("warning", "WINDOW_PARTIAL_DAY", "/observation/window",
                             f"the window includes {PARTIAL_DAY}, which was only partially collected.",
                             f"set `to` to {PARTIAL_DAY} to end the window before that day."))


def _check_terms(spec, issues):
    terms = list(_iter_terms(spec["filter"]))
    if not [t for t in terms if t[4] != "none_terms"]:
        issues.append(_issue("error", "NO_TERMS", "/filter",
                             "the filter has nothing to search for.",
                             "put at least one term in any_terms, all_terms or hashtags."))
    if len(terms) > MAX_TERMS:
        issues.append(_issue("error", "TOO_MANY_TERMS", "/filter",
                             f"the filter has {len(terms)} terms, more than the {MAX_TERMS} allowed.",
                             "keep the terms that actually change the match count and drop the rest."))
    seen = {}
    for pointer, text, match, case_sensitive, field in terms:
        if not 2 <= len(text) <= 80:
            issues.append(_issue("error", "TERM_LENGTH", pointer,
                                 f"the term {text!r} is {len(text)} characters long.",
                                 "use between 2 and 80 characters."))
        if match == "word" and not text.isascii():
            issues.append(_issue("error", "TERM_WORD_MATCH_NON_ASCII", pointer,
                                 f"the term {text!r} is not ASCII, and word matching would silently match nothing.",
                                 "use match:'substring' for this term."))
        elif len(text) <= 3 and not (match == "word" and case_sensitive):
            issues.append(_issue("warning", "TERM_SHORT_AMBIGUOUS", pointer,
                                 f"the short term {text!r} will match inside other words, as 'AI' does in 'said'.",
                                 "set match:'word' and case_sensitive:true, or use a longer term."))
        if field == "hashtags" and not text.startswith("#"):
            issues.append(_issue("error", "HASHTAG_MISSING_HASH", pointer,
                                 f"the hashtag {text!r} does not start with '#'.",
                                 f"write it as '#{text.lstrip('#')}' or move it to any_terms."))
        if text.lower() in seen:
            issues.append(_issue("warning", "TERM_DUPLICATE", pointer,
                                 f"the term {text!r} is already listed at {seen[text.lower()]}.",
                                 "remove one of the two; duplicates only cost preview time."))
        else:
            seen[text.lower()] = pointer


def _check_source_fields(spec, issues):
    source, filter_block = spec["observation"]["source"], spec["filter"]
    languages = filter_block["languages"]
    for index, code in enumerate(languages):
        if not LANG_RE.match(code):
            issues.append(_issue("error", "LANGUAGE_CODE_INVALID", f"/filter/languages/{index}",
                                 f"{code!r} is not a language code as it appears in the data.",
                                 "use a 2-3 letter code such as 'en', 'ja' or 'zxx'."))
    if languages and source == "congress":
        issues.append(_issue("error", "LANGUAGES_NOT_SUPPORTED", "/filter/languages",
                             "the Congress file has no language column, so a language filter cannot be applied.",
                             "set languages to []."))
    if filter_block["min_likes"] < 0:
        issues.append(_issue("error", "MIN_LIKES_NEGATIVE", "/filter/min_likes",
                             f"min_likes is {filter_block['min_likes']}.",
                             "use 0 or a positive number."))
    elif filter_block["min_likes"] > 0 and source == "congress":
        issues.append(_issue("error", "MIN_LIKES_NOT_SUPPORTED", "/filter/min_likes",
                             "the Congress file carries no engagement data, so min_likes cannot be applied.",
                             "set min_likes to 0."))
    congress = filter_block["congress"]
    if congress is None:
        return
    if source != "congress":
        issues.append(_issue("error", "CONGRESS_BLOCK_NOT_ALLOWED", "/filter/congress",
                             f"a congress block was given but the source is {source}.",
                             "set filter.congress to null, or set observation.source to 'congress'."))
    if congress["chamber"] is not None and congress["chamber"] not in CHAMBERS:
        issues.append(_issue("error", "CONGRESS_CHAMBER_INVALID", "/filter/congress/chamber",
                             f"chamber is {congress['chamber']!r}.",
                             f"the file's 31 spellings are normalized to {', '.join(CHAMBERS)}."))
    if congress["party"] is not None and congress["party"] not in PARTIES:
        issues.append(_issue("error", "CONGRESS_PARTY_INVALID", "/filter/congress/party",
                             f"party is {congress['party']!r}.",
                             f"use one of {', '.join(PARTIES)}."))
    if congress["state"] is not None and not STATE_RE.match(congress["state"]):
        issues.append(_issue("error", "CONGRESS_STATE_INVALID", "/filter/congress/state",
                             f"state is {congress['state']!r}.",
                             "use the two-letter postal code, such as 'MD'."))


def _check_sampling(spec, issues):
    sampling = spec["sampling"]
    if not 100 <= sampling["max_posts"] <= 200_000:
        issues.append(_issue("error", "MAX_POSTS_RANGE", "/sampling/max_posts",
                             f"max_posts is {sampling['max_posts']}.",
                             "use between 100 and 200,000 posts."))
    if sampling["strategy"] == "top_engagement":
        issues.append(_issue("warning", "SAMPLING_NOT_REPRESENTATIVE", "/sampling/strategy",
                             "with 'top_engagement' the results are not representative: only the loudest posts are scored.",
                             "use 'stratified_by_day' unless you only want the loudest posts."))


def _check_classification(spec, issues):
    questions = spec["classification"]
    if len(questions) + 1 > MAX_QUESTIONS:
        issues.append(_issue("error", "TOO_MANY_QUESTIONS", "/classification",
                             f"{len(questions)} questions plus sentiment is more than the {MAX_QUESTIONS} allowed.",
                             "merge the questions that ask the same thing; every question is asked of every post."))
    seen = {}
    for index, question in enumerate(questions):
        pointer, name = f"/classification/{index}", question["name"]
        if name == "sentiment":
            issues.append(_issue("error", "QUESTION_NAME_RESERVED", pointer + "/name",
                                 "'sentiment' is the name of the sentiment question.",
                                 "rename it, for example to 'tone'."))
        elif not NAME_RE.match(name):
            issues.append(_issue("error", "QUESTION_NAME_INVALID", pointer + "/name",
                                 f"the question name {name!r} is not snake_case.",
                                 "use lower-case words joined by underscores, such as 'is_relevant'."))
        if name in seen:
            issues.append(_issue("error", "QUESTION_NAME_DUPLICATE", pointer + "/name",
                                 f"the name {name!r} is already used at {seen[name]}.",
                                 "give every question its own name; the results are keyed by it."))
        else:
            seen[name] = pointer
        if not 10 <= len(question["instructions"]) <= 600:
            issues.append(_issue("error", "INSTRUCTIONS_LENGTH", pointer + "/instructions",
                                 f"the instructions for {name!r} are {len(question['instructions'])} characters long.",
                                 "write one clear question of 10 to 600 characters."))
        options = question["options"]
        if question["type"] == "noul":
            if options is not None:
                issues.append(_issue("error", "NOUL_HAS_OPTIONS", pointer + "/options",
                                     f"{name!r} is a yes/no question but carries options.",
                                     "set options to null, or change type to 'choice'."))
            continue
        options = options or []
        if not 2 <= len(options) <= 255:
            issues.append(_issue("error", "CHOICE_OPTION_COUNT", pointer + "/options",
                                 f"the choice question {name!r} has {len(options)} options.",
                                 "give it between 2 and 255 options; 3 to 6 work best."))
        elif len(options) > 12:
            issues.append(_issue("warning", "CHOICE_OPTIONS_MANY", pointer + "/options",
                                 f"{name!r} has {len(options)} options, and long lists make the labels unstable.",
                                 "group the rare options together, ideally staying under 12."))
        names = {}
        for option_index, option in enumerate(options):
            if option["name"] in names:
                issues.append(_issue("error", "CHOICE_OPTION_DUPLICATE", f"{pointer}/options/{option_index}/name",
                                     f"the option {option['name']!r} appears twice in {name!r}.",
                                     "merge the two options or rename one."))
            names[option["name"]] = option_index
        if options and not any(o["name"].lower() in FALLBACK_OPTIONS for o in options):
            issues.append(_issue("warning", "CHOICE_NO_FALLBACK_OPTION", pointer + "/options",
                                 f"{name!r} has no bucket for posts that do not fit, so off-topic posts are forced into a real label.",
                                 f"add an option named one of {', '.join(FALLBACK_OPTIONS)}."))


def _check_gate(spec, issues):
    gate = spec["relevance_gate"]
    if gate is None:
        return
    question = next((q for q in spec["classification"] if q["name"] == gate["question"]), None)
    if question is None:
        issues.append(_issue("error", "GATE_QUESTION_UNKNOWN", "/relevance_gate/question",
                             f"there is no question named {gate['question']!r}.",
                             "name one of the classification questions, or set relevance_gate to null."))
    elif question["type"] != "noul":
        issues.append(_issue("error", "GATE_QUESTION_NOT_NOUL", "/relevance_gate/question",
                             f"{gate['question']!r} is a {question['type']} question, and the gate needs a probability.",
                             "point the gate at a noul (yes/no) question such as 'is_relevant'."))
    if not 0 < gate["min_probability"] < 1:
        issues.append(_issue("error", "GATE_PROBABILITY_RANGE", "/relevance_gate/min_probability",
                             f"min_probability is {gate['min_probability']}.",
                             "use a probability strictly between 0 and 1, such as 0.5."))


def _check_sentiment(spec, issues):
    criteria = spec["sentiment"]["criteria"]
    if not 2 <= len(criteria) <= 10:
        issues.append(_issue("error", "SENTIMENT_CRITERIA_COUNT", "/sentiment/criteria",
                             f"the sentiment rubric has {len(criteria)} levels.",
                             "use 2 to 10 ordered levels, such as the usual five from very negative to very positive."))
    for index, criterion in enumerate(criteria):
        if not criterion.strip():
            issues.append(_issue("error", "SENTIMENT_CRITERION_EMPTY", f"/sentiment/criteria/{index}",
                                 "one of the sentiment levels is empty.",
                                 "name every level; the labels are what Jev orders the scale by."))


def _check_budget(spec, issues, max_project_usd):
    max_usd = spec["budget"]["max_usd"]
    estimate = estimate_cost_usd(spec)
    if max_usd <= 0:
        issues.append(_issue("error", "BUDGET_NOT_POSITIVE", "/budget/max_usd",
                             f"max_usd is {max_usd}.",
                             "set a positive budget; the estimate for this draft is "
                             f"${estimate:.2f}."))
    if max_usd > max_project_usd:
        issues.append(_issue("error", "BUDGET_ABOVE_CEILING", "/budget/max_usd",
                             f"max_usd is ${max_usd:.2f}, above the ${max_project_usd:.2f} ceiling for one project.",
                             f"lower max_usd to ${max_project_usd:.2f} or less, and lower max_posts with it."))
    if estimate > max_usd:
        issues.append(_issue("error", "BUDGET_BELOW_ESTIMATE", "/budget/max_usd",
                             f"scoring {spec['sampling']['max_posts']} posts costs about ${estimate:.2f} "
                             f"including a {RETRY_MARGIN}x retry margin, more than the ${max_usd:.2f} budget.",
                             "raise max_usd, lower sampling.max_posts, or shorten the instructions."))


def _check_preview(spec, issues, preview):
    if not isinstance(preview, dict):
        issues.append(_issue("error", "PREVIEW_MISSING", "",
                             "this draft has not been previewed, so nobody knows what it would capture.",
                             "run the preview first, then validate again."))
        return
    if preview.get("spec_hash") != canonical_hash(spec):
        issues.append(_issue("error", "PREVIEW_STALE", "",
                             "the draft changed since the preview, so the preview counts belong to another filter.",
                             "run the preview again on the current draft."))
        return
    total = preview.get("total") or 0
    if total:
        return
    if preview.get("exact"):
        issues.append(_issue("error", "PREVIEW_ZERO_MATCHES", "/filter",
                             "an exact preview of this filter found no posts at all.",
                             "widen the terms, drop none_terms, add languages, or move the window."))
    else:
        issues.append(_issue("warning", "PREVIEW_SAMPLED_ZERO", "/filter",
                             "the 1% sample found no posts, which happens 37% of the time for a term with about "
                             "100 true matches.",
                             "an exact count of the busiest days would settle it before you spend the budget."))


def validate_project(spec, *, preview=None, max_project_usd=3.0, today=None):
    """Schema first, then the semantic rules. Returns issues, never raises."""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    if not isinstance(spec, dict):
        return [_issue("error", "SCHEMA_INVALID", "", f"the draft is a {type(spec).__name__}, not an object.",
                       "write the whole specification as one JSON object.")]
    issues = [
        _issue("error", "SCHEMA_INVALID", _pointer(error.absolute_path), error.message,
               "fix the draft to match the spec schema and write it again.")
        for error in sorted(jsonschema.Draft202012Validator(SPEC_SCHEMA).iter_errors(spec),
                            key=lambda e: list(e.absolute_path))
    ]
    if issues:
        return issues  # the semantic rules below assume well-typed fields
    try:
        _check_window(spec, issues, today)
        _check_terms(spec, issues)
        _check_source_fields(spec, issues)
        _check_sampling(spec, issues)
        _check_classification(spec, issues)
        _check_gate(spec, issues)
        _check_sentiment(spec, issues)
        _check_budget(spec, issues, max_project_usd)
        _check_preview(spec, issues, preview)
    except Exception as exc:  # a validator crash must not look like a valid draft
        issues.append(_issue("error", "VALIDATOR_CRASH", "", f"the validator failed on this draft: {exc!r}.",
                             "simplify the draft and validate again; this is a harness bug."))
    return issues
