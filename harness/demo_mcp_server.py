# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "duckdb==1.5.5", "jsonschema>=4.23,<5", "pytz", "aiohttp>=3.11,<4"]
# ///
"""The only tool surface the headless Claude Code can reach (harness/DESIGN.md §0, §7).

Started by claude_runner.py through a per-session mcp.json, so the session is
identified by the environment, never by an argument the model can influence:
HARNESS_SESSION, HARNESS_SESSION_DIR (private state), HARNESS_REPO (parquet).

Optional tool modules (brief_tools, analysis_tools, jev_tools) are added by load_plugins() when the
server starts, so a stream of work adds tools without editing this file. A test that only imports
this module sees the tools defined here; call load_plugins(mcp) to get the optional ones too.
"""
import asyncio
import functools
import hashlib
import importlib
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bluesky  # noqa: E402  (this file is run by path, so the harness directory is not on sys.path by default)
import steps  # noqa: E402  (the same plain wording the chat's step list and project card use)

mcp = MCPServer("harness")

TTL_MS = 300_000
MAX_WINDOW_DAYS = 3
SCAN_TIMEOUT_S = 60
LIVE_BUDGET_S = 40
LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})?")
REGEX_META = re.compile(r"([\\.^$|()\[\]{}*+?])")  # RE2 rejects unknown escapes, so escape only what it knows
_FILE_RANGES: list[tuple[str, object, object]] | None = None

SOURCES = [
    {
        "id": "bluesky_live", "name": "live Bluesky",
        "covers": {"from": "right now, every public post as it is published", "to": "now, about a second behind"},
        "posts": "about 35 to 80 posts a second across the whole network", "languages": "all languages, taken from the post itself",
        "columns": ["uri", "text", "langs", "created_at", "like_count", "repost_count", "reply_count", "handle"],
        "caveats": [
            "This is the source for anything happening now, and for a project that keeps running and watching.",
            "A look back of about a day is possible, but 15 minutes is what a preview covers in seconds. Longer look backs are read newest first and may be partial.",
            "Likes and reposts are the totals right now, not what the post had at the time.",
            "Bluesky is much smaller than the X/Twitter archive. A quiet 15 minutes does not mean a quiet topic. Widen the window before concluding anything.",
        ],
    },
    {
        "id": "twitter_firehose", "name": "the X/Twitter archive",
        "covers": {"from": "2026-08-17", "to": "2026-09-17"}, "posts": 377_270_972, "languages": "all languages, 26% Japanese and 31% English",
        "columns": ["id", "body", "created_at", "lang", "like_count", "retweet_count", "views_count", "reply_to_status_id", "quoting_id"],
        "caveats": [
            "Days from 2026-09-01 hold only 0.8 to 4.8 million posts against 22 to 29 million in August. The collection changed, not the world. Compare shares between days, never raw counts.",
            "We only count posts people wrote themselves. Reposts, quotes and replies are left out.",
            "The same post can be stored more than once, so every count is over distinct posts.",
        ],
    },
    {
        "id": "congress", "name": "US Congress posts",
        "covers": {"from": "1999-11-29", "to": "2026-08-24"}, "posts": 5_095_245, "languages": "not recorded",
        "columns": ["tweet_id", "text", "created_at", "chamber", "party", "state", "handle"],
        "caveats": ["No like or view counts at all, so nothing can be weighted or sorted by how popular it was.",
                    "This one cannot be previewed in this demo."],
    },
]


def session_dir() -> Path:
    directory = Path(os.environ["HARNESS_SESSION_DIR"])
    (directory / "confirmations").mkdir(parents=True, exist_ok=True)
    return directory


def repo() -> Path:
    return Path(os.environ.get("HARNESS_REPO") or Path(__file__).resolve().parent.parent)


def fail(code: str, message: str, hint: str) -> dict:
    return {"error": {"code": code, "message": message, "hint": hint}}


REASON_LIMIT = 240


