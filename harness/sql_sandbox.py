# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Read-only SQL escape hatch for the analysis agent (DESIGN.md §7.4, §11, verified on DuckDB 1.5.5).

The SQL is assumed hostile: it is written by a model that reads attacker-controlled tweets.
Five layers, in this order — statement gate on the byte-identical string that is executed, a
locked connection that reads its own settings back, output caps, an audit line, and a startup
self-test that disables the tool. pytz is a hard dependency: without it DuckDB cannot hand a
TIMESTAMPTZ column to Python, which is every time series over this corpus.

Attack suite: python -m uv run harness/tests/test_sql_sandbox.py
"""
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

MAX_SQL_CHARS = 20_000
MAX_OUTPUT_CHARS = 12_000  # the whole tool result, JSON-encoded, stays well inside the 3k-token cap
OUTPUT_BUDGET = MAX_OUTPUT_CHARS - 120  # room for row_count, truncated and seconds around the cells
MIN_CELL_CHARS = 40
MEMORY_LIMIT = "2GB"
TEMP_LIMIT = "2GB"
THREADS = 2

TWEET_COLUMNS = (
    "id", "author_id", "body", "created_at", "like_count", "reply_count", "retweet_count",
    "quote_count", "views_count", "bookmarks_count", "lang", "source", "reply_to_status_id",
    "reply_to_user_id", "conversation_id", "quoting_id", "version", "added_at",
)  # never media/embed/poll: blobs blow the output cap and carry nothing an analysis needs
CONGRESS_COLUMNS = (
    "tweet_id", "author_handle", "author_name", "party", "chamber", "state", "created_at",
    "text", "topic", "source_corpus",
)

STARTS_OK = re.compile(r"(?is)\A(select|with|from)\b")
DOLLAR_TAG = re.compile(r"\$([A-Za-z_]\w*)?\$")
BANNED_FUNCTIONS = {"getenv", "current_setting"}
BANNED_FUNCTION_PREFIXES = ("pragma_", "duckdb_", "sqlite_")
TABLEREF_OK = {"BASE_TABLE", "SUBQUERY", "JOIN", "EXPRESSION_LIST", "EMPTY"}
TABLEREF_BANNED = {"TABLE_FUNCTION", "SHOW_REF", "PIVOT", "DELIM_GET", "COLUMN_DATA_REF", "BOUND_TABLE_REF"}
CATALOGS_OK = {"", "memory", "temp"}
SCHEMAS_OK = {"", "main"}
BYTE_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3,
              "tib": 1024**4, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}
SIZE = re.compile(r"\s*([0-9.]+)\s*([A-Za-z]*)\s*")

_LOG_LOCK = threading.Lock()
_SELF_TEST_OK = None


class SandboxBroken(RuntimeError):
    """A locked connection came back with a setting that is not the one that was set: serve nothing."""


def repo_root_of(repo_root=None) -> Path:
    return Path(repo_root or os.environ.get("HARNESS_REPO") or Path(__file__).resolve().parent.parent)


def fail(code: str, message: str, hint: str) -> dict:
    return {"error": {"code": code, "message": message, "hint": hint}}


def literal(value) -> str:
    """Only our own paths and identifiers go through here; user text is never concatenated into SQL."""
    return "'" + str(value).replace("\\", "/").replace("'", "''") + "'"


def sandbox_temp() -> Path:
    directory = Path(tempfile.gettempdir()) / "harness-sql-sandbox"  # outside the repo: spills must never land in it
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def as_bytes(text):
    """'1.8 GiB' -> 1932735283.2; DuckDB reports back a rounded, re-unitted form of what was set."""
    match = SIZE.fullmatch(str(text))
    unit = BYTE_UNITS.get(match.group(2).casefold()) if match else None
    return float(match.group(1)) * unit if unit else None


def norm_dir(path) -> str:
    """Windows hands out short 8.3 paths (KAANER~1) where DuckDB reports the expanded ones."""
    try:
        text = str(Path(path).resolve())
    except OSError:
        text = str(path)
    return text.replace("\\", "/").rstrip("/").casefold()


def strip_comments(sql: str) -> str:
    """Comments cannot hide the statement kind: `/* nice */ PRAGMA version` must read as PRAGMA."""
    out, i, n = [], 0, len(sql)
    while i < n:
        char = sql[i]
        if char in "'\"":
            j = i + 1
            while j < n:
                if sql[j] != char:
                    j += 1
                elif j + 1 < n and sql[j + 1] == char:
                    j += 2
                else:
                    j += 1
                    break
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(" ")
            i = j
        elif char == "$" and DOLLAR_TAG.match(sql, i):
            tag = DOLLAR_TAG.match(sql, i).group(0)
            end = sql.find(tag, i + len(tag))
            j = n if end < 0 else end + len(tag)
            out.append(sql[i:j])
            i = j
        else:
            out.append(char)
            i += 1
    return "".join(out)


def published_views(root: Path, results_dir=None) -> list[dict]:
    """Only views whose files exist are published; a name that is not published is not queryable."""
    prepared = root / "harness" / "data" / "prepared"
    views = []
    if any((root / "twitter-firehose").glob("tweets-*.parquet")):
        views.append({
            "name": "tweets",
            "sql": f"SELECT {', '.join(TWEET_COLUMNS)} FROM read_parquet({literal(root / 'twitter-firehose' / 'tweets-*.parquet')})",
            "columns": list(TWEET_COLUMNS),
            "directory": root / "twitter-firehose",
            "note": (
                "Raw firehose, 395M rows / 377M tweets, 2026-08-17..2026-09-17. ALWAYS FILTER created_at: "
                "bind UTC bounds as TIMESTAMPTZ '2026-09-09T00:00:00+00' (upper bound exclusive), which "
                "prunes whole files by parquet statistics. A September day scans in ~1 s, an August day in "
                "9-12 s, the whole month in 139 s and will be cancelled. A tweet appears once per version "
                "snapshot: count with count(DISTINCT id) and take rows with QUALIFY row_number() OVER "
                "(PARTITION BY id ORDER BY version DESC, added_at DESC) = 1. Match text with "
                "regexp_matches(body, '(?i)term'), never ILIKE. Original posts only: NOT starts_with(body, "
                "'RT @') AND coalesce(reply_to_status_id, '') = '' AND coalesce(quoting_id, '') = ''. "
                "August days hold 22-29M tweets against 0.8-4.8M in September, so compare shares, not counts."
            ),
        })
    congress_file = prepared / "congress.parquet"
    prepared_congress = congress_file.exists()
    if not prepared_congress:
        congress_file = root / "congress-tweets" / "congress-tweets-unified.parquet"
    if congress_file.exists():
        projection = "*" if prepared_congress else ", ".join(CONGRESS_COLUMNS)
        views.append({
            "name": "congress",
            "sql": f"SELECT {projection} FROM read_parquet({literal(congress_file)})",
            "columns": None if prepared_congress else list(CONGRESS_COLUMNS),
            "directory": congress_file.parent,
            "note": (
                "US Congress and executive tweets, 1999-11-29..2026-08-24, about 5.1M rows. The text column "
                "is `text`, not `body`; created_at is a naive TIMESTAMP so do not compare it to a "
                "TIMESTAMPTZ; tweet_id is already unique, so no dedupe; there is no lang and no engagement "
                "column. chamber has 31 spellings: House and representative mean House, Senate and senator "
                "mean Senate, every other non-null value is Executive"
                + (", so group by chamber_norm when the column list above shows it (this is the prepared "
                   "copy: rows with a NULL created_at are already dropped)." if prepared_congress
                   else " (the raw file, 5,095,245 rows, 28 of them with a NULL created_at).")
            ),
        })
    if (prepared / "sample.parquet").exists():
        views.append({
            "name": "sample",
            "sql": f"SELECT * FROM read_parquet({literal(prepared / 'sample.parquet')})",
            "columns": None,
            "directory": prepared,
            "note": (
                "1% of tweet ids (hash(id) % 100 = 0), latest version only, ~3.6M rows. Month-wide estimates "
                "in ~1 s: scale counts by 100 and report the 95% Poisson interval (x +- 1.96*sqrt(x)) * 100. "
                "Under ~300 sample hits the estimate is rough; duplicate-text share is understated ~100x."
            ),
        })
    if (prepared / "daily_totals.parquet").exists():
        views.append({
            "name": "daily_totals",
            "sql": f"SELECT * FROM read_parquet({literal(prepared / 'daily_totals.parquet')})",
            "columns": None,
            "directory": prepared,
            "note": (
                "Denominators per UTC day and per day x lang ('<ALL>' is every language): tweets and "
                "originals are distinct-id counts. The denominator must match the filter (originals when "
                "retweets are excluded, tweets otherwise) or the share wobbles 10-35% for no reason. "
                "partial_day marks days the collection does not cover fully."
            ),
        })
    if results_dir and (Path(results_dir) / "posts.parquet").exists():
        views.append({
            "name": "project_posts",
            "sql": f"SELECT * FROM read_parquet({literal(Path(results_dir) / 'posts.parquet')})",
            "columns": None,
            "directory": Path(results_dir),
            "note": "Scored posts of the current project as the pipeline wrote them: one row per post, already deduped and labelled.",
        })
    return views


def gate(sql: str, view_names: set[str]) -> dict | None:
    """Layer 1. Returns an error dict, or None when the statement may be executed unchanged."""
    if not isinstance(sql, str) or not sql.strip():
        return fail("empty_sql", "No SQL was given.", "Send one read-only SELECT statement.")
    if len(sql) > MAX_SQL_CHARS:
        return fail("sql_too_long", f"The statement is {len(sql)} characters, the limit is {MAX_SQL_CHARS}.",
                    "Aggregate in SQL instead of listing values inline.")
    try:
        statements = duckdb.extract_statements(sql)
    except Exception as error:
        return fail("parse_error", f"DuckDB cannot parse the statement: {error}".split("\n")[0], "Fix the syntax and send one statement.")
    if len(statements) != 1:
        return fail("one_statement_only", f"{len(statements)} statements were sent; exactly one is allowed.",
                    "Remove every ';' and everything after it.")
    if statements[0].type != duckdb.StatementType.SELECT:
        return fail("select_only", f"Statement type {statements[0].type.name} is not allowed.",
                    "Only a single read-only SELECT is allowed: no COPY, SET, PRAGMA, ATTACH, INSTALL or DDL.")
    head = strip_comments(sql).lstrip()
    if not STARTS_OK.match(head):
        return fail("select_only", f"The statement starts with {(head.split() or ['nothing'])[0][:20]!r}, not SELECT, WITH or FROM.",
                    "PRAGMA, CALL, SHOW and DESCRIBE report as SELECT to DuckDB and are refused here.")
    parser = duckdb.connect()
    try:
        serialized = parser.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    except Exception as error:
        return fail("parse_error", f"The statement cannot be checked: {error}".split("\n")[0], "Send a plain SELECT statement.")
    finally:
        parser.close()
    tree = json.loads(serialized)
    if tree.get("error") or len(tree.get("statements") or []) != 1:
        return fail("parse_error", f"The statement cannot be checked: {tree.get('error_message', 'unknown parse error')}".split("\n")[0],
                    "Only plain SELECT statements can be verified, so only they are allowed.")
    return walk(tree["statements"][0], {name.casefold() for name in view_names})


def walk(node, allowed_tables: set[str]) -> dict | None:
    """Layer 1c. Depth-first over the parse tree; CTE names widen the allow-list for their own scope only."""
    if isinstance(node, list):
        for child in node:
            verdict = walk(child, allowed_tables)
            if verdict:
                return verdict
        return None
    if not isinstance(node, dict):
        return None

    cte_map = node.get("cte_map")
    if isinstance(cte_map, dict) and cte_map.get("map"):
        names = {str(entry.get("key", "")).casefold() for entry in cte_map["map"] if isinstance(entry, dict)}
        allowed_tables = allowed_tables | names

    kind = node.get("type")
    kind = kind if isinstance(kind, str) else None  # a CONSTANT's "type" is a dict describing its data type
    if kind in TABLEREF_BANNED:
        if kind == "TABLE_FUNCTION":
            called = ((node.get("function") or {}).get("function_name")) or "a table function"
            return fail("table_function_not_allowed", f"Table function {called}() is not allowed.",
                        "Query the published views only; read_parquet, read_csv, read_text, query, glob and range are all refused.")
        return fail("table_not_allowed", f"Table reference {kind} is not allowed.",
                    "Only the published views and CTEs of the same statement may be read.")
    for key in ("from_table",) + (("left", "right") if kind == "JOIN" else ()):
        child = node.get(key) or {}
        child_kind = child.get("type") if isinstance(child, dict) else None
        if isinstance(child_kind, str) and child_kind not in TABLEREF_OK and child_kind not in TABLEREF_BANNED:
            return fail("table_not_allowed", f"Table reference {child_kind} is not allowed.",
                        "Only the published views and CTEs of the same statement may be read.")
    if "table_name" in node:
        if kind != "BASE_TABLE":
            return fail("table_not_allowed", f"Table reference {kind} is not allowed.", "Query the published views only.")
        name = str(node.get("table_name") or "")
        if name.casefold() not in allowed_tables:
            return fail("table_not_allowed", f"Table {name[:80]!r} is not a published view.",
                        "Call list_views for the queryable names; a file path is never a table name.")
        if str(node.get("catalog_name") or "").casefold() not in CATALOGS_OK or str(node.get("schema_name") or "").casefold() not in SCHEMAS_OK:
            return fail("table_not_allowed", "Qualified catalog or schema names are not allowed.", "Use the bare view name.")
    if node.get("class") == "FUNCTION" or kind == "FUNCTION":
        called = str(node.get("function_name") or "").casefold()
        if called in BANNED_FUNCTIONS or called.startswith(BANNED_FUNCTION_PREFIXES):
            return fail("function_not_allowed", f"Function {called}() is not allowed.",
                        "Settings, environment variables and catalog internals are not readable here.")

    for value in node.values():
        if isinstance(value, (dict, list)):
            verdict = walk(value, allowed_tables)
            if verdict:
                return verdict
    return None


SETTINGS_READ_BACK = ("TimeZone", "enable_external_access", "lock_configuration", "threads",
                      "memory_limit", "max_temp_directory_size", "temp_directory", "allowed_directories")


def verify(con, directories: list[Path]) -> None:
    """Layer 2b. Read the locked settings back, because every gated attack is refused before a
    connection exists: without this a connection that locks nothing looks perfectly healthy.
    `allowed_directories` set without external access off restricts nothing (DESIGN.md §7.4)."""
    got = dict(zip(SETTINGS_READ_BACK,
                   con.execute("SELECT " + ", ".join(f"current_setting('{name}')" for name in SETTINGS_READ_BACK)).fetchone()))
    listed = got["allowed_directories"]
    listed = listed if isinstance(listed, (list, tuple)) else re.findall(r"'([^']*)'", str(listed))
    asked = {norm_dir(directory) for directory in directories}
    allowed = {norm_dir(entry) for entry in listed}
    ceiling = as_bytes(MEMORY_LIMIT) * 1.05
    broken = []
    if str(got["TimeZone"]) != "UTC":
        broken.append(f"TimeZone={got['TimeZone']!r}")
    if str(got["enable_external_access"]).casefold() not in {"false", "0"}:
        broken.append(f"enable_external_access={got['enable_external_access']!r}")
    if str(got["lock_configuration"]).casefold() not in {"true", "1"}:
        broken.append(f"lock_configuration={got['lock_configuration']!r}")
    if str(got["threads"]) != str(THREADS):
        broken.append(f"threads={got['threads']!r}")
    for key in ("memory_limit", "max_temp_directory_size"):
        size = as_bytes(got[key])
        if not size or size > ceiling:
            broken.append(f"{key}={got[key]!r}")
    if norm_dir(got["temp_directory"]) != norm_dir(sandbox_temp()):
        broken.append(f"temp_directory={got['temp_directory']!r}")
    if asked - allowed or allowed - asked - {norm_dir(sandbox_temp())}:  # DuckDB adds temp_directory itself
        broken.append(f"allowed_directories missing={sorted(asked - allowed)} unexpected={sorted(allowed - asked - {norm_dir(sandbox_temp())})}")
    if broken:
        raise SandboxBroken("the connection is not locked: " + "; ".join(broken))


def connect(views: list[dict], directories: list[Path]):
    """Layer 2. The order is load-bearing: external access off before allowed_directories reads nothing,
    allowed_directories without external access off restricts nothing (both verified, DESIGN.md §7.4)."""
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"SET temp_directory={literal(sandbox_temp())}")
    con.execute(f"SET max_temp_directory_size='{TEMP_LIMIT}'")
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    con.execute(f"SET threads={THREADS}")
    for view in views:
        try:
            con.execute(f"CREATE VIEW {view['name']} AS {view['sql']}")
        except duckdb.Error:
            pass  # a prepared file with an unexpected schema drops that one view, never the sandbox
    con.execute("SET allowed_directories=[" + ", ".join(literal(directory) for directory in directories) + "]")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    try:
        verify(con, directories)
    except (duckdb.Error, SandboxBroken):
        con.close()
        raise
    return con


def cell(value, limit: int):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # NaN and +/-Infinity are not JSON: a strict parser rejects the bare tokens
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:limit] if len(text) > limit else text


def render(fetched, limit: int) -> tuple[list[list], bool]:
    rows, cut = [], False
    for row in fetched:
        out = []
        for value in row:
            shrunk = cell(value, limit)
            cut = cut or (isinstance(shrunk, str) and len(shrunk) == limit and len(str(value)) > limit)
            out.append(shrunk)
        rows.append(out)
    return rows, cut


def payload_size(names: list[str], rows: list[list]) -> int:
    return len(json.dumps({"columns": names, "rows": rows}, default=str))


def shrink(columns, fetched, max_rows: int, max_cell_chars: int) -> tuple[list[str], list[list], bool, int]:
    """Layer 3. Rows, then cell width, then trailing columns, until the payload is inside the cap.
    Column names are cut too: they are aliases the caller chose, so up to MAX_SQL_CHARS of them."""
    names = [str(cell(name, max_cell_chars)) for name in columns]
    truncated = len(fetched) > max_rows or any(len(str(name)) > max_cell_chars for name in columns)
    limit = max_cell_chars
    rows, cut = render(fetched[:max_rows], limit)
    truncated = truncated or cut
    size = payload_size(names, rows)
    while rows and size > OUTPUT_BUDGET:
        truncated = True
        if len(rows) > 1:
            rows.pop()
        elif limit > MIN_CELL_CHARS:
            limit = max(MIN_CELL_CHARS, limit // 2)
            rows, _ = render(fetched[:1], limit)
        elif len(names) > 1:
            drop = min(len(names) - 1, max(1, (size - OUTPUT_BUDGET) // max(1, size // len(names)) + 1))
            del names[len(names) - drop:]
            rows = [row[:len(names)] for row in rows]
        else:
            break
        size = payload_size(names, rows)
    return names, rows, truncated, size


def log(root: Path, record: dict) -> None:
    path = root / "harness" / "state" / "logs" / "sql.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds")} | record, default=str) + "\n"
        with _LOG_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass  # an unwritable log never blocks an answer


def execute(sql, *, repo_root=None, results_dir=None, timeout_s=30, max_rows=200, max_cell_chars=500) -> dict:
    root = repo_root_of(repo_root)
    timeout_s = min(120.0, max(1.0, float(timeout_s)))  # a caller can tighten the caps, never lift them
    max_rows = min(1_000, max(1, int(max_rows)))
    max_cell_chars = min(2_000, max(20, int(max_cell_chars)))
    views = published_views(root, results_dir)
    names = {view["name"] for view in views}
    record = {"sql": str(sql)[:4_000], "views": sorted(names), "timeout_s": timeout_s}

    verdict = gate(sql, names)
    if verdict:
        log(root, record | {"outcome": "refused", "code": verdict["error"]["code"], "seconds": 0.0})
        return verdict

    directories = []
    for directory in [view["directory"] for view in views] + ([Path(results_dir)] if results_dir else []):
        if directory.exists() and directory not in directories:
            directories.append(directory)
    started = time.monotonic()
    try:
        con = connect(views, directories)
    except (duckdb.Error, SandboxBroken) as error:
        log(root, record | {"outcome": "error", "code": "sandbox_error", "seconds": round(time.monotonic() - started, 3)})
        return fail("sandbox_error", f"The locked connection could not be built: {error}".split("\n")[0][:300],
                    "This is a harness fault, not a bad query; the packaged analysis tools still work.")
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        con.execute(sql)
        columns = [description[0] for description in (con.description or [])]
        fetched = con.fetchmany(max_rows + 1)
    except duckdb.InterruptException:
        seconds = round(time.monotonic() - started, 3)
        log(root, record | {"outcome": "timeout", "code": "timeout", "seconds": seconds})
        return fail("timeout", f"The query was cancelled after {timeout_s:g} s.",
                    "Narrow the created_at window, count(DISTINCT id) instead of listing rows, or query the sample view.")
    except duckdb.Error as error:
        seconds = round(time.monotonic() - started, 3)
        log(root, record | {"outcome": "error", "code": "query_error", "seconds": seconds})
        return fail("query_error", str(error).split("\n")[0][:400], "Check the column names against list_views and try again.")
    finally:
        timer.cancel()
        con.close()

    seconds = round(time.monotonic() - started, 3)
    names, rows, truncated, size = shrink(columns, fetched, max_rows, max_cell_chars)
    if size > OUTPUT_BUDGET:
        log(root, record | {"outcome": "error", "code": "output_too_wide", "seconds": seconds})
        return fail("output_too_wide", f"One row of this result is {size} characters, the limit is {MAX_OUTPUT_CHARS}.",
                    "Select fewer or narrower columns, or aggregate instead of listing values.")
    log(root, record | {"outcome": "ok", "code": None, "seconds": seconds, "rows": len(rows), "truncated": truncated})
    return {"columns": names, "rows": rows, "row_count": len(rows), "truncated": truncated, "seconds": seconds}


def run_sql(sql, *, repo_root=None, results_dir=None, timeout_s=30, max_rows=200, max_cell_chars=500) -> dict:
    """The agent-facing entry point: one read-only SELECT over the published views, or a structured error."""
    if not enabled(repo_root):
        return fail("sandbox_disabled", "The SQL sandbox failed its own attack suite and is disabled.",
                    "Use the packaged analysis tools; run harness/tests/test_sql_sandbox.py to see which attack got through.")
    return execute(sql, repo_root=repo_root, results_dir=results_dir, timeout_s=timeout_s,
                   max_rows=max_rows, max_cell_chars=max_cell_chars)


def list_views(repo_root=None, results_dir=None) -> list[dict]:
    """Name, columns and caveats of everything run_sql can read — this is the agent-facing tool description."""
    root = repo_root_of(repo_root)
    views = published_views(root, results_dir)
    directories = [view["directory"] for view in views if view["directory"].exists()]
    con = connect(views, directories)
    try:
        listed = []
        for view in views:
            columns = view["columns"]
            if not columns:
                try:
                    con.execute(f"SELECT * FROM {view['name']} LIMIT 0")
                    columns = [description[0] for description in con.description]
                except duckdb.Error:
                    continue
            listed.append({"name": view["name"], "columns": columns, "note": view["note"]})
        return listed
    finally:
        con.close()


def attacks(root: Path) -> list[str]:
    allowed = root / "twitter-firehose"
    return [
        f"SELECT content FROM read_text({literal(root / '.gitignore')})",
        "SELECT * FROM read_text('C:/Windows/win.ini')",
        "SELECT * FROM read_csv('C:/Windows/win.ini')",
        f"SELECT filename FROM read_text({literal(allowed / '*')})",
        "SELECT * FROM read_csv('https://example.com/a.csv')",
        f"COPY (SELECT 1) TO {literal(allowed / 'pwned.csv')}",
        "SET enable_external_access=true",
        "PRAGMA database_list",
        "PRAGMA version",
        "SELECT * FROM query('SELECT 1')",
        "INSTALL httpfs",
        "ATTACH 'evil.db' AS evil",
        "SELECT 1; SELECT 2",
        "/* just looking */ PRAGMA database_list",
        "SELECT getenv('PATH')",
        "SELECT * FROM duckdb_settings()",
        "WITH tweets AS (SELECT * FROM read_text('C:/Windows/win.ini')) SELECT * FROM tweets",
        "SELECT * FROM 'C:/Windows/win.ini'",
    ]


def locked_probes(root: Path) -> list[tuple[str, bool]]:
    """Layer 5b. The statement gate is bypassed on purpose. Every attack above is refused before a
    connection is built, so without these probes nothing in layer 5 ever tests layer 2 at all."""
    views = published_views(root)
    directories = []
    for view in views:
        if view["directory"].exists() and view["directory"] not in directories:
            directories.append(view["directory"])
    system_file = Path(os.environ.get("SystemRoot", "C:/Windows")) / "win.ini" if os.name == "nt" else Path("/etc/hosts")
    probes = [
        f"SELECT content FROM read_text({literal(root / '.gitignore')})",
        f"SELECT content FROM read_text({literal(root / 'harness' / 'state' / 'logs' / 'sql.jsonl')})",
        f"SELECT content FROM read_text({literal(system_file)})",
        "SELECT * FROM read_csv('https://example.com/a.csv')",
        f"SELECT file FROM glob({literal(root / '*')})",
    ]
    try:
        con = connect(views, directories)
    except (duckdb.Error, SandboxBroken) as error:
        return [(f"no locked connection: {str(error).split(chr(10))[0][:200]}", True)]  # serves nothing, so leaks nothing
    try:
        results = []
        for probe in probes:
            try:
                con.execute(probe).fetchmany(1)
                results.append((f"layer 2, gate bypassed: {probe[:100]}", False))
            except duckdb.Error as error:
                results.append((f"layer 2, gate bypassed: {probe[:100]}", "Permission Error" in str(error)))
        if directories:  # canary: the reverse misorder locks everything out, and then the tool is a liar, not a sandbox
            try:
                con.execute(f"SELECT count(*) FROM glob({literal(directories[0] / '*')})").fetchone()
            except duckdb.Error as error:
                results.append((f"layer 2 canary, the published data is unreadable: {str(error).split(chr(10))[0][:140]}", False))
        return results
    finally:
        con.close()


def self_test(repo_root=None) -> list[tuple[str, bool]]:
    """Layer 5. Every attack must come back as an error; anything that returns rows disables the tool."""
    root = repo_root_of(repo_root)
    results = []
    for attack in attacks(root):
        outcome = execute(attack, repo_root=root, timeout_s=5, max_rows=1, max_cell_chars=80)
        results.append((attack, "error" in outcome))
    return results + locked_probes(root)


def enabled(repo_root=None) -> bool:
    global _SELF_TEST_OK
    if _SELF_TEST_OK is None:
        results = self_test(repo_root)
        _SELF_TEST_OK = all(blocked for _, blocked in results)
        log(repo_root_of(repo_root), {"outcome": "self_test", "passed": _SELF_TEST_OK,
                                      "leaked": [attack for attack, blocked in results if not blocked]})
    return _SELF_TEST_OK


if __name__ == "__main__":
    for view in list_views():
        print(f"{view['name']}({', '.join(view['columns'])})\n  {view['note'][:160]}\n")
    results = self_test()
    print(f"self-test: {sum(1 for _, blocked in results if blocked)}/{len(results)} attacks blocked")
    for attack, blocked in results:
        if not blocked:
            print(f"  LEAKED: {attack}")
