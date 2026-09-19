# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""DuckDB DataSource over the local parquet: describe, file pre-filter, two-tier preview.

    from datasource_duckdb import DuckDBSource
    source = DuckDBSource(repo_root)
    source.describe()
    source.preview(spec, timeout_s=60)

A successful preview carries `ok: True` and `spec_hash`; a failed one carries `ok: False`, an
`error` block and `request_spec_hash` (never `spec_hash`), so no reader can mistake a failure for a
preview that found nothing.

Everything here is synchronous and blocking; callers run it in a thread. Every connection sets
TimeZone='UTC' (this laptop defaults to America/New_York and a bare date literal silently shifts a
day window by 4 hours), caps memory and threads, and carries an interrupt timer.
See DESIGN.md 6.1, 6.4, 7.2.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb

from filter import COLUMNS, compile_filter

EXACT_TWEET_BUDGET = 30_000_000   # about one August day, or all of September
SAMPLE_SCALE = 100                # sample.parquet keeps hash(id) % 100 = 0
ROUGH_SAMPLE_HITS = 300           # below this the scaled estimate is not worth quoting tightly
THIN_MATCHES = 100
DOMINANT_TEXT_SHARE = 0.30
DOMINANT_TEXT_FLOOR = 5           # 1 of 1 identical posts is not a campaign
COLLECTION_CHANGE = dt.date(2026, 9, 1)
PARTIAL_DAY = dt.date(2026, 9, 17)

# lower-case, drop URLs and @mentions, collapse whitespace: two copies of a campaign template
# differing only in the link become one normalized text.
NORM_TEXT = ("trim(regexp_replace(regexp_replace(regexp_replace(lower({t}), 'https?://\\S+', ' ', 'g'),"
             " '@\\w+', ' ', 'g'), '\\s+', ' ', 'g'))")

FIREHOSE_COLUMNS = ["id", "author_id", "body", "created_at", "like_count", "reply_count", "retweet_count",
                    "quote_count", "views_count", "bookmarks_count", "lang", "source", "reply_to_status_id",
                    "reply_to_user_id", "conversation_id", "poll", "embed", "quoting_id", "added_at",
                    "media", "synced", "embedded", "version"]

CAVEATS = [
    "Collection changed on 2026-09-01: August days hold 22-29M tweets, September days 0.8-4.8M. Compare "
    "shares per 100k comparable tweets, never raw daily counts, across that boundary.",
    "Replies are under-collected (3% of rows), so reply-thread and conversation analysis is unreliable.",
    "2026-09-17 is a partial day: collection stops at 14:32 UTC, so its counts are not comparable.",
    "The congress file has no engagement and no language columns: no likes, views, retweets or lang, "
    "and no like-weighting or language filter is possible there. It also has no reply_to_status_id "
    "and no quoting_id, so replies and quote-tweets cannot be told apart from original posts.",
    "59% of firehose tweets are plain retweets; the pipeline only ever analyses original posts "
    "(no retweets, replies or quotes) - except on the congress file, where only 'RT @' retweets can "
    "be dropped, so its counts include an unknown share of replies and quotes.",
    "A tweet appears once per version snapshot (rows / distinct ids = 1.02 overall, 1.13 for posts "
    "with 10k+ views), so every count here is over distinct ids at their latest version.",
]