def needs_reason(function):
    """Every step the user sees is real: the WHAT and the outcome are built from this call and its
    result, and the WHY is this `reason`, in the model's own words. A tool without one does not run.
    The wrapper keeps the wrapped signature, so `reason` stays required in the tool's schema, and
    still answers a call that arrives without it with our own structured error instead of a crash.
    """
    @functools.wraps(function)
    def wrapper(reason=None, *arguments, **keywords):
        text = " ".join(reason.split()) if isinstance(reason, str) else ""
        if not text:
            return fail("no_reason", "Every step needs a reason, one short plain sentence for the user.",
                        "Call it again with reason=\"…\", saying in the user's language why you are doing this right now.")
        return function(text[:REASON_LIMIT], *arguments, **keywords)
    return wrapper


def now_ms() -> int:
    return int(time.time() * 1000)


def spec_hash(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write_json(path: Path, payload) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # a reader must never see half a confirmation
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)


def draft_file() -> Path:
    return session_dir() / "draft.json"


def draft_path() -> str:
    """Where the project really is on this laptop, relative to the repo: the user asks to see the file."""
    path = draft_file()
    try:
        return path.resolve().relative_to(repo().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_draft():
    path = draft_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def drop_confirmations() -> None:
    for path in (session_dir() / "confirmations").glob("*.json"):
        path.unlink(missing_ok=True)


def one_line(value, limit: int = 120) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value))[:limit]


def branch(value, *keys):
    """A draft written by the model may nest a field or not, so read both spellings defensively."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def render_window(source: str, window) -> str:
    """A live project looks back and then keeps collecting; an archive project has two fixed dates."""
    window = window if isinstance(window, dict) else {}
    if source == "bluesky_live" or window.get("mode") == "live":
        return steps.live_words(window.get("lookback_hours"), window.get("run_hours"))
    return steps.window_words(window.get("from"), window.get("to"))


def render_groups(spec) -> list:
    groups = spec.get("categories") or spec.get("classification") or []
    out = []
    for group in groups[:12] if isinstance(groups, list) else []:
        name = one_line(group.get("name") if isinstance(group, dict) else group, 60)
        about = one_line((group.get("description") or group.get("about")) if isinstance(group, dict) else "", 160)
        if name:
            out.append(f"{name}: {about}" if about else name)
    return out


def render_summary(spec) -> str:
    """The words next to the Confirm button, written here from the draft, never by the model (DESIGN.md §10).

    Same labels and same values as the project card in the chat, so the two never disagree.
    """
    if not isinstance(spec, dict):
        return "There is nothing saved to confirm yet."
    observation = spec.get("observation") if isinstance(spec.get("observation"), dict) else {}
    window = observation.get("window") if isinstance(observation.get("window"), dict) else {}
    source = one_line(branch(observation, "source") or spec.get("source") or "", 40)
    keywords = spec.get("keywords") or branch(spec, "filter", "any_terms") or []
    languages = spec.get("language") or branch(observation, "language") or branch(spec, "filter", "languages")
    feeling = spec.get("sentiment_question") or branch(spec, "sentiment", "instructions")
    rows = [
        ("Name", one_line(spec.get("name") or "")),
        ("What we are watching", one_line(branch(observation, "intent") or spec.get("intent") or "", 240)),
        ("Where", steps.source_words(source)),
        ("When", render_window(source, {**window, "from": window.get("from") or spec.get("date_from"),
                                        "to": window.get("to") or spec.get("date_to")})),
        ("Language", ", ".join(steps.language_words(code) for code in languages[:6]) if isinstance(languages, list) and languages
         else steps.language_words(languages if isinstance(languages, str) else "")),
        ("Words we search for", steps.word_list(keywords if isinstance(keywords, list) else [])),
        ("Feeling question", one_line(feeling, 240)),
    ]
    lines = [f"{label}: {value}" for label, value in rows if value]
    groups = render_groups(spec)
    if groups:
        lines.insert(len(lines) - 1 if lines and lines[-1].startswith("Feeling question") else len(lines),
                     "Groups we sort posts into:\n" + "\n".join(groups))
    if not [line for line in lines if not line.startswith("Language:")]:  # "Any language" alone says nothing
        return "There is nothing saved to confirm yet."
    return steps.plain("\n".join(lines))


@mcp.tool()
@needs_reason
def describe_sources(reason: str) -> dict:
    """List the data sources this product can observe, their coverage, size and caveats.

    Call this FIRST, before saying anything about what data exists or proposing dates. There are two
    kinds: Bluesky live, which is happening right now and can keep running, and the historical
    archives, whose fixed coverage window and September collection change decide which dates are
    worth observing. Preview the live source with bluesky_recent, the archive with preview_keywords.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    import classified_data
    sources = [classified_data.info() if source['id'] == 'twitter_firehose' and classified_data.available() else source for source in SOURCES]
    return {"sources": sources, "preview_window_limit_days": MAX_WINDOW_DAYS,
            "live_source": "bluesky_live", "historical_sources": ["twitter_firehose", "congress"]}


