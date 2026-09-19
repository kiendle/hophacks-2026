# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2", "duckdb>=1.4,<2", "pytz", "aiohttp>=3.11,<4"]
# ///
"""The only tool surface the headless Claude Code can reach (harness/DESIGN.md §0, §7).

Started by claude_runner.py through a per-session mcp.json, so the session is
identified by the environment, never by an argument the model can influence:
HARNESS_SESSION, HARNESS_SESSION_DIR (private state), HARNESS_REPO (parquet).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bluesky  # noqa: E402  (this file is run by path, so the harness directory is not on sys.path by default)

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
        "id": "bluesky_live", "name": "Bluesky, in real time (Jetstream)",
        "coverage_utc": {"from": "live: every public post as it is published", "to": "now, about a second behind"},
        "tweets": "about 35-80 posts a second across the whole network", "languages": "all languages, from the post's own langs field",
        "columns": ["uri", "text", "langs", "created_at", "like_count", "repost_count", "reply_count", "handle"],
        "caveats": [
            "This is the source for anything happening now, and for a project that keeps running and watching.",
            "Replay can look back about a day, but a 15-minute look-back is what a preview scans in seconds; longer windows are scanned newest-first and may be partial.",
            "Engagement comes from the Bluesky AppView when a preview is built, so likes and reposts are current totals, not what the post had at the time.",
            "Bluesky is much smaller than the Twitter archive: a quiet 15 minutes does not mean a quiet topic. Widen the window before concluding anything.",
        ],
    },
    {
        "id": "twitter_firehose", "name": "X / Twitter firehose (live sample, last month)",
        "coverage_utc": {"from": "2026-08-17", "to": "2026-09-17"}, "tweets": 377_270_972, "languages": "all languages (Japanese is 26%, English 31%)",
        "columns": ["id", "body", "created_at", "lang", "like_count", "retweet_count", "views_count", "reply_to_status_id", "quoting_id"],
        "caveats": [
            "Days from 2026-09-01 hold only 0.8-4.8M tweets against 22-29M in August: the collection changed, not the world. Compare shares between days, never raw counts.",
            "The pipeline analyses only original posts: retweets, quotes and replies are dropped, so previews count originals too.",
            "A tweet appears once per engagement snapshot, so every count is over distinct tweet ids.",
        ],
    },
    {
        "id": "congress", "name": "US Congress and executive accounts, historical",
        "coverage_utc": {"from": "1999-11-29", "to": "2026-08-24"}, "tweets": 5_095_245, "languages": "not recorded",
        "columns": ["tweet_id", "text", "created_at", "chamber", "party", "state", "handle"],
        "caveats": ["No like or view counts at all, so nothing can be weighted or sorted by engagement.", "Not wired into preview_keywords in this demo."],
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


def now_ms() -> int:
    return int(time.time() * 1000)


def spec_hash(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write_json(path: Path, payload) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # a reader must never see half a confirmation
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)


def read_draft():
    path = session_dir() / "draft.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def drop_confirmations() -> None:
    for path in (session_dir() / "confirmations").glob("*.json"):
        path.unlink(missing_ok=True)


def one_line(value, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value))[:limit]


def whole(value, low: int, high: int, fallback: str = "?") -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else fallback


def render_window(source: str, window) -> str:
    """A live project looks back and then keeps collecting; an archive project has two fixed dates."""
    window = window if isinstance(window, dict) else {}
    if source == "bluesky_live" or window.get("mode") == "live":
        return (f"Window: live — look back {whole(window.get('lookback_hours'), 0, 24, '0')} h over what Bluesky already published,"
                f" then keep collecting for {whole(window.get('run_hours'), 1, 168, '?')} h")
    return f"Window (UTC, end exclusive): {one_line(window.get('from'), 20)} to {one_line(window.get('to'), 20)}"


def render_summary(spec) -> str:
    """The text next to the Confirm button is written here, from the draft — never by the model (DESIGN.md §10)."""
    if not isinstance(spec, dict):
        return "The draft is not an object."
    observation = spec.get("observation") if isinstance(spec.get("observation"), dict) else {}
    window = observation.get("window") if isinstance(observation.get("window"), dict) else {}
    source = one_line(observation.get("source") or spec.get("source") or "twitter_firehose", 40)
    keywords = spec.get("keywords") or (spec.get("filter") or {}).get("any_terms") or []
    categories = [c.get("name") if isinstance(c, dict) else c for c in (spec.get("categories") or spec.get("classification") or [])]
    rows = [
        f"Project: {one_line(spec.get('name') or 'untitled')}",
        f"Observe: {one_line(observation.get('intent') or spec.get('intent') or '(not stated)', 240)}",
        f"Source: {source}",
        render_window(source, {**window, "from": window.get("from") or spec.get("date_from"), "to": window.get("to") or spec.get("date_to")}),
        f"Language: {one_line(spec.get('language') or observation.get('language') or 'all', 40)}",
        f"Keywords ({len(keywords)}): {one_line(', '.join(one_line(k, 40) for k in keywords[:20]), 300)}",
        f"Categories ({len(categories)}): {one_line(', '.join(one_line(c, 40) for c in categories[:12]), 300)}",
        f"Sentiment question: {one_line(spec.get('sentiment_question') or (spec.get('sentiment') or {}).get('instructions') if isinstance(spec.get('sentiment'), dict) else spec.get('sentiment_question'), 240)}",
    ]
    return "\n".join(rows)


@mcp.tool()
def describe_sources() -> dict:
    """List the data sources this product can observe, their coverage, size and caveats.

    Call this FIRST, before saying anything about what data exists or proposing dates. There are two
    kinds: Bluesky live, which is happening right now and can keep running, and the historical
    archives, whose fixed coverage window and September collection change decide which dates are
    worth observing. Preview the live source with bluesky_recent, the archive with preview_keywords.
    """
    return {"sources": SOURCES, "preview_window_limit_days": MAX_WINDOW_DAYS, "timezone": "UTC",
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


def scan(keywords: list[str], low: str, high: str, language: str | None) -> dict:
    started = time.time()
    connection = duckdb.connect()  # connections are not thread-safe: this one is born and dies inside this thread
    timer = threading.Timer(SCAN_TIMEOUT_S, connection.interrupt)
    try:
        for setting in ("SET TimeZone='UTC'", "SET threads=4", "SET memory_limit='4GB'"):
            connection.execute(setting)
        files = files_for_window(connection, low, high)
        if not files:
            return fail("out_of_coverage", "No data files overlap that window.", "The firehose covers 2026-08-17 to 2026-09-17 UTC; call describe_sources.")
        arguments = {"files": files, "low": low, "high": high} | {f"k{index}": "(?i)" + REGEX_META.sub(r"\\\1", keyword) for index, keyword in enumerate(keywords)}
        matches = " OR ".join(f"regexp_matches(body, ${name})" for name in arguments if name.startswith("k"))
        if language:
            arguments["lang"] = language
        timer.start()
        connection.execute(f"""
            CREATE OR REPLACE TEMP TABLE matched AS
            SELECT id, body, created_at, lang, like_count, version
            FROM read_parquet($files)
            WHERE created_at >= $low::TIMESTAMPTZ AND created_at < $high::TIMESTAMPTZ
              AND ({matches})
              AND NOT starts_with(body, 'RT @')
              AND coalesce(reply_to_status_id, '') = '' AND coalesce(quoting_id, '') = ''
              {"AND lang = $lang" if language else ""}""", arguments)
        total = connection.execute("SELECT count(DISTINCT id) FROM matched").fetchone()[0]
        per_day = connection.execute(
            "SELECT strftime(created_at::DATE, '%Y-%m-%d') AS day, count(DISTINCT id) AS count FROM matched GROUP BY 1 ORDER BY 1").fetchall()
        examples = connection.execute("""
            SELECT id, strftime(created_at::DATE, '%Y-%m-%d'), like_count, lang, left(body, 240)
            FROM matched QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1
            ORDER BY like_count DESC NULLS LAST LIMIT 6""").fetchall()
    except duckdb.InterruptException:
        return fail("timeout", f"The scan passed {SCAN_TIMEOUT_S} seconds and was cancelled.", "Use a shorter window, one day, or fewer and narrower keywords.")
    except duckdb.Error as error:
        return fail("query_failed", one_line(error, 300), "Check the keywords and the dates, then try again.")
    finally:
        timer.cancel()
        connection.close()
    return {
        "keywords": keywords, "date_from": low[:10], "date_to": high[:10], "language": language, "exact": True,
        "files_scanned": len(files), "seconds": round(time.time() - started, 1), "total": total,
        "per_day": [{"day": day, "count": count} for day, count in per_day],
        "examples": [{"id": row[0], "day": row[1], "like_count": row[2], "lang": row[3], "body": row[4]} for row in examples],
        "counts": "distinct tweet ids, original posts only (no retweets, quotes or replies)",
    }


@mcp.tool()
def preview_keywords(keywords: list[str], date_from: str, date_to: str, language: str | None = None) -> dict:
    """Count how many real posts a set of keywords matches per day, with the six most-liked examples.

    Call this before finalizing a project and show the user the numbers and examples, asking whether
    that is what they meant — it is the only way either of you learns whether the keywords work.
    Dates are UTC ISO days, date_from inclusive, date_to exclusive, at most three days apart in this
    demo. A keyword matches case-insensitively anywhere in the post; only original posts are counted,
    because only originals are analysed. language is an exact code such as "en" or "ja", or null for all.
    """
    if not keywords or not isinstance(keywords, list) or any(not isinstance(k, str) or not 2 <= len(k.strip()) <= 80 for k in keywords):
        return fail("bad_keywords", "Give 1 to 20 keywords, each 2 to 80 characters.", "Try the words people actually type, for example a product name and a nickname for it.")
    if len(keywords) > 20:
        return fail("bad_keywords", "At most 20 keywords per preview.", "Preview the most promising ones first.")
    if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") for date in (date_from, date_to)):
        return fail("bad_dates", "Dates must be plain UTC days like 2026-09-09.", "date_from is inclusive, date_to exclusive.")
    low, high = f"{date_from}T00:00:00+00", f"{date_to}T00:00:00+00"
    days = (time.mktime(time.strptime(date_to, "%Y-%m-%d")) - time.mktime(time.strptime(date_from, "%Y-%m-%d"))) / 86400
    if not 0 < days <= MAX_WINDOW_DAYS:
        return fail("window_too_large", f"The window must be 1 to {MAX_WINDOW_DAYS} days ({days:.0f} requested).", "Preview the busiest three days; the project itself may still be wider.")
    with ThreadPoolExecutor(max_workers=1) as pool:  # keeps a 10-second scan off the MCP server's event loop
        return pool.submit(scan, [k.strip() for k in keywords], low, high, language).result()


def live_keywords(keywords) -> dict | None:
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20:
        return fail("bad_keywords", "Give 1 to 20 keywords.", "Two or three words people would actually post is usually enough.")
    if any(not isinstance(word, str) or not 2 <= len(word.strip()) <= 80 for word in keywords):
        return fail("bad_keywords", "Every keyword must be 2 to 80 characters.", "Bluesky matches whole words; write acronyms in capitals (AI, LLM) to keep them case-sensitive.")
    return None


def live_language(language) -> dict | None:
    if language is not None and not (isinstance(language, str) and LANGUAGE.fullmatch(language)):
        return fail("bad_language", "language must be a code like \"en\", \"ja\" or \"pt-BR\", or null for all languages.", "Leave it out unless the user asked for one language.")
    return None


def run_live(coroutine) -> dict:
    """MCP tools here are plain functions, so the event loop lives in a worker thread for the call."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@mcp.tool()
