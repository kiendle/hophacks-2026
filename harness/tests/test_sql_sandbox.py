# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_sql_sandbox.py

The attack suite of DESIGN.md §7.4 against the real parquet files: every attack must be refused and
the next legitimate query must still work, so a refusal can never leave the sandbox poisoned.
Assertions lean on twitter-firehose only (nobody writes it); harness/data/prepared/ is another
package's output and may be half-written while this runs, so those views are checked loosely.
2 threads / 2 GB come from the sandbox itself; the widest window used here is one September day.

Cases are also layer-aware: the gated attacks never reach the locked connection, so layer 2 is
probed with the gate bypassed, and gutting layer 2 must make layer 5 report a leak.
"""
import json
import sys
import time
import traceback
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sql_sandbox as sandbox  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = sandbox.sandbox_temp().parent / "harness-sql-sandbox-tests"  # fixtures live in the system temp dir, never in the repo
CASES = []
WINDOW = ("WHERE created_at >= TIMESTAMPTZ '2026-09-09T00:00:00+00' "
          "AND created_at < TIMESTAMPTZ '2026-09-09T00:05:00+00'")  # five minutes: two files after pruning
CHEAP = f"SELECT count(DISTINCT id) AS ids FROM tweets {WINDOW}"
SEPT_DAY = (
    "SELECT count(DISTINCT id) AS ids FROM tweets "
    "WHERE created_at >= TIMESTAMPTZ '2026-09-09T00:00:00+00' "
    "AND created_at < TIMESTAMPTZ '2026-09-10T00:00:00+00' "
    "AND regexp_matches(body, '(?i)anthropic')"
)
BASELINE = []


def case(function):
    CASES.append(function)
    return function


def ask(sql, **kwargs):
    return sandbox.run_sql(sql, repo_root=ROOT, **kwargs)


def health() -> list:
    """The same cheap query before and after every attack: same rows means the machinery survived."""
    outcome = ask(CHEAP, timeout_s=30)
    assert "error" not in outcome, f"the healthy query failed: {str(outcome)[:200]}"
    if not BASELINE:
        BASELINE.append(outcome["rows"])
    assert outcome["rows"] == BASELINE[0], f"{outcome['rows']} != baseline {BASELINE[0]}"
    return outcome["rows"]


def refused(sql, **kwargs) -> str:
    """One attack: it must come back as a structured error, and the sandbox must still serve after it."""
    outcome = ask(sql, timeout_s=10, **kwargs)
    assert "error" in outcome, f"NOT REFUSED: {sql!r} -> {str(outcome)[:200]}"
    assert "rows" not in outcome, f"NOT REFUSED: {sql!r} returned rows"
    health()
    return outcome["error"]["code"]


def fixture() -> Path:
    """A tiny posts.parquet standing in for the pipeline's results, joinable against tweets."""
    ids = ask(f"SELECT DISTINCT id FROM tweets {WINDOW} ORDER BY id LIMIT 25", timeout_s=30)
    assert "error" not in ids and ids["row_count"] == 25, str(ids)[:200]
    RESULTS.mkdir(parents=True, exist_ok=True)
    posts = RESULTS / "posts.parquet"
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone='UTC'")
        con.execute("SET threads=2")
        con.execute("SET memory_limit='2GB'")
        kind = "VARCHAR" if isinstance(ids["rows"][0][0], str) else "BIGINT"
        con.execute(f"CREATE TABLE posts(post_id {kind}, category VARCHAR, sentiment DOUBLE, likes INTEGER)")
        con.executemany("INSERT INTO posts VALUES (?, 'climate', 0.25, 3)", [[row[0]] for row in ids["rows"]])
        con.execute(f"COPY posts TO '{str(posts).replace(chr(92), '/')}' (FORMAT parquet)")
    finally:
        con.close()
    return RESULTS


@case
def self_test_blocks_everything():
    results = sandbox.self_test(ROOT)
    leaked = [attack for attack, blocked in results if not blocked]
    assert not leaked, f"attacks got through: {leaked}"
    assert sandbox.enabled(ROOT) is True
    health()
    return f"{len(results)} attacks blocked"