def files_for_window(connection, low: str, high: str) -> list[str]:
    """Parquet files are strictly time-ordered; reading their min/max first turns a 30x file scan into a 1x one."""
    global _FILE_RANGES
    if _FILE_RANGES is None:
        every = sorted(str(path) for path in (repo() / "twitter-firehose").glob("tweets-*.parquet"))
        _FILE_RANGES = connection.execute(
            "SELECT file_name, min(stats_min_value)::TIMESTAMPTZ, max(stats_max_value)::TIMESTAMPTZ FROM parquet_metadata($files)"
            " WHERE path_in_schema = 'created_at' GROUP BY file_name ORDER BY file_name", {"files": every}).fetchall()
    bounds = connection.execute("SELECT $low::TIMESTAMPTZ, $high::TIMESTAMPTZ", {"low": low, "high": high}).fetchone()
    return [name for name, first, last in _FILE_RANGES if last >= bounds[0] and first < bounds[1]]


def search_pattern(keyword: str) -> str:
    """A search word as RE2 sees it, always ignoring capitals.

    A short word (4 letters or fewer) or one written in capitals (PS6, AI) matches only as a whole
    word: "ps6" must not match inside "MCqPS6NfCl", and "AI" must not match inside "said". A longer
    word keeps matching anywhere, so "playstation" still finds "PlayStations". A word that does not
    start and end with a plain letter or digit, or that is not ASCII, keeps matching anywhere too,
    because RE2's word boundary only knows ASCII.
    """
    escaped = REGEX_META.sub(r"\\\1", keyword)
    plain_edges = keyword.isascii() and keyword[:1].isalnum() and keyword[-1:].isalnum()
    whole_word = len(keyword) <= 4 or (keyword.isupper() and any(character.isalpha() for character in keyword))
    return "(?i)" + (rf"\b{escaped}\b" if plain_edges and whole_word else escaped)


# Links are not words anyone posted: a t.co address such as https://t.co/MCqPS6NfCl carries "PS6"
# inside it and matched 87 posts about nothing. Words are matched against the post with its links
# taken out; the examples still show the post exactly as it was written.
LINKLESS = "regexp_replace(body, 'https?://\\S+', ' ', 'g')"

# An example post goes to the page twice: `body`, the short form the card shows first, and
# `full_text`, what "Show full post" opens. Both are the post exactly as it was written.
BODY_LIMIT, FULL_TEXT_LIMIT = 240, 2000
STATUS_ID = re.compile(r"[0-9]{1,25}")  # ASCII digits only: \d would also take digits of other scripts


def post_url(identifier) -> str | None:
    """Where the post lives on X, for an id that is digits and nothing else.

    The id comes out of the archive, not from us, and it becomes part of an address a person will
    click, so anything that is not a plain number ("12/../x", "1?x=", "１２３", True) gets no link.
    """
    if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
        return None
    text = str(identifier)
    return f"https://x.com/i/web/status/{text}" if STATUS_ID.fullmatch(text) else None