def hash_spec(spec):
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DuckDBSource:
    def __init__(self, repo_root, *, memory_limit="2GB", threads=4):
        self.root = Path(repo_root).resolve()
        self.memory_limit, self.threads = memory_limit, threads
        self.prepared = self.root / "harness" / "data" / "prepared"
        self.denominators_csv = self.root / "topic-analysis" / "daily-denominators.csv"
        self._ranges = self._rows = self._denoms = self._congress = None

    # ---------- connections ----------

    def _connect(self):
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        con.execute(f"SET memory_limit='{self.memory_limit}'")
        con.execute(f"SET threads={int(self.threads)}")
        # spill under harness/data/ (git-ignored), never into whatever the working directory is
        temp = self.root / "harness" / "data" / ".tmp"
        try:
            temp.mkdir(parents=True, exist_ok=True)
            con.execute("SET max_temp_directory_size='10GB'")
            con.execute(f"SET temp_directory='{str(temp).replace(chr(39), chr(39) * 2)}'")
        except OSError:
            pass
        return con

    @contextmanager
    def _interrupt_after(self, con, seconds):
        timer = threading.Timer(max(seconds, 0.001), con.interrupt)
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    # ---------- metadata ----------

    def _file_ranges(self):
        """[(path, min created_at, max created_at)] for every firehose file. 0.4 s cold, then cached."""
        if self._ranges is not None:
            return self._ranges
        names = sorted(str(p).replace("\\", "/") for p in (self.root / "twitter-firehose").glob("tweets-*.parquet"))
        cache = self.root / "harness" / "data" / ".cache" / "firehose_ranges.json"
        blob = None
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if blob.get("files") != names:
                blob = None
        except (OSError, ValueError):
            blob = None
        if blob is None:
            glob = str(self.root / "twitter-firehose" / "tweets-*.parquet").replace("\\", "/")
            con = self._connect()
            try:
                # DuckDB aggregates the stats as timestamps but hands them back as text: correct
                # chronological min/max without needing pytz to build Python tz-aware datetimes.
                rows = con.execute(
                    """SELECT replace(file_name, '\\', '/') AS path,
                              min(stats_min_value::TIMESTAMPTZ)::VARCHAR AS lo,
                              max(stats_max_value::TIMESTAMPTZ)::VARCHAR AS hi
                       FROM parquet_metadata($glob) WHERE path_in_schema = 'created_at'
                       GROUP BY 1 ORDER BY 1""", {"glob": glob}).fetchall()
                total = con.execute("SELECT sum(num_rows) FROM parquet_file_metadata($glob)", {"glob": glob}).fetchone()[0]
            finally:
                con.close()
            blob = {"files": names, "rows": int(total or 0), "ranges": [list(row) for row in rows]}
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(blob), encoding="utf-8")
            except OSError:
                pass
        self._rows = blob.get("rows")
        self._ranges = [(p, dt.datetime.fromisoformat(lo), dt.datetime.fromisoformat(hi)) for p, lo, hi in blob["ranges"]]
        return self._ranges

    def files_for_window(self, date_from, date_to):
        """Only the files whose created_at range overlaps [from, to). The predicate still stays in the
        query, because boundary files spill into neighbouring days."""
        lo = _utc_midnight(date_from)
        hi = _utc_midnight(date_to)
        return [path for path, flo, fhi in self._file_ranges() if fhi >= lo and flo < hi]

    def _denominators(self):
        """{(day, lang): {tweets, originals, partial_day}} from the already-computed daily denominators."""
        if self._denoms is None:
            table = {}
            with self.denominators_csv.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    table[(dt.date.fromisoformat(row["day"]), row["lang"])] = {
                        "tweets": int(row["tweets"]), "originals": int(row["originals"]),
                        "partial_day": row["partial_day"].strip().lower() == "true"}
            self._denoms = table
        return self._denoms

    def _congress_meta(self):
        if self._congress is None:
            con = self._connect()
            try:
                path = str(self.root / COLUMNS["congress"]["path"]).replace("\\", "/")
                lo, hi, rows, undated = con.execute(
                    """SELECT min(created_at), max(created_at), count(*),
                              count(*) FILTER (WHERE created_at IS NULL)
                       FROM read_parquet($path)""", {"path": path}).fetchone()
            finally:
                con.close()
            self._congress = {"from": str(lo.date()), "to": str(hi.date()), "rows": rows, "undated_rows": undated}
        return self._congress

    def describe(self):
        ranges = self._file_ranges()
        prepared = {name: (self.prepared / f"{name}.parquet").exists() for name in ("sample", "congress", "daily_totals")}
        return {
            "sources": [
                {"name": "twitter_firehose",
                 "coverage": {"from": str(min(lo for _, lo, _ in ranges).date()),
                              "to": str(max(hi for _, _, hi in ranges).date())},
                 "rows": self._rows, "distinct_tweets": 377_000_000, "files": len(ranges),
                 "id_column": "id", "text_column": "body", "time_column": "created_at (TIMESTAMPTZ, UTC)",
                 "columns": FIREHOSE_COLUMNS,
                 "has_language": True, "has_engagement": True, "versioned": True},
                {"name": "congress",
                 "coverage": {"from": self._congress_meta()["from"], "to": self._congress_meta()["to"]},
                 "rows": self._congress_meta()["rows"], "undated_rows": self._congress_meta()["undated_rows"],
                 "id_column": "tweet_id", "text_column": "text", "time_column": "created_at (naive TIMESTAMP)",
                 "columns": COLUMNS["congress"]["select"] + ["chamber_norm (prepared table only)"],
                 "has_language": False, "has_engagement": False, "versioned": False,
                 "facets": {"party": ["Democrat", "Republican", "Independent"],
                            "chamber": ["House", "Senate", "Executive"], "state": "two-letter codes"}}],
            "caveats": CAVEATS,
            "prepared_tables": prepared,
            "partial_days": sorted(str(day) for (day, lang), row in self._denominators().items()
                                   if lang == "<ALL>" and row["partial_day"]),
        }

    # ---------- preview ----------

    def preview(self, spec, timeout_s=60):
        started = time.monotonic()
        deadline = started + max(float(timeout_s), 0.0)
        spec_hash = hash_spec(spec)
        # preview runs before validate_project (DESIGN.md 10), so this is the first code to touch a
        # model-written draft: anything malformed must come back as a structured error, not a traceback.
        try:
            compiled = compile_filter(spec, originals_only=True)
            columns = compiled["columns"]
            source, warnings = columns["source"], []
            date_from = compiled["params"]["date_from"].date()
            date_to = compiled["params"]["date_to"].date()
            days = [date_from + dt.timedelta(days=i) for i in range((date_to - date_from).days)]
            languages = compiled["params"].get("langs", [])
        except ValueError as error:
            return _error("invalid_filter", str(error), "fix the filter block and preview again",
                          spec_hash, started)
        except (TypeError, AttributeError, KeyError, IndexError) as error:
            return _error("invalid_filter", f"the draft is malformed ({type(error).__name__}: {error})",
                          "check that every field has the shape the spec schema asks for, then preview again",
                          spec_hash, started)

        tier, files = self._pick_tier(source, days, date_from, date_to, warnings)
        if not files:
            ranges = self._file_ranges()
            return _error("empty_window", f"no data file overlaps {date_from} .. {date_to}",
                          f"the firehose covers {min(lo for _, lo, _ in ranges).date()} .. "
                          f"{max(hi for _, _, hi in ranges).date()}", spec_hash, started)
        if date_from < COLLECTION_CHANGE < date_to:
            warnings.append("the window crosses the 2026-09-01 collection change (August days are ~20x "
                            "bigger than September days): read the per-100k shares, not the raw counts")
        if date_from <= PARTIAL_DAY < date_to:
            warnings.append("the window includes 2026-09-17, a partial day (collection stops 14:32 UTC)")
        if source == "congress":
            warnings.append("congress has no engagement or language data: no like counts, no language mix, "
                            "and examples are ordered by date instead of likes")
            warnings.append("replies and quote-tweets cannot be identified in the congress file (it has no "
                            "reply_to_status_id and no quoting_id), so only 'RT @' retweets are dropped: "
                            "these counts include an unknown share of replies and quotes")

        con = self._connect()
        try:
            _check(deadline)
            with self._interrupt_after(con, deadline - time.monotonic()):
                self._materialize(con, tier, columns, files, compiled)
                _check(deadline)
                rows, hits = con.execute("SELECT count(*), count(DISTINCT id) FROM matched").fetchone()
                scale = SAMPLE_SCALE if tier == "sample" else 1
                total = hits * scale
                per_day = self._per_day(con, source, languages, scale)
                _check(deadline)
                language_mix = self._language_mix(con, rows, scale) if columns["has_lang"] and rows else None
                duplicate_text_share = None
                if tier == "exact":
                    duplicate_text_share = self._duplicates(con, rows, warnings)
                else:
                    warnings.append("duplicate-text share is only computed on exact scans: a 100-copy "
                                    "template leaves about one copy in a 1% sample, which understates "
                                    "duplication ~100x")
                    warnings.append("the examples come from the 1% sample, so the 'top_in_sample' ones are "
                                    "NOT the window's most-engaged posts (the real top post is typically "
                                    "~100x more engaged): never quote them as the biggest reaction. Ask for "
                                    "an exact preview of the busiest day to see the actual top posts.")
                examples = self._examples(con, columns, tier)
        except (duckdb.InterruptException, TimeoutError):
            return _error("timeout", f"the preview did not finish within {timeout_s}s",
                          "narrow the window, add terms, or let the sample tier estimate it",
                          spec_hash, started, tier=tier)
        except duckdb.Error as error:
            return _error("query_failed", str(error), "check the filter and the window", spec_hash,
                          started, tier=tier)
        finally:
            con.close()

        if tier == "sample":
            if hits == 0:
                warnings.append("the 1% sample found no match: that means fewer than about 300 matches in "
                                "this window, NOT zero. Ask for an exact count on the busiest days.")
            elif hits < ROUGH_SAMPLE_HITS:
                warnings.append(f"rough estimate: the 1% sample returned only {hits} hits, so the interval "
                                "is wide. An exact count of the busiest days is cheap.")
        if total < THIN_MATCHES:
            warnings.append(f"the estimate is under {THIN_MATCHES} matches: probably too few to analyse"
                            if tier == "sample" else
                            f"only {total} matches: too few to analyse. Widen the terms or the window.")
        return {
            "ok": True,
            "exact": tier == "exact",
            "tier": tier,
            "total": total,
            "interval95": _poisson_interval(hits, SAMPLE_SCALE) if tier == "sample" else None,
            "per_day": per_day,
            "denominator": self._denominator_name(source, languages),
            "language_mix": language_mix,
            "duplicate_text_share": duplicate_text_share,
            "examples": examples,
            "warnings": warnings,
            "seconds": round(time.monotonic() - started, 2),
            "spec_hash": spec_hash,
            "source": source,
            "window": {"from": str(date_from), "to": str(date_to)},
            "originals_only": compiled["originals_only"],  # "retweets_only" on congress: see the warning
            "files_scanned": len(files),
            "sample_hits": hits if tier == "sample" else None,
        }

    def _pick_tier(self, source, days, date_from, date_to, warnings):
        if source == "congress":
            return "exact", [str(self.root / COLUMNS["congress"]["path"]).replace("\\", "/")]
        denominators = self._denominators()
        window_tweets = sum(denominators.get((day, "<ALL>"), {}).get("tweets", 0) for day in days)
        if window_tweets <= EXACT_TWEET_BUDGET:
            return "exact", self.files_for_window(date_from, date_to)
        sample = self.prepared / "sample.parquet"
        if sample.exists():
            return "sample", [str(sample).replace("\\", "/")]
        warnings.append(f"the window holds about {window_tweets:,} tweets, too many to scan exactly, but "
                        "harness/data/prepared/sample.parquet does not exist yet: ran an exact scan under "
                        "the timeout instead. Run harness/prepare_data.py to build the 1% sample.")
        return "exact", self.files_for_window(date_from, date_to)

    def _materialize(self, con, tier, columns, files, compiled):
        """One scan: every preview field is then read from `matched`."""
        text, norm = columns["text"], NORM_TEXT.format(t=columns["text"])
        if columns["source"] == "congress":
            projection = (f"SELECT tweet_id AS id, created_at, NULL::VARCHAR AS lang, "
                          f"NULL::BIGINT AS like_count, text AS body, {norm} AS norm_text")
            dedupe = ""
        else:
            projection = f"SELECT id, created_at, lang, like_count, body, {norm} AS norm_text"
            # sample.parquet already holds the latest state per id
            dedupe = ("\nQUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, added_at DESC) = 1"
                      if tier == "exact" else "")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE matched AS
{projection}
FROM read_parquet($files)
WHERE {compiled['where']}{dedupe}""", {"files": files, **compiled["params"]})

    def _per_day(self, con, source, languages, scale):
        rows = con.execute("SELECT created_at::DATE AS day, count(DISTINCT id) AS matches "
                           "FROM matched GROUP BY 1 ORDER BY 1").fetchall()
        keys = self._denominator_keys(languages)
        denominators = self._denominators()
        out = []
        for day, matches in rows:
            count = matches * scale
            denominator = None
            if source != "congress":
                denominator = sum(denominators.get((day, key), {}).get("originals", 0) for key in keys)
            per_100k = round(count / denominator * 100_000, 2) if denominator else None
            out.append({"day": str(day), "count": count, "per_100k": per_100k, "denominator": denominator})
        return out

    def _denominator_keys(self, languages):
        return list(languages) if languages else ["<ALL>"]

    def _denominator_name(self, source, languages):
        if source == "congress":
            return "none: the congress file has no daily totals, so per_100k is null"
        return "originals, lang=" + "+".join(self._denominator_keys(languages))

    def _language_mix(self, con, rows, scale):
        """The top 12 languages. Shares are of all matches, so they need not add up to 1."""
        mix = con.execute("SELECT coalesce(lang, '(null)') AS lang, count(*) AS n FROM matched "
                          "GROUP BY 1 ORDER BY n DESC, lang LIMIT 12").fetchall()
        return [{"lang": lang, "count": n * scale, "share": round(n / rows, 4)} for lang, n in mix]

    def _duplicates(self, con, rows, warnings):
        """Share of matches that repeat another match's normalized text. Posts that normalize to nothing
        (only a link or a mention, 283 of 570k originals on 2026-09-09) are not evidence of duplication."""
        if not rows:
            return 0.0
        duplicated, top_n, top_text = con.execute(
            """SELECT coalesce(sum(n) FILTER (WHERE n > 1), 0), coalesce(max(n), 0), arg_max(norm_text, n)
               FROM (SELECT norm_text, count(*) AS n FROM matched WHERE norm_text <> '' GROUP BY 1)""").fetchone()
        if top_n >= DOMINANT_TEXT_FLOOR and top_n / rows > DOMINANT_TEXT_SHARE:
            warnings.append(f"{top_n / rows:.0%} of matches ({top_n} posts) share one normalized text: "
                            f"{(top_text or '')[:80]!r} - this looks like a manufactured campaign, not "
                            "organic reaction")
        return round(duplicated / rows, 4)

    def _examples(self, con, columns, tier):
        # On the sample tier the label carries the caveat: the top of a 1% sample is a mid-tier post,
        # and these examples are quoted to the user as evidence.
        suffix = "_in_sample" if tier == "sample" else ""
        order = "like_count DESC NULLS LAST, id" if columns["has_likes"] else "created_at DESC, id"
        fields = ("id, created_at::DATE AS day, lang, like_count, substr(body, 1, 280) AS body")
        top = [dict(zip(("id", "day", "lang", "like_count", "body"), row), pick="top" + suffix)
               for row in con.execute(f"SELECT {fields} FROM matched ORDER BY {order} LIMIT 5").fetchall()]
        if not top:
            return []
        # hash(id) instead of random(): the same spec previews the same examples twice
        random = [dict(zip(("id", "day", "lang", "like_count", "body"), row), pick="random" + suffix)
                  for row in con.execute(
                      f"SELECT {fields} FROM matched WHERE NOT list_contains($skip, id) "
                      "ORDER BY hash(id) LIMIT 5", {"skip": [row["id"] for row in top]}).fetchall()]
        for example in top + random:
            example["day"] = str(example["day"])
        return top + random


def _error(code, message, hint, spec_hash, started, tier=None):
    """A failed preview must never look like a preview that found nothing.

    The hash of the requested spec is reported as `request_spec_hash`, NOT as `spec_hash`: only a
    successful preview may carry `spec_hash`, because spec.py matches that field against the draft to
    decide the spec was really previewed (DESIGN.md 5.1) and the confirm/submit gate hangs off it.
    `ok: False` with `exact: None` and `total: None` says the same thing to any other reader."""
    return {"error": {"code": code, "message": message, "hint": hint},
            "ok": False, "exact": None, "total": None,
            "warnings": [f"the preview failed ({code}) and counted nothing: {message}"],
            "per_day": [], "examples": [], "request_spec_hash": spec_hash,
            "seconds": round(time.monotonic() - started, 2), "tier": tier}


def _check(deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("preview deadline exceeded")


def _poisson_interval(hits, scale):
    if hits == 0:
        return [0, 3 * scale]  # 95% upper bound when zero events are observed
    half = 1.96 * math.sqrt(hits)
    return [max(0, int(round((hits - half) * scale))), int(round((hits + half) * scale))]


def _utc_midnight(value):
    day = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
    return dt.datetime.combine(day, dt.time(), dt.timezone.utc)