@case
def locked_connection_is_locked():
    """Layer 2 itself. Every gated attack is refused before a connection exists, so probe it directly."""
    probes = sandbox.locked_probes(ROOT)
    leaked = [label for label, blocked in probes if not blocked]
    assert len(probes) >= 5, f"only {len(probes)} layer-2 probes ran: {probes}"
    assert not leaked, f"the locked connection allowed: {leaked}"
    views = sandbox.published_views(ROOT)
    dirs = []
    for view in views:
        if view["directory"].exists() and view["directory"] not in dirs:
            dirs.append(view["directory"])
    con = sandbox.connect(views, dirs)
    try:
        for path in (ROOT / ".gitignore", ROOT / "harness" / "state" / "logs" / "sql.jsonl"):
            try:
                rows = con.execute(f"SELECT content FROM read_text({sandbox.literal(path)})").fetchall()
                raise AssertionError(f"read {path.name} on the locked connection: {str(rows)[:80]}")
            except duckdb.Error as error:
                assert "Permission Error" in str(error), f"{path.name}: {str(error)[:200]}"
        allowed = con.execute(f"SELECT count(*) FROM glob({sandbox.literal(dirs[0] / '*')})").fetchone()[0]
        assert allowed > 0, "the locked connection cannot read the published data either"
    finally:
        con.close()
    return f"{len(probes)} probes blocked, {allowed} files readable inside {dirs[0].name}"


@case
def settings_readback_catches_an_open_connection():
    """`allowed_directories` without external access off restricts nothing (DESIGN.md 7.4)."""
    plain = duckdb.connect()
    try:
        plain.execute(f"SET allowed_directories=[{sandbox.literal(ROOT / 'twitter-firehose')}]")
        try:
            sandbox.verify(plain, [ROOT / "twitter-firehose"])
            raise AssertionError("verify accepted a connection that locks nothing")
        except sandbox.SandboxBroken as error:
            reported = str(error)
    finally:
        plain.close()
    for expected in ("TimeZone", "enable_external_access", "lock_configuration", "memory_limit", "temp_directory"):
        assert expected in reported, f"{expected} unchecked: {reported[:300]}"
    return reported[:80]


@case
def self_test_sees_layer_2_break():
    """The regression that shipped: with only the statement gate, every attack still 'passes'."""
    def unlocked(views, directories):
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        for view in views:
            try:
                con.execute(f"CREATE VIEW {view['name']} AS {view['sql']}")
            except duckdb.Error:
                pass
        return con

    real, leaked = sandbox.connect, []
    sandbox.connect = unlocked
    try:
        results = sandbox.self_test(ROOT)
        leaked = [label for label, blocked in results if not blocked]
        assert leaked, f"an unlocked connection passed all {len(results)} self-test cases"
        assert all(label.startswith("layer 2") for label in leaked), leaked
    finally:
        sandbox.connect = real
        sandbox._SELF_TEST_OK = None
    assert sandbox.enabled(ROOT) is True, "the real sandbox failed its own self-test"
    health()
    return f"{len(leaked)} leaks caught with layer 2 gutted"


@case
def read_windows_ini():
    return " ".join([refused("SELECT * FROM read_text('C:/Windows/win.ini')"),
                     refused("SELECT * FROM read_csv('C:/Windows/win.ini')"),
                     refused("SELECT * FROM 'C:/Windows/win.ini'")])


@case
def read_repo_file():
    return refused(f"SELECT content FROM read_text('{str(ROOT / '.gitignore').replace(chr(92), '/')}')")


@case
def glob_inside_allowed_directory():
    path = str(ROOT / "twitter-firehose" / "*").replace("\\", "/")
    return " ".join([refused(f"SELECT filename FROM read_text('{path}')"),
                     refused(f"SELECT file FROM glob('{path}')")])


@case
def network_read():
    return refused("SELECT * FROM read_csv('https://raw.githubusercontent.com/duckdb/duckdb/main/README.md')")


@case
def copy_into_allowed_directory():
    code = refused(f"COPY (SELECT 1) TO '{str(ROOT / 'twitter-firehose' / 'pwned.csv').replace(chr(92), '/')}'")
    assert not (ROOT / "twitter-firehose" / "pwned.csv").exists(), "COPY wrote a file"
    return code


@case
def configuration_changes():
    return " ".join([refused("SET enable_external_access=true"),
                     refused("INSTALL httpfs"),
                     refused("ATTACH 'evil.db' AS evil")])


@case
def pragmas_and_catalog():
    return " ".join([refused("PRAGMA database_list"),
                     refused("PRAGMA version"),
                     refused("SELECT * FROM duckdb_settings()"),
                     refused("SELECT * FROM duckdb_functions() LIMIT 1")])


@case
def query_table_function():
    return " ".join([refused("SELECT * FROM query('SELECT 1')"),
                     refused("SELECT * FROM query_table('tweets')"),
                     refused("SELECT * FROM range(3)")])


