# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""The Pipeline port (DESIGN.md 0, 6.3, 7.3, 7.3.1).

A teammate owns the real pipeline: it fetches posts, keyword-matches them, sends only
ORIGINAL posts to Jev, and writes the results into a database. The harness only starts a
run and reads results, so everything it needs sits behind the five methods below.

Until that contract exists, FakePipeline serves the one real finished project under
harness/demo/projects/ and pretends a freshly submitted spec is that project, so the whole
conversation - submit, wait, analyse - can be demoed. It never writes into harness/demo/.
"""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Protocol

import duckdb

DEMO_PROJECT_ID = "demo-playstation"
MAX_POSTS_PER_PAGE = 50
BODY_CHARS = 280
QUEUED_S = 3.0
RUNNING_S = 20.0  # READY from here on, so a demo waits 20 s, not 15 minutes
LOW_CONFIDENCE_NOUL = 0.2  # a noul has no confidence, so "unsure" means p is near 0.5 (DESIGN.md 8)
QUERY_TIMEOUT_S = 15
SORTS = ("engagement", "random", "most_negative", "most_positive", "low_confidence")
STATUSES = ("QUEUED", "RUNNING", "READY", "FAILED")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # the same shape spec.py enforces on a window bound


class PipelineError(Exception):
    """A structured failure the tool layer can hand to the model verbatim."""

    def __init__(self, code: str, message: str, hint: str):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


class Pipeline(Protocol):
    async def submit(self, spec: dict, idempotency_key: str) -> dict: ...
    async def status(self, project_id: str) -> dict: ...
    async def list_charts(self, project_id: str) -> list[dict]: ...
    async def chart(self, project_id: str, chart_id: str) -> dict: ...
    async def posts(self, project_id: str, *, date_from: str | None = None, date_to: str | None = None,
                    question: str | None = None, label: str | None = None, sort: str = "engagement",
                    limit: int | None = MAX_POSTS_PER_PAGE) -> list[dict]: ...


def _connect() -> duckdb.DuckDBPyConnection:
    """Connections are not thread-safe, so one is born and dies inside the worker thread."""
    connection = duckdb.connect()
    for setting in ("SET TimeZone='UTC'", "SET threads=2", "SET memory_limit='2GB'"):
        connection.execute(setting)
    return connection


def _day(value, field: str) -> str | None:
    """Model input, so a typo must come back as a hint it can act on, not a DuckDB stack trace."""
    if value is None or value == "":
        return None
    text = str(value)
    hint = f"Pass {field} as a UTC calendar day, 'YYYY-MM-DD', such as '2026-09-10'; leave it out for the whole window."
    if not DATE_RE.match(text):
        raise PipelineError("bad_request", f"{field} is {value!r}, which is not a date.", hint)
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise PipelineError("bad_request", f"{field} is {value!r}: {error}.", hint) from error
    return text


def _page_size(value) -> int:
    if value is None:  # an unset limit is the page size, not an error
        return MAX_POSTS_PER_PAGE
    try:
        wanted = int(value)
    except (TypeError, ValueError) as error:
        raise PipelineError("bad_request", f"limit is {value!r}, which is not a whole number.",
                            f"Pass an integer from 1 to {MAX_POSTS_PER_PAGE}, or leave it out.") from error
    return max(1, min(wanted, MAX_POSTS_PER_PAGE))  # a page size, not a budget: the agent pages by calling again


def _stored_noul(spec: dict, nouls: list[str]) -> str | None:
    """Which noul's yes-probability posts.parquet keeps in relevant_p: the gated one, under any name."""
    gate = (spec.get("relevance_gate") or {}).get("question")
    if gate in nouls:
        return gate
    return nouls[0] if len(nouls) == 1 else None


def _read_json(path: Path, code: str, hint: str) -> dict:
    if not path.exists():
        raise PipelineError(code, f"{path.name} is missing under {path.parent.name}/.", hint)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PipelineError(code, f"{path.name} is not valid JSON ({error}).", hint) from error