def example_post(identifier, day, like_count, lang, body, full_text) -> dict:
    """One example row. DuckDB cuts both texts, since its left() never splits one emoji in two."""
    return {"id": identifier, "day": day, "like_count": like_count, "lang": lang, "body": body or "",
            "full_text": full_text or "", "url": post_url(identifier)}


def scan(keywords: list[str], low: str, high: str, language: str | None) -> dict:
    import classified_data
    if classified_data.available():
        return classified_data.preview(keywords, low, high, language)
    started = time.time()
    connection = duckdb.connect()  # connections are not thread-safe: this one is born and dies inside this thread
    timer = threading.Timer(SCAN_TIMEOUT_S, connection.interrupt)
    try:
        for setting in ("SET TimeZone='UTC'", "SET threads=4", "SET memory_limit='4GB'"):
            connection.execute(setting)
        files = files_for_window(connection, low, high)
        if not files:
            return fail("out_of_coverage", "We have no posts saved for those dates.", "The archive covers 2026-08-17 to 2026-09-17. Call describe_sources.")
        # Two stages, because a word inside a t.co code is not a mention ("ps6" on 2026-09-10 passed 28
        # posts raw and 6 really said it; 70 and 21 counting reposts) and stripping the links off every
        # row is slow. The raw match stays the cheap prefilter that keeps the scan fast, and only its
        # survivors are read again without links. DuckDB does not promise to run one WHERE in order, so
        # the two stages have to be two queries, not an AND.
        arguments = {"files": files, "low": low, "high": high} | {f"k{index}": search_pattern(keyword) for index, keyword in enumerate(keywords)}
        arguments |= {f"p{index}": "(?i)" + REGEX_META.sub(r"\\\1", keyword) for index, keyword in enumerate(keywords)}
        prefilter = " OR ".join(f"regexp_matches(body, $p{index})" for index in range(len(keywords)))
        matches = " OR ".join(f"regexp_matches(plain, $k{index})" for index in range(len(keywords)))
        if language:
            arguments["lang"] = language
        timer.start()
        connection.execute(f"""
            CREATE OR REPLACE TEMP TABLE matched AS
            SELECT id, body, created_at, lang, like_count, version FROM (
                SELECT id, body, created_at, lang, like_count, version, {LINKLESS} AS plain
                FROM read_parquet($files)
                WHERE created_at >= $low::TIMESTAMPTZ AND created_at < $high::TIMESTAMPTZ
                  AND ({prefilter})
                  AND NOT starts_with(body, 'RT @')
                  AND coalesce(reply_to_status_id, '') = '' AND coalesce(quoting_id, '') = ''
                  {"AND lang = $lang" if language else ""}) AS rough
            WHERE ({matches})""", arguments)
        total = connection.execute("SELECT count(DISTINCT id) FROM matched").fetchone()[0]
        per_day = connection.execute(
            "SELECT strftime(created_at::DATE, '%Y-%m-%d') AS day, count(DISTINCT id) AS count FROM matched GROUP BY 1 ORDER BY 1").fetchall()
        examples = connection.execute(f"""
            SELECT id, strftime(created_at::DATE, '%Y-%m-%d'), like_count, lang, left(body, {BODY_LIMIT}), left(body, {FULL_TEXT_LIMIT})
            FROM matched QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1
            ORDER BY like_count DESC NULLS LAST LIMIT 6""").fetchall()
    except duckdb.InterruptException:
        return fail("timeout", f"The search passed {SCAN_TIMEOUT_S} seconds and was stopped.", "Use a shorter window, one day, or fewer and narrower words.")
    except duckdb.Error as error:
        return fail("query_failed", "The search could not be finished.", f"Check the words and the dates, then try again. {one_line(error, 300)}")
    finally:
        timer.cancel()
        connection.close()
    return {
        "keywords": keywords, "date_from": low[:10], "date_to": high[:10], "language": language, "exact": True,
        "files_scanned": len(files), "seconds": round(time.time() - started, 1), "total": total,
        "per_day": [{"day": day, "count": count} for day, count in per_day],
        "examples": [example_post(*row) for row in examples],
        "counts": "distinct posts people wrote themselves, no reposts, quotes or replies",
        "matching": "words are matched against the post with its links taken out, and a short or capitalised word matches only as a whole word",
    }