@case
def two_statements():
    return " ".join([refused("SELECT 1; SELECT 2"),
                     refused("SELECT count(*) FROM tweets; DROP VIEW tweets")])


@case
def hidden_behind_a_comment():
    return " ".join([refused("/* just looking */ PRAGMA database_list"),
                     refused("-- harmless\nPRAGMA version"),
                     refused("/* a */ -- b\n SELECT * FROM read_text('C:/Windows/win.ini')")])


@case
def environment_and_settings():
    return " ".join([refused("SELECT getenv('PATH')"),
                     refused("SELECT current_setting('allowed_directories')")])


@case
def cte_shadowing_a_view():
    return " ".join([refused("WITH tweets AS (SELECT * FROM read_text('C:/Windows/win.ini')) SELECT * FROM tweets"),
                     refused("WITH congress AS (SELECT * FROM read_parquet('C:/Windows/win.ini')) SELECT * FROM congress")])


@case
def ast_bypass_attempts():
    """A table function reached through a set operation, a bare FROM, a comma join or a schema name."""
    return " ".join([refused(f"SELECT id FROM tweets {WINDOW} UNION ALL SELECT content FROM read_text('C:/Windows/win.ini')"),
                     refused("FROM read_text('C:/Windows/win.ini')"),
                     refused(f"SELECT t.id FROM tweets t, read_text('C:/Windows/win.ini') w {WINDOW}"),
                     refused("SELECT table_name FROM information_schema.tables"),
                     refused("SELECT * FROM pg_catalog.pg_tables")])


@case
def private_state_is_unreachable():
    path = str(ROOT / "harness" / "state").replace("\\", "/")
    return " ".join([refused(f"SELECT * FROM read_text('{path}/logs/sql.jsonl')"),
                     refused(f"SELECT * FROM read_json('{path}/sessions/*/draft.json')")])


@case
def runaway_is_cancelled():
    started = time.monotonic()
    outcome = ask("SELECT count(*) FROM tweets a JOIN tweets b ON a.id = b.reply_to_status_id", timeout_s=3)
    elapsed = time.monotonic() - started
    assert outcome.get("error", {}).get("code") == "timeout", f"expected a timeout, got {str(outcome)[:200]}"
    assert elapsed < 12, f"cancelled only after {elapsed:.1f}s"
    after = ask(SEPT_DAY, timeout_s=30)
    assert after.get("rows") == [[5396]], f"sandbox broken after the runaway: {str(after)[:200]}"
    return f"cancelled after {elapsed:.1f}s, then 5396 ids in {after['seconds']}s"


@case
def recursive_cte_bomb_is_cancelled():
    started = time.monotonic()
    outcome = ask("WITH RECURSIVE bomb(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM bomb) SELECT count(*) FROM bomb", timeout_s=3)
    elapsed = time.monotonic() - started
    assert outcome.get("error", {}).get("code") == "timeout", f"expected a timeout, got {str(outcome)[:200]}"
    health()
    return f"cancelled after {elapsed:.1f}s"


@case
def september_day_count():
    outcome = ask(SEPT_DAY, timeout_s=30)
    assert "error" not in outcome, str(outcome)[:300]
    assert outcome["rows"] == [[5396]], f"expected 5396 distinct ids, got {outcome['rows']}"
    assert outcome["columns"] == ["ids"] and outcome["row_count"] == 1 and not outcome["truncated"]
    return f"5396 ids in {outcome['seconds']}s"


@case
def join_of_project_posts():
    outcome = ask(
        "SELECT p.category, count(DISTINCT t.id) AS posts, round(avg(p.sentiment), 2) AS mean_sentiment "
        f"FROM project_posts p JOIN tweets t ON t.id = p.post_id {WINDOW.replace('WHERE', 'AND').replace('created_at', 't.created_at')} "
        "GROUP BY 1",
        results_dir=str(fixture()), timeout_s=30,
    )
    assert "error" not in outcome, str(outcome)[:300]
    assert outcome["rows"] == [["climate", 25, 0.25]], f"unexpected join result: {outcome['rows']}"
    return f"joined 25 posts in {outcome['seconds']}s"


@case
def project_posts_hidden_without_results_dir():
    outcome = ask("SELECT count(*) FROM project_posts")
    assert outcome.get("error", {}).get("code") == "table_not_allowed", str(outcome)[:200]
    return outcome["error"]["code"]