class FakePipeline:
    """Reads one canned project; maps every submitted spec onto it and fakes the wait."""

    def __init__(self, projects_dir, state_dir, clock=time.time, project_id: str = DEMO_PROJECT_ID):
        self.projects = Path(projects_dir)
        self.state = Path(state_dir)
        self.clock = clock
        self.project_id = project_id

    # --- files -----------------------------------------------------------------

    def _dir(self, project_id: str) -> Path:
        if project_id != self.project_id:
            raise PipelineError("unknown_project", f"No project {project_id!r} exists.",
                                f"This harness only serves {self.project_id!r}; call project_status with that id.")
        directory = self.projects / project_id
        if not directory.is_dir():
            raise PipelineError("no_results", f"The demo project {project_id!r} has not been built.",
                                "Run: python -m uv run harness/demo/build_demo_project.py")
        return directory

    def _spec(self, project_id: str) -> dict:
        return _read_json(self._dir(project_id) / "spec.json", "no_results",
                          "Run: python -m uv run harness/demo/build_demo_project.py")

    def _run(self, project_id: str) -> dict:
        return _read_json(self._dir(project_id) / "run.json", "no_results",
                          "Run: python -m uv run harness/demo/build_demo_project.py")

    def _posts_parquet(self, project_id: str) -> str:
        path = self._dir(project_id) / "posts.parquet"
        if not path.exists():
            raise PipelineError("no_results", f"{project_id} has no posts.parquet yet.",
                                "Run: python -m uv run harness/demo/build_demo_project.py")
        return str(path)

    # --- submission ------------------------------------------------------------

    def _record_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        return self.state / "submissions" / f"{digest}.json"

    async def submit(self, spec: dict, idempotency_key: str) -> dict:
        if not idempotency_key:
            raise PipelineError("bad_request", "submit needs an idempotency key.",
                                "Use spec_hash + confirmation_id, as DESIGN.md 10 requires.")
        path = self._record_path(idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # a retry must never create a second project
            record = json.loads(path.read_text(encoding="utf-8"))
            return {"project_id": record["project_id"], "status": self._phase(record["submitted_ms"])[0]}
        record = {"project_id": self.project_id, "idempotency_key": idempotency_key,
                  "submitted_ms": int(self.clock() * 1000), "name": str(spec.get("name") or ""),
                  "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}
        temp = path.with_name(path.name + ".tmp")  # a reader must never see half a record
        temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        temp.replace(path)
        return {"project_id": record["project_id"], "status": "QUEUED"}

    def _submitted_ms(self, project_id: str) -> int | None:
        """The latest submission of this project, if the session ever submitted one."""
        directory = self.state / "submissions"
        stamps = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if record.get("project_id") == project_id and isinstance(record.get("submitted_ms"), int):
                stamps.append(record["submitted_ms"])
        return max(stamps) if stamps else None

    def _phase(self, submitted_ms: int) -> tuple[str, float]:
        elapsed = self.clock() - submitted_ms / 1000
        if elapsed < QUEUED_S:
            return "QUEUED", 0.0
        if elapsed < RUNNING_S:
            return "RUNNING", round((elapsed - QUEUED_S) / (RUNNING_S - QUEUED_S), 3)
        return "READY", 1.0

    # --- reads -----------------------------------------------------------------

    async def status(self, project_id: str) -> dict:
        run = await asyncio.to_thread(self._run, project_id)
        submitted_ms = await asyncio.to_thread(self._submitted_ms, project_id)
        phase, progress = self._phase(submitted_ms) if submitted_ms else ("READY", 1.0)
        recorded = str(run.get("status") or "READY").upper()
        if phase == "READY" and recorded != "READY":
            phase = "FAILED"  # only a run.json that claims success may be served as a finished project
        counts = {key: run.get(key) for key in ("n_matched", "n_scored", "n_failed")}
        counts |= {"matched_by_day": run.get("matched_by_day", {}),
                   "sampled_fraction_by_day": run.get("sampled_fraction_by_day", {}),
                   "jev_cost_usd": run.get("jev_cost_usd")}
        error = None
        if phase == "FAILED":
            reasons = run.get("failure_reasons") or {}
            error = {"code": "run_failed",
                     "message": f"{project_id} is {recorded}: {counts['n_scored']} of "
                                f"{(counts['n_scored'] or 0) + (counts['n_failed'] or 0)} sampled posts were scored."
                                + (f" Reasons: {reasons}." if reasons else ""),
                     "hint": "Its charts and posts would be empty or unrepresentative; say so instead of "
                             "reporting numbers, and rerun the build once the reason is fixed."}
        return {"project_id": project_id, "status": phase, "progress": progress,
                "counts": counts if phase in ("READY", "FAILED") else {"n_matched": None, "n_scored": None, "n_failed": None},
                "error": error}

    async def list_charts(self, project_id: str) -> list[dict]:
        def read():
            directory = self._dir(project_id) / "charts"
            charts = []
            for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
                try:
                    packaged = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                charts.append({"chart_id": packaged.get("chart_id", path.stem),
                               "title": packaged.get("title", path.stem), "type": packaged.get("type", "unknown")})
            return charts
        return await asyncio.to_thread(read)

    async def chart(self, project_id: str, chart_id: str) -> dict:
        def read():
            directory = self._dir(project_id) / "charts"
            path = directory / f"{Path(chart_id).name}.json"
            if not path.exists():
                available = sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []
                raise PipelineError("unknown_chart", f"{project_id} has no chart {chart_id!r}.",
                                    f"Available: {', '.join(available) or 'none yet - charts.py has not run'}.")
            return _read_json(path, "unknown_chart", "Call list_charts for the chart ids that exist.")
        return await asyncio.to_thread(read)

    async def posts(self, project_id: str, *, date_from=None, date_to=None, question=None, label=None,
                    sort="engagement", limit=MAX_POSTS_PER_PAGE) -> list[dict]:
        if sort not in SORTS:
            raise PipelineError("bad_sort", f"{sort!r} is not a sort order.", f"Use one of: {', '.join(SORTS)}.")
        return await asyncio.to_thread(self._read_posts, project_id, _day(date_from, "date_from"),
                                       _day(date_to, "date_to"), question, label, sort, _page_size(limit))

    def _read_posts(self, project_id, date_from, date_to, question, label, sort, limit) -> list[dict]:
        """Every DuckDB failure becomes a PipelineError: the model sees tool errors, not tracebacks."""
        try:
            return self._select_posts(project_id, date_from, date_to, question, label, sort, limit)
        except duckdb.InterruptException as error:
            raise PipelineError("timeout", f"Reading posts passed {QUERY_TIMEOUT_S} seconds and was cancelled.",
                                "Narrow the date range or ask for fewer posts.") from error
        except duckdb.Error as error:
            raise PipelineError("read_failed", f"Reading {project_id}'s posts failed: "
                                               f"{str(error).splitlines()[0] if str(error) else type(error).__name__}",
                                "The results may be half-written; rebuild them with "
                                "python -m uv run harness/demo/build_demo_project.py") from error

    def _select_posts(self, project_id, date_from, date_to, question, label, sort, limit) -> list[dict]:
        path = self._posts_parquet(project_id)
        spec = self._spec(project_id)
        kinds = {item["name"]: item["type"] for item in spec.get("classification", [])}
        choices = [name for name, kind in kinds.items() if kind == "choice"]
        nouls = [name for name, kind in kinds.items() if kind == "noul"]
        stored_noul = _stored_noul(spec, nouls)
        if question is not None and question not in kinds:
            raise PipelineError("unknown_question", f"{project_id} has no question {question!r}.",
                                f"Its questions are: {', '.join(kinds) or 'none'}.")
        if question in nouls and question != stored_noul:
            raise PipelineError("unknown_question", f"{project_id} stores no probability for {question!r}.",
                                f"relevant_p holds one yes/no probability, {stored_noul!r}; filter on that one "
                                f"or on a choice question ({', '.join(choices) or 'none'}).")

        where, arguments = ["scored"], {"path": path}
        if date_from:
            where.append("day >= $date_from::DATE")
            arguments["date_from"] = date_from
        if date_to:  # exclusive, like every window in the spec
            where.append("day < $date_to::DATE")
            arguments["date_to"] = date_to
        if label is not None:
            if question is None:
                raise PipelineError("bad_request", "A label needs the question it belongs to.",
                                    f"Pass question=<one of {', '.join(kinds) or 'none'}> together with label.")
            if question in nouls:
                if label not in ("yes", "no"):
                    raise PipelineError("bad_label", f"{question!r} is a yes/no question, so {label!r} is not one of its labels.",
                                        "Use label='yes' or label='no'.")
                gate = (spec.get("relevance_gate") or {}).get("min_probability", 0.5)
                where.append(f"relevant_p {'>=' if label == 'yes' else '<'} {float(gate)}")
            else:
                where.append(f'"{question}" = $label')
                arguments["label"] = label
        noul_column = "relevant_p" if question and question == stored_noul else None
        if sort == "low_confidence" and noul_column:
            where.append(f"abs({noul_column} - 0.5) < {LOW_CONFIDENCE_NOUL}")

        order = {
            "engagement": "like_count DESC NULLS LAST, views_count DESC NULLS LAST, id",
            "random": "hash(id || 'page'), id",  # stable, so re-asking the same question cites the same posts
            "most_negative": "sentiment ASC NULLS LAST, id",
            "most_positive": "sentiment DESC NULLS LAST, id",
            "low_confidence": (f"abs({noul_column} - 0.5) ASC, id" if noul_column
                               else (f'"{question}_confidence" ASC NULLS FIRST, id' if question in choices
                                     else "sentiment_confidence ASC NULLS FIRST, id")),
        }[sort]
        columns = ["id", "strftime(day, '%Y-%m-%d') AS day", "strftime(created_at, '%Y-%m-%dT%H:%M:%SZ') AS created_at",
                   "lang", "like_count", "retweet_count", "views_count", "relevant_p"]
        for name in choices:
            columns += [f'"{name}"', f'"{name}_confidence"']
        columns += ["sentiment", "sentiment_confidence", f"left(body, {BODY_CHARS}) AS body",
                    f"length(body) > {BODY_CHARS} AS body_truncated"]

        connection = _connect()
        timer = threading.Timer(QUERY_TIMEOUT_S, connection.interrupt)
        timer.start()
        try:
            cursor = connection.execute(
                f"SELECT {', '.join(columns)} FROM read_parquet($path) WHERE {' AND '.join(where)}"
                f" ORDER BY {order} LIMIT {limit}", arguments)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:  # _read_posts turns the interrupt and every other DuckDB failure into a PipelineError
            timer.cancel()
            connection.close()


def get_pipeline(repo_root) -> Pipeline:
    root = Path(repo_root)
    if os.environ.get("PIPELINE") == "team":
        raise NotImplementedError(
            "TeamPipeline is not written yet. Three things are still needed from the pipeline owner "
            "(DESIGN.md 17): (1) how the harness starts a run - a function call, an HTTP endpoint or a row "
            "in a table - and the exact JSON it takes; (2) the result tables and columns (post id, text, "
            "created time, likes, category, sentiment, confidence, and the per-day sampled fraction); "
            "(3) which API the posts come from, so the preview counts the same universe the run will fetch. "
            "Unset PIPELINE to use FakePipeline and the canned demo project.")
    return FakePipeline(root / "harness" / "demo" / "projects", root / "harness" / "state" / "pipeline")