@mcp.tool()
@needs_reason
def preview_keywords(reason: str, keywords: list[str], date_from: str, date_to: str, language: str | None = None) -> dict:
    """Count how many real posts a set of keywords matches per day, with the six most-liked examples.

    Call this before finalizing a project. Show the user the numbers and the examples and ask whether
    that is what they meant. It is the only way either of you learns whether the words work.
    For the active classified export, dates also accept exact UTC ISO timestamps with an exclusive
    end, with no three-day limit. It uses saved company/product aliases or whole-word text, ANY
    keyword; AI means the whole export. Only publication events are counted. For activity driving
    the displayed chart, use query_classified_posts instead. Language labels are unavailable.
    The following limits apply only to the legacy raw archive fallback:
    Dates look like 2026-09-09. date_from is the first day counted and date_to is the day after the
    last one, at most three days apart in this demo. Words are matched against the post with its
    links taken out, and capitals are ignored. A word of four letters or fewer, or one written in
    capitals (PS6, AI), matches only as a whole word, so it cannot hide inside a link or a longer
    word. A longer word still matches anywhere, so "playstation" finds "PlayStations". Only posts
    people wrote themselves are counted, never reposts, quotes or replies. language is an exact code
    such as "en" or "ja", or null for every language.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    if not keywords or not isinstance(keywords, list) or any(not isinstance(k, str) or not 2 <= len(k.strip()) <= 80 for k in keywords):
        return fail("bad_keywords", "Give 1 to 20 search words, each 2 to 80 letters long.", "Try the words people actually type, for example a product name and a nickname for it.")
    if len(keywords) > 20:
        return fail("bad_keywords", "We can look for at most 20 words at a time.", "Preview the most promising ones first.")
    import classified_data
    if classified_data.available():
        try:
            return classified_data.preview([k.strip() for k in keywords], date_from, date_to, language)
        except (TypeError, ValueError) as error:
            return fail("bad_dates", str(error), "Use ISO dates or UTC timestamps with a start before the exclusive end.")
    if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") for date in (date_from, date_to)):
        return fail("bad_dates", "Dates have to look like 2026-09-09.", "date_from is the first day counted, date_to is the day after the last one.")
    low, high = f"{date_from}T00:00:00+00", f"{date_to}T00:00:00+00"
    days = (time.mktime(time.strptime(date_to, "%Y-%m-%d")) - time.mktime(time.strptime(date_from, "%Y-%m-%d"))) / 86400
    if not 0 < days <= MAX_WINDOW_DAYS:
        return fail("window_too_large", f"A preview can cover 1 to {MAX_WINDOW_DAYS} days, and that one asked for {days:.0f}.", "Preview the busiest three days. The project itself may still be wider.")
    with ThreadPoolExecutor(max_workers=1) as pool:  # keeps a 10-second scan off the MCP server's event loop
        return pool.submit(scan, [k.strip() for k in keywords], low, high, language).result()


def live_keywords(keywords) -> dict | None:
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20:
        return fail("bad_keywords", "Give 1 to 20 search words.", "Two or three words people would actually post is usually enough.")
    if any(not isinstance(word, str) or not 2 <= len(word.strip()) <= 80 for word in keywords):
        return fail("bad_keywords", "Every search word must be 2 to 80 letters long.", "Bluesky matches whole words. Write short acronyms in capitals (AI, LLM) to keep them case sensitive.")
    return None


def live_language(language) -> dict | None:
    if language is not None and not (isinstance(language, str) and LANGUAGE.fullmatch(language)):
        return fail("bad_language", "That language was not understood.", "Use a code like \"en\", \"ja\" or \"pt-BR\", or null for all languages. Leave it out unless the user asked for one.")
    return None


def run_live(coroutine) -> dict:
    """MCP tools here are plain functions, so the event loop lives in a worker thread for the call."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@mcp.tool()