@case
def cte_query_works():
    outcome = ask(
        "WITH per_hour AS ("
        "  SELECT date_trunc('hour', created_at) AS hour, count(DISTINCT id) AS ids FROM tweets"
        "  WHERE created_at >= TIMESTAMPTZ '2026-09-09T00:00:00+00' AND created_at < TIMESTAMPTZ '2026-09-09T03:00:00+00'"
        "    AND regexp_matches(body, '(?i)anthropic') GROUP BY 1)"
        " SELECT count(*) AS hours, sum(ids) AS ids FROM per_hour",
        timeout_s=30,
    )
    assert "error" not in outcome, str(outcome)[:300]
    assert outcome["rows"][0][0] == 3 and outcome["rows"][0][1] > 0, outcome["rows"]
    return f"3 hourly buckets, {outcome['rows'][0][1]} ids in {outcome['seconds']}s"


@case
def trailing_semicolon_and_schema_name():
    outcome = ask(CHEAP + ";", timeout_s=30)
    assert outcome.get("rows") == BASELINE[0], str(outcome)[:200]
    qualified = ask(CHEAP.replace("FROM tweets", "FROM main.tweets"), timeout_s=30)
    assert qualified.get("rows") == BASELINE[0], str(qualified)[:200]
    return "a trailing semicolon and main.tweets both run"


@case
def prepared_views_are_readable():
    """harness/data/prepared/ belongs to another package: whatever exists there must at least be queryable."""
    listed = [view["name"] for view in sandbox.list_views(ROOT)]
    reports = []
    for name, lower in (("congress", 5_000_000), ("sample", 1_000_000), ("daily_totals", 30)):
        if name not in listed:
            reports.append(f"{name}=absent")
            continue
        outcome = ask(f"SELECT count(*) AS n FROM {name}", timeout_s=30)
        assert "error" not in outcome, f"{name}: {str(outcome)[:200]}"
        assert outcome["rows"][0][0] >= lower, f"{name} has {outcome['rows'][0][0]} rows"
        reports.append(f"{name}={outcome['rows'][0][0]}")
    return " ".join(reports)


@case
def row_truncation():
    outcome = ask(f"SELECT id FROM tweets {WINDOW} LIMIT 500", max_rows=200, timeout_s=30)
    assert "error" not in outcome, str(outcome)[:300]
    assert outcome["row_count"] == 200 and outcome["truncated"] is True, (outcome["row_count"], outcome["truncated"])
    return "500 rows -> 200 rows, truncated"


@case
def cell_truncation():
    outcome = ask(f"SELECT body FROM tweets {WINDOW} AND length(body) > 120 LIMIT 3", max_cell_chars=40, timeout_s=30)
    assert "error" not in outcome, str(outcome)[:300]
    assert outcome["truncated"] is True and outcome["row_count"] == 3
    assert all(len(row[0]) == 40 for row in outcome["rows"]), [len(row[0]) for row in outcome["rows"]]
    return "cells cut to 40 chars"


@case
def timestamptz_results_come_back():
    """DuckDB needs pytz to build a Python datetime, so without it every time series is an error."""
    raw = ask(f"SELECT id, created_at FROM tweets {WINDOW} ORDER BY id LIMIT 2", timeout_s=30)
    assert "error" not in raw, str(raw)[:300]
    assert raw["row_count"] == 2, raw["row_count"]
    assert all(str(row[1]).startswith("2026-09-09 00:0") for row in raw["rows"]), raw["rows"]
    series = ask("SELECT date_trunc('day', created_at) AS day, count(DISTINCT id) AS ids FROM tweets "
                 "WHERE created_at >= TIMESTAMPTZ '2026-09-09T00:00:00+00' "
                 "AND created_at < TIMESTAMPTZ '2026-09-10T00:00:00+00' "
                 "AND regexp_matches(body, '(?i)anthropic') GROUP BY 1", timeout_s=60)
    assert "error" not in series, str(series)[:300]
    assert series["rows"] == [["2026-09-09 00:00:00+00:00", 5396]], series["rows"]
    for outcome in (raw, series):
        json.dumps(outcome, allow_nan=False)  # the tool contract is JSON: a datetime or a NaN would raise here
    if "sample" in [view["name"] for view in sandbox.list_views(ROOT)]:
        wide = ask("SELECT * FROM sample LIMIT 1", timeout_s=30)
        assert "error" not in wide and wide["row_count"] == 1, str(wide)[:200]
    return f"day bucket {series['rows'][0]} in {series['seconds']}s"