def bluesky_recent(keywords: list[str], minutes: int = 15, language: str | None = None) -> dict:
    """What happened in the last N minutes on Bluesky, right now: real posts, counted and quoted.

    This is the preview for the live source, and the tool to answer "what are people saying about X
    right now / today / at the moment". It replays every public Bluesky post of the last `minutes`
    (1 to 60, 15 is a good default) over parallel connections and keeps the ones matching a keyword,
    then asks the Bluesky AppView for each example's current likes and reposts.
    Keywords match whole words in the post text and in any link card; a keyword of five characters or
    fewer written in capitals (AI, LLM) matches case-sensitively, everything else ignores case.
    `covered_fraction` says how much of the window was really scanned before the time budget ran out —
    if it is below 1, every count is a floor. A quiet 15 minutes does not mean a quiet topic: widen to
    60 minutes before telling the user a subject is dead. language is a code like "en", or null for all.
    """
    problem = live_keywords(keywords) or live_language(language)
    if problem:
        return problem
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
        return fail("bad_window", "minutes must be a whole number from 1 to 60.", "Start at 15; widen to 60 if the topic looks quiet.")
    return run_live(bluesky.scan_recent([word.strip() for word in keywords], minutes=minutes, language=language, budget_s=LIVE_BUDGET_S))