@needs_reason
def bluesky_recent(reason: str, keywords: list[str], minutes: int = 15, language: str | None = None) -> dict:
    """What happened in the last N minutes on Bluesky, right now: real posts, counted and quoted.

    This is the preview for the live source, and the tool to answer "what are people saying about X
    right now / today / at the moment". It replays every public Bluesky post of the last `minutes`
    (1 to 60, 15 is a good default) over parallel connections and keeps the ones matching a keyword,
    then asks the Bluesky AppView for each example's current likes and reposts.
    Words match whole words in the post text and in any link card. A word of five letters or fewer
    written in capitals (AI, LLM) matches capitals only. Everything else ignores capitals.
    `covered_fraction` says how much of the window was really read before the time ran out. If it is
    below 1, every count is a floor, and you must say so. A quiet 15 minutes does not mean a quiet
    topic, so widen to 60 minutes before telling the user a subject is dead. language is a code like
    "en", or null for every language.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    problem = live_keywords(keywords) or live_language(language)
    if problem:
        return problem
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
        return fail("bad_window", "The number of minutes has to be a whole number from 1 to 60.", "Start at 15 and widen to 60 if the topic looks quiet.")
    return run_live(bluesky.scan_recent([word.strip() for word in keywords], minutes=minutes, language=language, budget_s=LIVE_BUDGET_S))


@mcp.tool()
@needs_reason
def bluesky_listen(reason: str, keywords: list[str], seconds: int = 20, language: str | None = None) -> dict:
    """Watch the live Bluesky stream for a few seconds and report what went past, as it happens.

    Use this only for the "what is being said this very second" moment, or to show the user that the
    stream is genuinely live: it waits `seconds` (5 to 45, 20 is a good default) of real time and can
    only see what is published while it waits, so a rare topic will match nothing. For any real
    question about the present, including "right now" and "today", use bluesky_recent instead.
    Matching and the result shape are the same as bluesky_recent, with five-second buckets.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    problem = live_keywords(keywords) or live_language(language)
    if problem:
        return problem
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 5 <= seconds <= 45:
        return fail("bad_window", "The number of seconds has to be a whole number from 5 to 45.", "20 seconds is enough to show the stream is live.")
    return run_live(bluesky.listen_live([word.strip() for word in keywords], seconds=seconds, language=language))


@mcp.tool()
@needs_reason
def save_draft(reason: str, spec_json: str) -> dict:
    """Write the project down and show it to the user in the project card inside the chat.

    Call this as soon as you have something concrete, and again after every change the user asks for.
    Include: name, observation.intent, observation.source, observation.window, keywords, language,
    categories (each a name and a description of what belongs in it) and sentiment_question.
    The window depends on the source. For an archive source (twitter_firehose, congress) it is
    {"from": "2026-09-09", "to": "2026-09-12"}, where "to" is the day after the last day you want, so
    that example covers Sep 9, 10 and 11. For bluesky_live it is {"mode": "live", "lookback_hours":
    0-24, "run_hours": 1-168}: how far back to look first, then how long to keep collecting.
    Saving cancels any confirmation the user has not pressed yet, so save before asking to confirm.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    try:
        spec = json.loads(spec_json)
    except ValueError as error:
        return fail("bad_json", "Your project could not be written down.", f"The draft was not valid JSON: {one_line(error, 200)}. Send one JSON object as a string.")
    if not isinstance(spec, dict):
        return fail("bad_json", "Your project could not be written down.", "The draft must be a JSON object. Wrap the fields in {…}.")
    drop_confirmations()
    write_json(draft_file(), spec)
    return {"spec": spec, "spec_hash": spec_hash(spec), "saved": True, "draft_path": draft_path()}


@mcp.tool()
@needs_reason
def request_confirmation(reason: str) -> dict:
    """Put a Confirm / Cancel card in front of the user, summarising the saved draft.

    Call this once the draft is saved and previewed and the user is happy. Then STOP and end your
    turn: only the button the user presses can approve the project, and you will be told about it in
    the next message. The summary shown is generated from the draft itself, so do not restate it.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    draft = read_draft()
    if draft is None:
        return fail("no_draft", "There is nothing saved to confirm yet.", "Call save_draft first.")
    created = now_ms()
    record = {"confirmation_id": secrets.token_urlsafe(12), "spec_hash": spec_hash(draft), "created_ms": created,
              "expires_ms": created + TTL_MS, "decision": None, "decided_ms": None, "consumed": False}
    drop_confirmations()
    write_json(session_dir() / "confirmations" / f"{record['confirmation_id']}.json", record)
    return {"confirmation_id": record["confirmation_id"], "summary": render_summary(draft), "expires_ms": record["expires_ms"],
            "spec": draft, "spec_hash": record["spec_hash"], "draft_path": draft_path(),
            "next": "Wait for the user. Do not call submit_project until you are told the button was pressed."}