@case
def non_finite_floats_stay_json():
    outcome = ask("SELECT 'NaN'::DOUBLE AS nan, 'Infinity'::DOUBLE AS inf, -'Infinity'::DOUBLE AS ninf, "
                  "exp(1000) AS overflow, 1.5::DOUBLE AS finite, 1.5 AS decimal_literal, NULL AS nothing")
    assert "error" not in outcome, str(outcome)[:300]
    # a bare 1.5 is DECIMAL, which arrives as a Decimal and is stringified: json.dumps cannot encode one
    assert outcome["rows"] == [["nan", "inf", "-inf", "inf", 1.5, "1.5", None]], outcome["rows"]
    json.dumps(outcome, allow_nan=False)  # bare NaN/Infinity tokens are not RFC 8259 JSON
    return "nan, inf and -inf stringified"


@case
def wide_result_cap():
    """One row of 300 aliased columns, and column names the caller made 9,000 characters long."""
    sizes = []
    for count in (60, 300, 600):
        columns = ", ".join(f"body AS c{index}" for index in range(count))
        outcome = ask(f"SELECT {columns} FROM tweets {WINDOW} AND length(body) > 400 LIMIT 1", timeout_s=30)
        size = len(json.dumps(outcome, default=str))
        assert size <= sandbox.MAX_OUTPUT_CHARS, f"{count} columns -> {size} characters"
        if "error" in outcome:
            assert outcome["error"]["code"] == "output_too_wide", str(outcome)[:200]
        else:
            assert outcome["truncated"] is True and len(outcome["columns"]) == len(outcome["rows"][0]), str(outcome)[:200]
        sizes.append(f"{count}c={size}")
    alias = "z" * 9_000
    outcome = ask(f'SELECT count(DISTINCT id) AS "{alias}", 1 AS "{alias}b" FROM tweets {WINDOW}', timeout_s=30)
    size = len(json.dumps(outcome, default=str))
    assert size <= sandbox.MAX_OUTPUT_CHARS, f"two 9,000-character aliases -> {size} characters"
    assert "error" in outcome or outcome["truncated"] is True, str(outcome)[:200]
    budget = sandbox.OUTPUT_BUDGET
    try:  # one row of one narrow column always fits the shipped budget, so squeeze it to reach the backstop
        sandbox.OUTPUT_BUDGET = 50
        refusal = ask(f"SELECT body FROM tweets {WINDOW} LIMIT 1", max_cell_chars=80, timeout_s=30)
        assert refusal.get("error", {}).get("code") == "output_too_wide", str(refusal)[:200]
    finally:
        sandbox.OUTPUT_BUDGET = budget
    return " ".join(sizes + [f"aliases={size}", "budget floor refuses"])


@case
def total_output_cap():
    outcome = ask(f"SELECT body FROM tweets {WINDOW} AND length(body) > 400 LIMIT 200", max_cell_chars=500, timeout_s=30)
    assert "error" not in outcome, str(outcome)[:300]
    size = len(json.dumps(outcome, default=str))
    assert size <= sandbox.MAX_OUTPUT_CHARS, f"output is {size} characters"
    assert outcome["truncated"] is True and outcome["row_count"] < 200
    return f"{outcome['row_count']} rows, {size} characters"


@case
def views_are_described():
    listed = sandbox.list_views(ROOT, results_dir=str(RESULTS))
    names = [view["name"] for view in listed]
    assert names[0] == "tweets" and "project_posts" in names, names
    tweets = next(view for view in listed if view["name"] == "tweets")
    assert "always filter created_at" in tweets["note"].casefold(), tweets["note"][:120]
    assert "media" not in tweets["columns"] and "body" in tweets["columns"], tweets["columns"]
    assert all(view["note"] and view["columns"] for view in listed), names
    return ", ".join(names)


@case
def every_query_is_logged():
    path = ROOT / "harness" / "state" / "logs" / "sql.jsonl"
    before = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
    ask(CHEAP, timeout_s=30)
    ask("PRAGMA version")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) - before >= 2, f"{len(lines) - before} new log lines"
    assert '"outcome": "ok"' in lines[-2] and '"outcome": "refused"' in lines[-1], lines[-2:]
    return f"{len(lines)} audit lines"


def main() -> int:
    failures = 0
    for function in CASES:
        started = time.monotonic()
        try:
            detail = function() or ""
            print(f"PASS  {function.__name__}  {detail}  [{time.monotonic() - started:.1f}s]", flush=True)
        except Exception:
            failures += 1
            print(f"FAIL  {function.__name__}\n{traceback.format_exc().strip()}", flush=True)
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