@mcp.tool()
def bluesky_listen(keywords: list[str], seconds: int = 20, language: str | None = None) -> dict:
    """Watch the live Bluesky stream for a few seconds and report what went past, as it happens.

    Use this only for the "what is being said this very second" moment, or to show the user that the
    stream is genuinely live: it waits `seconds` (5 to 45, 20 is a good default) of real time and can
    only see what is published while it waits, so a rare topic will match nothing. For any real
    question about the present, including "right now" and "today", use bluesky_recent instead.
    Matching and the result shape are the same as bluesky_recent, with five-second buckets.
    """
    problem = live_keywords(keywords) or live_language(language)
    if problem:
        return problem
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 5 <= seconds <= 45:
        return fail("bad_window", "seconds must be a whole number from 5 to 45.", "20 seconds is enough to show the stream is live.")
    return run_live(bluesky.listen_live([word.strip() for word in keywords], seconds=seconds, language=language))


@mcp.tool()
def save_draft(spec_json: str) -> dict:
    """Store the draft project as JSON and show it to the user in the spec panel beside the chat.

    Call this as soon as you have something concrete, and again after every change the user asks for.
    Include: name, observation.intent, observation.source, observation.window, keywords, language,
    categories (each a name and a description of what belongs in it) and sentiment_question.
    The window depends on the source. For an archive source (twitter_firehose, congress) it is
    {"from": "2026-09-09", "to": "2026-09-12"}, UTC days with the end exclusive. For bluesky_live it
    is {"mode": "live", "lookback_hours": 0-24, "run_hours": 1-168}: how far back to replay first,
    then how long to keep collecting as posts appear.
    Saving cancels any confirmation the user has not pressed yet, so save before asking to confirm.
    """
    try:
        spec = json.loads(spec_json)
    except ValueError as error:
        return fail("bad_json", f"The draft is not valid JSON: {one_line(error, 200)}", "Send one JSON object as a string.")
    if not isinstance(spec, dict):
        return fail("bad_json", "The draft must be a JSON object.", "Wrap the fields in {…}.")
    drop_confirmations()
    write_json(session_dir() / "draft.json", spec)
    return {"spec": spec, "spec_hash": spec_hash(spec), "saved": True}