@mcp.tool()
@needs_reason
def submit_project(reason: str) -> dict:
    """Submit the confirmed project to the pipeline. This is the only tool with a real side effect.

    Call it only after you have been told the user pressed Confirm, and never say a project was
    submitted unless this tool returned a project_id. It refuses unless the harness itself recorded an
    approval for exactly the draft that is saved now.
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. If you are changing approach, say what you noticed, for example that a word
    was pulling in posts about something else. The user reads it exactly as you wrote it.
    """
    draft = read_draft()
    if draft is None:
        return fail("no_draft", "There is nothing saved to send yet.", "Call save_draft first.")
    wanted, now = spec_hash(draft), now_ms()
    reasons = []
    for path in sorted((session_dir() / "confirmations").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["spec_hash"] != wanted:
            reasons.append("The project changed after you last saw it, so it needs confirming again.")
        elif record["decision"] != "approved":
            reasons.append("You cancelled it." if record["decision"] == "declined" else "You have not pressed Confirm yet.")
        elif record["consumed"]:
            reasons.append("That confirmation was already used once.")
        elif record["expires_ms"] <= now:
            reasons.append("The button expired.")
        else:
            record["consumed"] = True
            write_json(path, record)
            project = {"project_id": f"proj_{secrets.token_hex(4)}", "status": "submitted", "spec_hash": wanted,
                       "confirmation_id": record["confirmation_id"], "submitted_ms": now, "spec": draft}
            write_json(session_dir() / "submitted.json", project)
            return {"project_id": project["project_id"], "status": "submitted",
                    "note": "demo: the real pipeline is owned by a teammate and is not connected yet"}
    return fail("not_confirmed", " ".join(reasons) or "Nobody has been asked to confirm this project yet.",
                "Call save_draft, then request_confirmation, then wait for the user to press Confirm.")


TOOL_MODULES = ("brief_tools", "analysis_tools", "jev_tools", "classified_tools", "automation_tools")  # optional, each with register(mcp)


def load_plugins(server=None, names=TOOL_MODULES):
    """Add the optional tool modules to the MCP server.

    A module that is not there is the normal case and says nothing. A module that IS there and fails
    prints its traceback and is skipped, because a broken stream of work must not cost the whole tool
    surface. brief_tools talks to the product's own server, so it is told where that server is.
    """
    server, loaded = server if server is not None else mcp, []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            continue
        try:
            register = getattr(importlib.import_module(name), "register", None)
            if register is None:
                print(f"tools: {name}.py has no register(mcp), so it was skipped", file=sys.stderr, flush=True)
                continue
            if name == "brief_tools":
                register(server, base_url=os.environ.get("SIGNAL_BASE_URL") or "http://127.0.0.1:5194")
            else:
                register(server)
        except Exception:  # noqa: BLE001
            print(f"tools: {name}.py could not be loaded, the server runs without it", file=sys.stderr, flush=True)
            traceback.print_exc()
            continue
        loaded.append(name)
        print(f"tools: loaded {name}.py", file=sys.stderr, flush=True)
    return loaded


if __name__ == "__main__":
    load_plugins()
    mcp.run()