@mcp.tool()
def request_confirmation() -> dict:
    """Put a Confirm / Cancel card in front of the user, summarising the saved draft.

    Call this once the draft is saved and previewed and the user is happy. Then STOP and end your
    turn: only the button the user presses can approve the project, and you will be told about it in
    the next message. The summary shown is generated from the draft itself, so do not restate it.
    """
    draft = read_draft()
    if draft is None:
        return fail("no_draft", "There is no draft to confirm.", "Call save_draft first.")
    created = now_ms()
    record = {"confirmation_id": secrets.token_urlsafe(12), "spec_hash": spec_hash(draft), "created_ms": created,
              "expires_ms": created + TTL_MS, "decision": None, "decided_ms": None, "consumed": False}
    drop_confirmations()
    write_json(session_dir() / "confirmations" / f"{record['confirmation_id']}.json", record)
    return {"confirmation_id": record["confirmation_id"], "summary": render_summary(draft), "expires_ms": record["expires_ms"],
            "next": "Wait for the user. Do not call submit_project until you are told the button was pressed."}


@mcp.tool()
def submit_project() -> dict:
    """Submit the confirmed project to the pipeline. This is the only tool with a real side effect.

    Call it only after you have been told the user pressed Confirm, and never say a project was
    submitted unless this tool returned a project_id — it refuses unless the harness itself recorded
    an approval for exactly the draft that is saved now.
    """
    draft = read_draft()
    if draft is None:
        return fail("no_draft", "There is no draft to submit.", "Call save_draft first.")
    wanted, now = spec_hash(draft), now_ms()
    reasons = []
    for path in sorted((session_dir() / "confirmations").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["spec_hash"] != wanted:
            reasons.append("a confirmation exists but the draft changed after it")
        elif record["decision"] != "approved":
            reasons.append("declined by the user" if record["decision"] == "declined" else "the user has not pressed the button yet")
        elif record["consumed"]:
            reasons.append("that confirmation was already used to submit")
        elif record["expires_ms"] <= now:
            reasons.append("the confirmation expired")
        else:
            record["consumed"] = True
            write_json(path, record)
            project = {"project_id": f"proj_{secrets.token_hex(4)}", "status": "submitted", "spec_hash": wanted,
                       "confirmation_id": record["confirmation_id"], "submitted_ms": now, "spec": draft}
            write_json(session_dir() / "submitted.json", project)
            return {"project_id": project["project_id"], "status": "submitted",
                    "note": "demo: the real pipeline is owned by a teammate and is not connected yet"}
    return fail("not_confirmed", "; ".join(reasons) or "no confirmation has been requested for this draft",
                "Call save_draft, then request_confirmation, then wait for the user to press Confirm.")


if __name__ == "__main__":
    mcp.run()
