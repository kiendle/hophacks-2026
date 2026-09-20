"""Durable, resumable Jev enrichment runner.

The engine deliberately keeps the provider boundary small: request construction,
answer validation, and categorisation semantics live in :mod:`jev_protocol`.
This module owns durable scheduling, accounting, and recovery only.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _datetime
import email.utils
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import httpx

from jev_protocol import (
    classification_request,
    classify,
    configuration_key,
    request_key,
    sentiment_request,
    validate_response,
)


API_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Pricing is $0.042 per million input tokens; output is free.
INPUT_PRICE_PER_TOKEN = 0.042 / 1_000_000.0
RESERVATION_TOKENS = 64_000
PROMPT_VERSION = "company-relevance-v2"


class _BudgetUnavailable(Exception):
    pass


class _BudgetWait(Exception):
    pass


class _RouteUnavailable(Exception):
    pass


@contextlib.asynccontextmanager
async def _client_context(client: Any):
    """Use either a real async client or a test double with the same API."""
    enter = getattr(client, "__aenter__", None)
    exit_ = getattr(client, "__aexit__", None)
    if enter is None or exit_ is None:
        yield client
        return
    entered = await enter()
    try:
        yield entered
    finally:
        await exit_(None, None, None)


class _Pacer:
    """A single monotonic clock gate shared by all request workers."""

    def __init__(self, requests_per_minute: float) -> None:
        self.requests_per_minute = float(requests_per_minute)
        self.interval = 60.0 / self.requests_per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    def set_rate(self, requests_per_minute: float) -> None:
        self.requests_per_minute = float(requests_per_minute)
        self.interval = 60.0 / self.requests_per_minute

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
            delay = at - now
        if delay > 0:
            await asyncio.sleep(delay)


class Engine:
    """Run Jev requests against a durable SQLite checkpoint database.

    ``Engine`` does not read credentials or contact the provider at
    construction time.  The file lock is acquired on ``__enter__`` (or lazily
    by the first operation for callers that do not use a ``with`` block).
    """

    def __init__(self, db_path: Path, taxonomy: dict, policy: dict) -> None:
        self.db_path = Path(db_path)
        self.taxonomy = taxonomy
        self.policy = policy
        self._configuration_key = configuration_key(taxonomy, policy)
        self._conn: sqlite3.Connection | None = None
        self._lock_file: Any = None
        self._entered = False
        self._stop_event = threading.Event()
        self._stop_reason: str | None = None
        self._active_run_id: str | None = None
        self._route_stop_events: dict[str, threading.Event] = {}
        self._route_pacers: dict[str, _Pacer] = {}
        self._openrouter_rate: float | None = None

    # ------------------------------------------------------------------
    # Context and SQLite lifecycle
    # ------------------------------------------------------------------
    def __enter__(self) -> "Engine":
        if self._conn is not None:
            return self
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.db_path) + ".lock")
        self._lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self._lock_file.close()
            self._lock_file = None
            raise BlockingIOError(f"Jev database is already in use: {self.db_path}")
        try:
            self._conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            # Snapshot readers must not block durable response commits.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
            self._migrate_schema()
            self._recover_inflight()
            self._entered = True
            return self
        except Exception:
            self._close_resources()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._close_resources()

    def _close_resources(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        lock_file, self._lock_file = self._lock_file, None
        if lock_file is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                lock_file.close()
        self._entered = False

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self.__enter__()
        assert self._conn is not None
        return self._conn

    def _create_schema(self) -> None:
        conn = self._ensure_open()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                configuration_key TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                retry_after TEXT,
                resume_after REAL,
                max_usd REAL,
                actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                actual_cost_usd REAL NOT NULL DEFAULT 0,
                runtime_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contents (
                content_version TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS contents_text_hash_idx ON contents(text_hash);
            CREATE TABLE IF NOT EXISTS items (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                content_version TEXT NOT NULL REFERENCES contents(content_version),
                stage TEXT NOT NULL DEFAULT 'categorization_pending',
                categorization_request_hash TEXT,
                categorization_response_json TEXT,
                categorization_usage_json TEXT,
                categorization_result_json TEXT,
                sentiment_request_hash TEXT,
                sentiment_response_json TEXT,
                sentiment_usage_json TEXT,
                sentiment_result_json TEXT,
                last_error TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY(run_id, content_version)
            );
            CREATE INDEX IF NOT EXISTS items_stage_idx ON items(run_id, stage);
            CREATE INDEX IF NOT EXISTS items_pending_idx ON items(run_id, content_version)
                WHERE stage IN ('categorization_pending','sentiment_pending');
            CREATE TABLE IF NOT EXISTS response_cache (
                request_hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                content_version TEXT NOT NULL,
                stage TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                route TEXT NOT NULL DEFAULT 'typesafe',
                request_json TEXT,
                provenance_json TEXT,
                provenance TEXT,
                attempt_no INTEGER NOT NULL,
                state TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_usd REAL NOT NULL DEFAULT 0,
                billing_uncertain INTEGER NOT NULL DEFAULT 0,
                actual_input_tokens INTEGER,
                actual_output_tokens INTEGER,
                http_status INTEGER,
                error_kind TEXT,
                diagnostic TEXT,
                response_json TEXT,
                retry_after TEXT,
                started_at REAL NOT NULL,
                sent_at REAL,
                finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS route_budgets (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                route TEXT NOT NULL,
                max_usd REAL,
                actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                actual_cost_usd REAL NOT NULL DEFAULT 0,
                reserved_usd REAL NOT NULL DEFAULT 0,
                uncertain_usd REAL NOT NULL DEFAULT 0,
                in_flight INTEGER NOT NULL DEFAULT 0,
                requests_started INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                completed_texts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'disabled',
                reason TEXT,
                retry_after TEXT,
                resume_after REAL,
                configured_concurrency INTEGER,
                requests_per_minute REAL,
                active_http INTEGER NOT NULL DEFAULT 0,
                peak_active_http INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(run_id, route)
            );
            CREATE INDEX IF NOT EXISTS route_budgets_status_idx
                ON route_budgets(run_id, status);
            CREATE INDEX IF NOT EXISTS attempts_run_idx ON attempts(run_id, state);
            CREATE INDEX IF NOT EXISTS attempts_item_idx ON attempts(run_id, content_version, stage);
            CREATE INDEX IF NOT EXISTS attempts_uncertain_idx ON attempts(run_id)
                WHERE billing_uncertain=1;
            """
        )

    def _migrate_schema(self) -> None:
        """Add route accounting/provenance while preserving legacy checkpoints."""
        conn = self._ensure_open()
        for table, name, definition in (
            ("attempts", "route", "TEXT NOT NULL DEFAULT 'typesafe'"),
            ("attempts", "request_json", "TEXT"),
            ("attempts", "provenance_json", "TEXT"),
            ("attempts", "provenance", "TEXT"),
            ("attempts", "sent_at", "REAL"),
        ):
            columns = {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS attempts_route_idx "
            "ON attempts(run_id, route, state)"
        )
        now = time.time()
        # Existing runs only ever used TypeSafe.  Seed that route once from
        # the durable aggregate; subsequent accounting never scans attempts.
        conn.execute(
            """
            INSERT OR IGNORE INTO route_budgets
            (run_id, route, max_usd, actual_input_tokens, actual_output_tokens,
             actual_cost_usd, requests_started, successes, completed_texts, status, updated_at)
            SELECT r.run_id, 'typesafe', r.max_usd, r.actual_input_tokens,
                   r.actual_output_tokens, r.actual_cost_usd,
                   (SELECT count(*) FROM attempts a WHERE a.run_id=r.run_id AND a.state!='canceled'),
                   (SELECT count(*) FROM attempts a WHERE a.run_id=r.run_id AND a.state='success'),
                   (SELECT count(*) FROM items i WHERE i.run_id=r.run_id AND i.stage='done'),
                   CASE WHEN r.status='running' THEN 'running' ELSE 'prepared' END,
                   r.updated_at
            FROM runs r WHERE NOT EXISTS (
                SELECT 1 FROM route_budgets b WHERE b.run_id=r.run_id AND b.route='typesafe'
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO route_budgets
            (run_id, route, max_usd, status, updated_at)
            SELECT run_id, 'openrouter', NULL, 'disabled', updated_at
            FROM runs
            """
        )
        # Any legacy attempt belongs to the direct route.  This UPDATE is
        # idempotent and only fills the newly-added provenance columns.
        conn.execute(
            "UPDATE attempts SET route='typesafe' "
            "WHERE route IS NULL OR route=''"
        )
        conn.execute(
            """
            UPDATE route_budgets
            SET in_flight=COALESCE((
                    SELECT COUNT(*) FROM attempts a
                    WHERE a.run_id=route_budgets.run_id
                      AND a.route=route_budgets.route AND a.state='in_flight'
                ),0),
                reserved_usd=COALESCE((
                    SELECT SUM(a.reserved_usd) FROM attempts a
                    WHERE a.run_id=route_budgets.run_id
                      AND a.route=route_budgets.route AND a.state='in_flight'
                ),0),
                uncertain_usd=COALESCE((
                    SELECT SUM(a.reserved_usd) FROM attempts a
                    WHERE a.run_id=route_budgets.run_id
                      AND a.route=route_budgets.route AND a.billing_uncertain=1
                ),0)
            """
        )

    def _tx(self) -> sqlite3.Connection:
        conn = self._ensure_open()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    @staticmethod
    def _commit(conn: sqlite3.Connection) -> None:
        conn.execute("COMMIT")

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")

    def _recover_inflight(self) -> None:
        """Requeue every interrupted item and retain ambiguous reservations."""
        conn = self._ensure_open()
        attempt_rows = conn.execute(
            """
            SELECT attempt_id, run_id, content_version, stage, route, reserved_usd
            FROM attempts WHERE state='in_flight'
            """
        ).fetchall()
        item_rows = conn.execute(
            """
            SELECT run_id, content_version, stage
            FROM items
            WHERE stage IN ('categorization_inflight','sentiment_inflight')
            """
        ).fetchall()
        if not attempt_rows and not item_rows:
            return
        now = time.time()
        tx = self._tx()
        try:
            for row in attempt_rows:
                tx.execute(
                    """
                    UPDATE attempts
                    SET state='ambiguous', billing_uncertain=1,
                        diagnostic=?, finished_at=?
                    WHERE attempt_id=? AND state='in_flight'
                    """,
                    (
                        "process recovery: request may have reached provider; billing is unknown",
                        now,
                        row["attempt_id"],
                    ),
                )
                tx.execute(
                    """
                    UPDATE route_budgets
                    SET in_flight=MAX(0,in_flight-1),
                        reserved_usd=MAX(0,reserved_usd-?),
                        uncertain_usd=uncertain_usd+?,
                        updated_at=?
                    WHERE run_id=? AND route=?
                    """,
                    (
                        float(row["reserved_usd"] or 0.0),
                        float(row["reserved_usd"] or 0.0),
                        now,
                        row["run_id"],
                        row["route"] or "typesafe",
                    ),
                )
                if (row["route"] or "typesafe") == "openrouter":
                    tx.execute(
                        """
                        UPDATE route_budgets
                        SET status='stopped', reason='recovered_ambiguous',
                            updated_at=?
                        WHERE run_id=? AND route='openrouter'
                        """,
                        (now, row["run_id"]),
                    )
            for row in item_rows:
                has_attempt = tx.execute(
                    """
                    SELECT 1 FROM attempts
                    WHERE run_id=? AND content_version=?
                      AND stage=?
                    LIMIT 1
                    """,
                    (row["run_id"], row["content_version"], row["stage"]),
                ).fetchone()
                pending = (
                    "categorization_pending"
                    if row["stage"] == "categorization_inflight"
                    else "sentiment_pending"
                )
                diagnostic = (
                    "recovered unfinished request; provider billing is unknown"
                    if has_attempt
                    else "recovered item claimed before request intent was persisted"
                )
                tx.execute(
                    """
                    UPDATE items SET stage=?, last_error=?, updated_at=?
                    WHERE run_id=? AND content_version=?
                    """,
                    (pending, diagnostic, now, row["run_id"], row["content_version"]),
                )
                tx.execute(
                    """
                    UPDATE runs SET status='paused', reason=?, updated_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    ("recovered_after_crash", now, row["run_id"]),
                )
            # An in-flight attempt always has a matching item in normal flow;
            # retain a paused run even if a partially-written legacy checkpoint
            # does not, so the uncertainty is visible to callers.
            for row in attempt_rows:
                tx.execute(
                    """
                    UPDATE runs SET status='paused', reason=?, updated_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    ("recovered_after_crash", now, row["run_id"]),
                )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    # ------------------------------------------------------------------
    # Run preparation and status
    # ------------------------------------------------------------------
    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _identity_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
        # Runtime knobs affect dispatch only, never the semantic run identity.
        runtime_names = {
            "api_key",
            "max_usd",
            "concurrency",
            "requests_per_minute",
            "timeout_seconds",
            "max_attempts",
            "runtime",
            "runtime_knobs",
        }
        return {k: v for k, v in manifest.items() if k not in runtime_names}

    def _normalise_manifest(
        self, manifest: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str, str]:
        if not isinstance(manifest, Mapping):
            raise TypeError("manifest must be a mapping")
        normalized = dict(manifest)
        given = normalized.get("configuration_key")
        if given is not None and given != self._configuration_key:
            raise ValueError(
                "manifest configuration_key does not match current taxonomy/policy"
            )
        normalized["configuration_key"] = self._configuration_key
        try:
            encoded = self._json(normalized)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"manifest is not JSON serializable: {exc}") from exc
        identity = self._identity_manifest(normalized)
        identity_hash = hashlib.sha256(self._json(identity).encode("utf-8")).hexdigest()
        return normalized, identity_hash, encoded

    def prepare(self, manifest: dict, records: Iterable[tuple[str, str]]) -> str:
        conn = self._ensure_open()
        normalized, identity_hash, manifest_json = self._normalise_manifest(manifest)
        now = time.time()
        run_id = "jev-" + identity_hash[:32]
        tx = self._tx()
        try:
            existing = tx.execute(
                "SELECT run_id, configuration_key, status FROM runs WHERE identity_hash=?",
                (identity_hash,),
            ).fetchone()
            if existing is None:
                tx.execute(
                    """
                    INSERT INTO runs
                    (run_id, identity_hash, manifest_json, configuration_key, status,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (
                        run_id,
                        identity_hash,
                        manifest_json,
                        self._configuration_key,
                        now,
                        now,
                    ),
                )
            else:
                run_id = existing["run_id"]
                if existing["configuration_key"] != self._configuration_key:
                    raise ValueError(
                        "existing run configuration_key does not match current engine"
                    )
                # Keep the original semantic manifest.  Runtime-only changes and
                # a repeated prepare must not reset completed work.
                run_id = str(run_id)

            added = 0
            for record in records:
                if not isinstance(record, (tuple, list)) or len(record) != 2:
                    raise ValueError(
                        "records must contain (content_version, text) pairs"
                    )
                content_version, text = record
                if not isinstance(content_version, str) or not content_version:
                    raise ValueError("content_version must be a nonempty string")
                if not isinstance(text, str) or not text:
                    raise ValueError("content text must be a nonempty string")
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                old = tx.execute(
                    "SELECT text, text_hash FROM contents WHERE content_version=?",
                    (content_version,),
                ).fetchone()
                if old is not None:
                    if old["text"] != text:
                        raise ValueError(
                            f"content_version {content_version!r} is already bound to different text"
                        )
                else:
                    # A caller-supplied content version is a content hash in the
                    # prepared data.  Refuse a hash collision rather than silently
                    # replacing the durable snapshot.
                    same_hash = tx.execute(
                        "SELECT text FROM contents WHERE text_hash=? LIMIT 1",
                        (text_hash,),
                    ).fetchone()
                    if same_hash is not None and same_hash["text"] != text:
                        raise ValueError(
                            "same content hash was supplied with different text"
                        )
                    tx.execute(
                        "INSERT INTO contents(content_version,text,text_hash) VALUES (?,?,?)",
                        (content_version, text, text_hash),
                    )
                inserted = tx.execute(
                    """
                    INSERT OR IGNORE INTO items
                    (run_id, content_version, stage, updated_at)
                    VALUES (?, ?, 'categorization_pending', ?)
                    """,
                    (run_id, content_version, now),
                ).rowcount
                added += int(inserted or 0)

            if (
                added
                and tx.execute(
                    "SELECT 1 FROM runs WHERE run_id=? AND status='completed'",
                    (run_id,),
                ).fetchone()
            ):
                tx.execute(
                    "UPDATE runs SET status='prepared', reason=NULL, updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
            self._commit(tx)
            return run_id
        except Exception:
            self._rollback(tx)
            raise

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = (
            self._ensure_open()
            .execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            .fetchone()
        )
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return row

    @staticmethod
    def _iso_timestamp(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        return _datetime.datetime.fromtimestamp(
            epoch, tz=_datetime.timezone.utc
        ).isoformat()

    def status(self, run_id: str) -> dict[str, Any]:
        return self._status(self._ensure_open(), run_id)

    def route_status(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Return persisted, route-local accounting without scanning attempts."""
        return self._route_status_rows(self._ensure_open(), run_id)

    @staticmethod
    def _route_status_rows(
        conn: sqlite3.Connection, run_id: str
    ) -> dict[str, dict[str, Any]]:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_budgets'"
        ).fetchone()
        if table is None:
            return {}
        rows = conn.execute(
            "SELECT * FROM route_budgets WHERE run_id=? ORDER BY route", (run_id,)
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            max_usd = row["max_usd"]
            spent = float(row["actual_cost_usd"] or 0.0)
            uncertain = float(row["uncertain_usd"] or 0.0)
            reserved = float(row["reserved_usd"] or 0.0)
            configured_concurrency = row["configured_concurrency"]
            requests_per_minute = row["requests_per_minute"]
            result[str(row["route"])] = {
                "route": str(row["route"]),
                "actual_input_tokens": int(row["actual_input_tokens"] or 0),
                "actual_output_tokens": int(row["actual_output_tokens"] or 0),
                "actual_cost_usd": spent,
                "max_usd": float(max_usd) if max_usd is not None else None,
                "in_flight": int(row["in_flight"] or 0),
                "reserved_usd": reserved,
                "uncertain_usd": uncertain,
                "requests_started": int(row["requests_started"] or 0),
                "successes": int(row["successes"] or 0),
                "completed_texts": int(row["completed_texts"] or 0),
                "status": row["status"],
                "reason": row["reason"],
                "retry_after": row["retry_after"],
                "resume_after": Engine._iso_timestamp(row["resume_after"]),
                "resume_after_epoch": row["resume_after"],
                "configured_concurrency": (
                    int(configured_concurrency)
                    if configured_concurrency is not None
                    else None
                ),
                "concurrency": (
                    int(configured_concurrency)
                    if configured_concurrency is not None
                    else None
                ),
                "requests_per_minute": (
                    float(requests_per_minute)
                    if requests_per_minute is not None
                    else None
                ),
                "configured_requests_per_minute": (
                    float(requests_per_minute)
                    if requests_per_minute is not None
                    else None
                ),
                "active_http": int(row["active_http"] or 0),
                "peak_active_http": int(row["peak_active_http"] or 0),
                "available_usd": (
                    None
                    if max_usd is None
                    else max(0.0, float(max_usd) - spent - uncertain - reserved)
                ),
                "updated_at": Engine._iso_timestamp(row["updated_at"]),
            }
        return result
    @staticmethod
    def _status(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        counts_rows = conn.execute(
            "SELECT stage, COUNT(*) AS n FROM items WHERE run_id=? GROUP BY stage",
            (run_id,),
        ).fetchall()
        counts: dict[str, int] = {str(r["stage"]): int(r["n"]) for r in counts_rows}
        total = sum(counts.values())
        done = counts.get("done", 0)
        cat_accepted = int(
            conn.execute(
                'SELECT COUNT(*) FROM items WHERE run_id=? AND categorization_result_json LIKE \'%"status":"accepted"%\'',
                (run_id,),
            ).fetchone()[0]
        )
        cat_others = int(
            conn.execute(
                'SELECT COUNT(*) FROM items WHERE run_id=? AND categorization_result_json LIKE \'%"status":"others"%\'',
                (run_id,),
            ).fetchone()[0]
        )
        ready = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM items
                WHERE run_id=? AND stage='done'
                  AND categorization_result_json LIKE '%"status":"accepted"%'
                  AND sentiment_response_json IS NOT NULL
                """,
                (run_id,),
            ).fetchone()[0]
        )
        active_row = conn.execute(
            "SELECT COALESCE(SUM(reserved_tokens),0) AS tokens, COALESCE(SUM(reserved_usd),0) AS usd "
            "FROM attempts WHERE run_id=? AND state='in_flight'",
            (run_id,),
        ).fetchone()
        uncertain_row = conn.execute(
            "SELECT COALESCE(SUM(reserved_tokens),0) AS tokens, COALESCE(SUM(reserved_usd),0) AS usd "
            "FROM attempts WHERE run_id=? AND billing_uncertain=1",
            (run_id,),
        ).fetchone()
        active_tokens = int(active_row["tokens"] or 0)
        active_usd = float(active_row["usd"] or 0.0)
        uncertain_tokens = int(uncertain_row["tokens"] or 0)
        uncertain_usd = float(uncertain_row["usd"] or 0.0)
        max_usd = row["max_usd"]
        spent = float(row["actual_cost_usd"] or 0.0)
        runtime = json.loads(row["runtime_json"] or "{}")
        result: dict[str, Any] = {
            "run_id": run_id,
            "status": row["status"],
            "reason": row["reason"],
            "manifest": json.loads(row["manifest_json"]),
            "configuration_key": row["configuration_key"],
            "counts": {
                "total": total,
                "completed": done,
                "pending": counts.get("categorization_pending", 0)
                + counts.get("sentiment_pending", 0),
                "categorization_pending": counts.get("categorization_pending", 0),
                "categorization_inflight": counts.get("categorization_inflight", 0),
                "sentiment_pending": counts.get("sentiment_pending", 0),
                "sentiment_inflight": counts.get("sentiment_inflight", 0),
                "done": done,
                "accepted": cat_accepted,
                "ready": ready,
                "others": cat_others,
                **counts,
            },
            "actual_input_tokens": int(row["actual_input_tokens"] or 0),
            "actual_output_tokens": int(row["actual_output_tokens"] or 0),
            "actual_cost_usd": spent,
            "spent_input_tokens": int(row["actual_input_tokens"] or 0),
            "spent_usd": spent,
            "uncertain_input_tokens": uncertain_tokens,
            "uncertain_charge_usd": uncertain_usd,
            "inflight_reserved_input_tokens": active_tokens,
            "inflight_reserved_usd": active_usd,
            "max_usd": float(max_usd) if max_usd is not None else None,
            "available_usd": (
                None
                if max_usd is None
                else max(0.0, float(max_usd) - spent - uncertain_usd - active_usd)
            ),
            "retry_after": row["retry_after"],
            "resume_after": Engine._iso_timestamp(row["resume_after"]),
            "runtime": runtime,
            "routes": Engine._route_status_rows(conn, run_id),
            "updated_at": Engine._iso_timestamp(row["updated_at"]),
        }
        # Useful accounting aliases for callers that use the vocabulary from
        # the command-line report.
        result["counts"]["in_flight"] = counts.get(
            "categorization_inflight", 0
        ) + counts.get("sentiment_inflight", 0)
        result["request_concurrency"] = result["runtime"].get("concurrency")
        result["max_concurrency"] = runtime.get("concurrency")
        result["requests_per_minute"] = runtime.get("requests_per_minute")
        result["concurrency"] = runtime.get("concurrency")
        return result

    # ------------------------------------------------------------------
    # Durable accounting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_error(value: Any, limit: int = 600) -> str:
        text = str(value).replace("\x00", " ")
        text = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer [redacted]", text)
        text = re.sub(
            r"(?i)(api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            text,
        )
        return text[:limit]

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int] | None:
        if not isinstance(response, Mapping):
            return None
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return None
        in_value = usage.get("input_tokens")
        out_value = usage.get("output_tokens")
        if isinstance(in_value, bool) or isinstance(out_value, bool):
            return None
        if not isinstance(in_value, int) or not isinstance(out_value, int):
            return None
        if in_value < 0 or out_value < 0:
            return None
        return {"input_tokens": in_value, "output_tokens": out_value}

    def _budget_begin_attempt(
        self,
        run_id: str,
        content_version: str,
        stage: str,
        request_hash: str,
        max_usd: float,
        route: str = "typesafe",
        request: Mapping[str, Any] | None = None,
    ) -> int:
        tx = self._tx()
        try:
            run = tx.execute("SELECT run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            budget = tx.execute(
                "SELECT * FROM route_budgets WHERE run_id=? AND route=?",
                (run_id, route),
            ).fetchone()
            if budget is None:
                raise _RouteUnavailable(f"missing budget for route {route}")
            if (
                budget["status"] == "paused"
                and budget["resume_after"] is not None
                and float(budget["resume_after"]) > time.time()
            ):
                self._rollback(tx)
                raise _RouteUnavailable(budget["reason"] or route)
            if budget["status"] in ("stopped", "disabled"):
                self._rollback(tx)
                raise _RouteUnavailable(budget["reason"] or route)
            # A route cap is durable.  ``max_usd`` is only the initial/resume
            # value; spend and reservations are never reset by a resume.
            stored_cap = budget["max_usd"]
            cap = float(max_usd if stored_cap is None else stored_cap)
            spent = float(budget["actual_cost_usd"] or 0.0)
            active_usd = float(budget["reserved_usd"] or 0.0)
            uncertain_usd = float(budget["uncertain_usd"] or 0.0)
            reserve_usd = RESERVATION_TOKENS * INPUT_PRICE_PER_TOKEN
            if spent + uncertain_usd + reserve_usd > cap + 1e-9:
                self._rollback(tx)
                raise _BudgetUnavailable
            if spent + uncertain_usd + active_usd + reserve_usd > cap + 1e-9:
                self._rollback(tx)
                raise _BudgetWait
            previous = tx.execute(
                """
                SELECT COALESCE(MAX(attempt_no),0) FROM attempts
                WHERE run_id=? AND content_version=? AND stage=?
                """,
                (run_id, content_version, stage),
            ).fetchone()[0]
            now = time.time()
            provenance = {
                "route": route,
                "provider": "OpenRouter/TypeSafe"
                if route == "openrouter"
                else "TypeSafe",
                "model": (request or {}).get("model"),
            }
            cur = tx.execute(
                """
                INSERT INTO attempts
                (run_id, content_version, stage, request_hash, route,
                 request_json, provenance_json, provenance, attempt_no, state,
                 reserved_tokens, reserved_usd, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', ?, ?, ?)
                """,
                (
                    run_id,
                    content_version,
                    stage,
                    request_hash,
                    route,
                    self._json(request) if request is not None else None,
                    self._json(provenance),
                    self._json(provenance),
                    int(previous or 0) + 1,
                    RESERVATION_TOKENS,
                    reserve_usd,
                    now,
                ),
            )
            tx.execute(
                """
                UPDATE route_budgets
                SET reserved_usd=reserved_usd+?, in_flight=in_flight+1,
                    updated_at=? WHERE run_id=? AND route=?
                """,
                (reserve_usd, now, run_id, route),
            )
            self._commit(tx)
            return int(cur.lastrowid)
        except Exception:
            self._rollback(tx)
            raise

    @staticmethod
    def _response_storage(response: Any, raw_response: str | None = None) -> str | None:
        if raw_response is not None:
            return raw_response
        if response is None:
            return None
        if isinstance(response, str):
            return response
        with contextlib.suppress(TypeError, ValueError):
            return Engine._json(response)
        return None

    def _finish_attempt(
        self,
        attempt_id: int,
        *,
        state: str,
        response: Any = None,
        raw_response: str | None = None,
        usage: dict[str, int] | None = None,
        actual_cost_usd: float | None = None,
        http_status: int | None = None,
        error_kind: str | None = None,
        diagnostic: str | None = None,
        retry_after: str | None = None,
        uncertain: bool = False,
    ) -> None:
        tx = self._tx()
        try:
            row = tx.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                self._rollback(tx)
                return
            now = time.time()
            response_json = self._response_storage(response, raw_response)
            known_usage = usage is not None
            actual_in = usage["input_tokens"] if usage else None
            actual_out = usage["output_tokens"] if usage else None
            reserved_tokens = int(row["reserved_tokens"] or 0)
            reserved_usd = float(row["reserved_usd"] or 0.0)
            route = row["route"] or "typesafe"
            if uncertain:
                billing_uncertain = 1
            else:
                billing_uncertain = 0
                reserved_tokens = 0
                reserved_usd = 0.0
            tx.execute(
                """
                UPDATE attempts SET state=?, reserved_tokens=?, reserved_usd=?,
                    billing_uncertain=?, actual_input_tokens=?, actual_output_tokens=?,
                    http_status=?, error_kind=?, diagnostic=?, response_json=?,
                    retry_after=?, finished_at=? WHERE attempt_id=?
                """,
                (
                    state,
                    reserved_tokens,
                    reserved_usd,
                    billing_uncertain,
                    actual_in,
                    actual_out,
                    http_status,
                    error_kind,
                    self._safe_error(diagnostic) if diagnostic else None,
                    response_json,
                    retry_after,
                    now,
                    attempt_id,
                ),
            )
            cost_known = (
                known_usage
                and actual_cost_usd is not None
                and math.isfinite(float(actual_cost_usd))
                and float(actual_cost_usd) >= 0
            )
            if known_usage and actual_cost_usd is None and route == "typesafe":
                actual_cost_usd = float(actual_in or 0) * INPUT_PRICE_PER_TOKEN
                cost_known = True
            tx.execute(
                """
                UPDATE route_budgets
                SET in_flight=MAX(0,in_flight-1),
                    reserved_usd=MAX(0,reserved_usd-?),
                    uncertain_usd=uncertain_usd+?,
                    actual_input_tokens=actual_input_tokens+?,
                    actual_output_tokens=actual_output_tokens+?,
                    actual_cost_usd=actual_cost_usd+?,
                    updated_at=?
                WHERE run_id=? AND route=?
                """,
                (
                    float(row["reserved_usd"] or 0.0),
                    float(row["reserved_usd"] or 0.0) if uncertain else 0.0,
                    int(actual_in or 0) if known_usage else 0,
                    int(actual_out or 0) if known_usage else 0,
                    float(actual_cost_usd) if cost_known else 0.0,
                    now,
                    row["run_id"],
                    route,
                ),
            )
            if known_usage:
                tx.execute(
                    """
                    UPDATE runs SET actual_input_tokens=actual_input_tokens+?,
                        actual_output_tokens=actual_output_tokens+?,
                        actual_cost_usd=actual_cost_usd+?, updated_at=?
                    WHERE run_id=?
                    """,
                    (
                        int(actual_in or 0),
                        int(actual_out or 0),
                        float(actual_cost_usd) if cost_known else 0.0,
                        now,
                        row["run_id"],
                    ),
                )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _set_item_stage(
        self,
        run_id: str,
        content_version: str,
        stage: str,
        *,
        error: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        fields = dict(fields or {})
        fields["stage"] = stage
        fields["last_error"] = self._safe_error(error) if error else None
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [run_id, content_version]
        conn = self._ensure_open()
        tx = self._tx()
        try:
            tx.execute(
                f"UPDATE items SET {assignments} WHERE run_id=? AND content_version=?",
                values,
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _cache_row(self, request_hash: str) -> sqlite3.Row | None:
        return (
            self._ensure_open()
            .execute(
                "SELECT response_json, usage_json, result_json FROM response_cache WHERE request_hash=?",
                (request_hash,),
            )
            .fetchone()
        )

    def _complete_success(
        self,
        attempt_id: int,
        request_hash: str,
        kind: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        usage: Mapping[str, int],
        result: Any,
        *,
        actual_cost_usd: float | None = None,
        raw_response: str | None = None,
    ) -> None:
        """Commit provider answer, route billing, cache, and item atomically."""
        tx = self._tx()
        try:
            attempt = tx.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise KeyError(f"unknown attempt {attempt_id}")
            now = time.time()
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            route = attempt["route"] or "typesafe"
            if actual_cost_usd is None:
                actual_cost_usd = input_tokens * INPUT_PRICE_PER_TOKEN
            actual_cost_usd = float(actual_cost_usd)
            if not math.isfinite(actual_cost_usd) or actual_cost_usd < 0:
                raise ValueError("actual_cost_usd must be finite and nonnegative")
            stored_response = self._response_storage(response, raw_response)
            tx.execute(
                """
                UPDATE attempts SET state='success', reserved_tokens=0,
                    reserved_usd=0, billing_uncertain=0,
                    actual_input_tokens=?, actual_output_tokens=?,
                    response_json=?, finished_at=?
                WHERE attempt_id=?
                """,
                (input_tokens, output_tokens, stored_response, now, attempt_id),
            )
            done_transition = kind != "categorization" or not (
                isinstance(result, Mapping) and result.get("status") == "accepted"
            )
            tx.execute(
                """
                UPDATE route_budgets
                SET in_flight=MAX(0,in_flight-1),
                    reserved_usd=MAX(0,reserved_usd-?),
                    actual_input_tokens=actual_input_tokens+?,
                    actual_output_tokens=actual_output_tokens+?,
                    actual_cost_usd=actual_cost_usd+?,
                    successes=successes+1,
                    completed_texts=completed_texts+?,
                    updated_at=?
                WHERE run_id=? AND route=?
                """,
                (
                    float(attempt["reserved_usd"] or 0.0),
                    input_tokens,
                    output_tokens,
                    actual_cost_usd,
                    1 if done_transition else 0,
                    now,
                    attempt["run_id"],
                    route,
                ),
            )
            tx.execute(
                """
                UPDATE runs SET actual_input_tokens=actual_input_tokens+?,
                    actual_output_tokens=actual_output_tokens+?,
                    actual_cost_usd=actual_cost_usd+?, updated_at=?
                WHERE run_id=?
                """,
                (
                    input_tokens,
                    output_tokens,
                    actual_cost_usd,
                    now,
                    attempt["run_id"],
                ),
            )
            tx.execute(
                """
                INSERT OR IGNORE INTO response_cache
                (request_hash,kind,request_json,response_json,usage_json,result_json,created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    request_hash,
                    kind,
                    self._json(request),
                    stored_response or self._json(response),
                    self._json(dict(usage)),
                    self._json(result) if result is not None else None,
                    now,
                ),
            )
            if kind == "categorization":
                fields: dict[str, Any] = {
                    "stage": "sentiment_pending"
                    if isinstance(result, Mapping)
                    and result.get("status") == "accepted"
                    else "done",
                    "categorization_request_hash": request_hash,
                    "categorization_response_json": stored_response,
                    "categorization_usage_json": self._json(dict(usage)),
                    "categorization_result_json": self._json(result),
                }
            else:
                fields = {
                    "stage": "done",
                    "sentiment_request_hash": request_hash,
                    "sentiment_response_json": stored_response,
                    "sentiment_usage_json": self._json(dict(usage)),
                    "sentiment_result_json": self._json(result),
                }
            fields["last_error"] = None
            fields["updated_at"] = now
            assignments = ", ".join(f"{key}=?" for key in fields)
            tx.execute(
                f"UPDATE items SET {assignments} WHERE run_id=? AND content_version=?",
                list(fields.values()) + [attempt["run_id"], attempt["content_version"]],
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _apply_category_result(
        self,
        run_id: str,
        content_version: str,
        request_hash: str,
        response: Any,
        usage: Mapping[str, int],
        result: Mapping[str, Any],
        *,
        route: str = "typesafe",
        raw_response: str | None = None,
    ) -> None:
        accepted = result.get("status") == "accepted"
        next_stage = "sentiment_pending" if accepted else "done"
        tx = self._tx()
        try:
            now = time.time()
            stored_response = self._response_storage(response, raw_response)
            tx.execute(
                """
                UPDATE items SET stage=?, last_error=NULL,
                    categorization_request_hash=?, categorization_response_json=?,
                    categorization_usage_json=?, categorization_result_json=?,
                    updated_at=? WHERE run_id=? AND content_version=?
                """,
                (
                    next_stage,
                    request_hash,
                    stored_response,
                    self._json(dict(usage)),
                    self._json(result),
                    now,
                    run_id,
                    content_version,
                ),
            )
            if next_stage == "done":
                tx.execute(
                    """
                    UPDATE route_budgets SET completed_texts=completed_texts+1,
                        updated_at=? WHERE run_id=? AND route=?
                    """,
                    (now, run_id, route),
                )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _apply_sentiment_result(
        self,
        run_id: str,
        content_version: str,
        request_hash: str,
        response: Any,
        usage: Mapping[str, int],
        result: Any,
        *,
        route: str = "typesafe",
        raw_response: str | None = None,
    ) -> None:
        tx = self._tx()
        try:
            now = time.time()
            stored_response = self._response_storage(response, raw_response)
            tx.execute(
                """
                UPDATE items SET stage='done', last_error=NULL,
                    sentiment_request_hash=?, sentiment_response_json=?,
                    sentiment_usage_json=?, sentiment_result_json=?,
                    updated_at=? WHERE run_id=? AND content_version=?
                """,
                (
                    request_hash,
                    stored_response,
                    self._json(dict(usage)),
                    self._json(result),
                    now,
                    run_id,
                    content_version,
                ),
            )
            tx.execute(
                """
                UPDATE route_budgets SET completed_texts=completed_texts+1,
                    updated_at=? WHERE run_id=? AND route=?
                """,
                (now, run_id, route),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise
    # ------------------------------------------------------------------
    # Request execution
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_runtime(
        max_usd: float,
        concurrency: int,
        requests_per_minute: float,
        timeout_seconds: float,
        max_attempts: int,
    ) -> tuple[float, int, float, float, int]:
        if isinstance(max_usd, bool) or not isinstance(max_usd, (int, float)):
            raise ValueError("max_usd must be a finite nonnegative number")
        if not math.isfinite(float(max_usd)) or float(max_usd) < 0:
            raise ValueError("max_usd must be a finite nonnegative number")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise ValueError("concurrency must be a positive integer")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        for name, value in (
            ("requests_per_minute", requests_per_minute),
            ("timeout_seconds", timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        return (
            float(max_usd),
            concurrency,
            float(requests_per_minute),
            float(timeout_seconds),
            max_attempts,
        )

    @staticmethod
    def _parse_retry_after(
        raw: str | None, now: float | None = None
    ) -> tuple[str | None, float | None]:
        if raw is None:
            return None, None
        value = raw.strip()
        if not value:
            return None, None
        now = time.time() if now is None else now
        with contextlib.suppress(ValueError):
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return value, now + seconds
        with contextlib.suppress(Exception):
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
            return value, parsed.timestamp()
        return value, None

    def _ensure_route_budgets(
        self,
        run_id: str,
        *,
        max_usd: float,
        concurrency: int,
        requests_per_minute: float,
        openrouter_enabled: bool,
        openrouter_max_usd: float,
        openrouter_concurrency: int,
        openrouter_requests_per_minute: float,
        timeout_seconds: float,
        max_attempts: int,
    ) -> None:
        now = time.time()
        tx = self._tx()
        try:
            tx.execute(
                """
                INSERT OR IGNORE INTO route_budgets
                (run_id, route, max_usd, status, updated_at)
                VALUES (?, 'typesafe', ?, 'prepared', ?)
                """,
                (run_id, max_usd, now),
            )
            tx.execute(
                """
                INSERT OR IGNORE INTO route_budgets
                (run_id, route, max_usd, status, updated_at)
                VALUES (?, 'openrouter', ?, 'disabled', ?)
                """,
                (run_id, openrouter_max_usd, now),
            )
            for route, cap, configured, rpm, enabled in (
                ("typesafe", max_usd, concurrency, requests_per_minute, True),
                (
                    "openrouter",
                    openrouter_max_usd,
                    openrouter_concurrency,
                    openrouter_requests_per_minute,
                    openrouter_enabled,
                ),
            ):
                current = tx.execute(
                    "SELECT resume_after "
                    "FROM route_budgets WHERE run_id=? AND route=?",
                    (run_id, route),
                ).fetchone()
                blocked = (
                    current is not None
                    and current["resume_after"] is not None
                    and float(current["resume_after"]) > now
                )
                # Uncertain attempts remain funded and are excluded by
                # _claim_item. An explicit resume may process other texts.
                status = "disabled" if not enabled else ("paused" if blocked else "running")
                tx.execute(
                    """
                    UPDATE route_budgets
                    SET max_usd=?, status=?,
                        reason=CASE WHEN ? THEN reason ELSE NULL END,
                        retry_after=CASE WHEN ? THEN retry_after ELSE NULL END,
                        resume_after=CASE WHEN ? THEN resume_after ELSE NULL END,
                        configured_concurrency=?, requests_per_minute=?,
                        updated_at=?
                    WHERE run_id=? AND route=?
                    """,
                    (
                        cap,
                        status,
                        blocked,
                        blocked,
                        blocked,
                        configured,
                        rpm,
                        now,
                        run_id,
                        route,
                    ),
                )
            runtime = {
                "concurrency": concurrency,
                "requests_per_minute": requests_per_minute,
                "timeout_seconds": timeout_seconds,
                "max_attempts": max_attempts,
                "routes": {
                    "typesafe": {
                        "concurrency": concurrency,
                        "requests_per_minute": requests_per_minute,
                    },
                    "openrouter": {
                        "concurrency": openrouter_concurrency,
                        "requests_per_minute": openrouter_requests_per_minute,
                        "enabled": openrouter_enabled,
                    },
                },
            }
            tx.execute(
                """
                UPDATE runs SET status='running', reason=NULL, max_usd=?,
                    runtime_json=?, retry_after=NULL, resume_after=NULL,
                    updated_at=? WHERE run_id=?
                """,
                (max_usd, self._json(runtime), now, run_id),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    @staticmethod
    def _route_validation(
        route: str, request: Mapping[str, Any], response: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if route == "openrouter":
            from jev_openrouter import validate_jev_response

            return validate_jev_response(request, response)
        return validate_response(request, response)

    def _route_classification(
        self, route: str, response: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if route == "openrouter":
            from jev_openrouter import classify_jev_response

            return classify_jev_response(response, self.policy)
        return classify(response, self.policy)

    @staticmethod
    def _route_cost(
        route: str, response: Mapping[str, Any], input_tokens: int
    ) -> float:
        if route == "openrouter":
            from jev_openrouter import response_cost_usd

            return float(response_cost_usd(response, input_tokens, route))
        return float(input_tokens) * INPUT_PRICE_PER_TOKEN

    @staticmethod
    def _raw_response(response: Any) -> str | None:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            with contextlib.suppress(UnicodeDecodeError):
                return content.decode("utf-8")
        if isinstance(content, str):
            return content
        return None

    def _mark_http_start(self, attempt_id: int) -> str:
        tx = self._tx()
        try:
            row = tx.execute(
                "SELECT run_id, route FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            now = time.time()
            route = row["route"] or "typesafe"
            tx.execute(
                """
                UPDATE route_budgets
                SET active_http=active_http+1,
                    peak_active_http=MAX(peak_active_http,active_http+1),
                    requests_started=requests_started+1, updated_at=?
                WHERE run_id=? AND route=?
                """,
                (now, row["run_id"], route),
            )
            tx.execute(
                "UPDATE attempts SET sent_at=? WHERE attempt_id=?", (now, attempt_id)
            )
            self._commit(tx)
            return route
        except Exception:
            self._rollback(tx)
            raise

    def _mark_http_end(self, attempt_id: int) -> None:
        tx = self._tx()
        try:
            row = tx.execute(
                "SELECT run_id, route FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                tx.execute(
                    """
                    UPDATE route_budgets SET active_http=MAX(0,active_http-1),
                        updated_at=? WHERE run_id=? AND route=?
                    """,
                    (time.time(), row["run_id"], row["route"] or "typesafe"),
                )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _stop_route(
        self,
        run_id: str,
        route: str,
        reason: str,
        retry_after: str | None,
        resume_after: float | None,
    ) -> None:
        tx = self._tx()
        try:
            current = tx.execute(
                "SELECT retry_after,resume_after FROM route_budgets "
                "WHERE run_id=? AND route=?",
                (run_id, route),
            ).fetchone()
            old_epoch = current["resume_after"] if current else None
            old_raw = current["retry_after"] if current else None
            if old_epoch is not None and (
                resume_after is None or float(old_epoch) >= float(resume_after)
            ):
                chosen_epoch, chosen_raw = old_epoch, old_raw
            else:
                chosen_epoch, chosen_raw = resume_after, retry_after
            now = time.time()
            tx.execute(
                """
                UPDATE route_budgets SET status='stopped', reason=?,
                    retry_after=?, resume_after=?, updated_at=?
                WHERE run_id=? AND route=?
                """,
                (reason, chosen_raw, chosen_epoch, now, run_id, route),
            )
            # The route is stopped, but the run remains live so another route
            # can continue claiming pending stages.  The final executor pass
            # derives overall paused/completed state from all route snapshots.
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _mark_routes_completed(self, run_id: str) -> None:
        tx = self._tx()
        try:
            tx.execute(
                """
                UPDATE route_budgets SET status='completed', updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (time.time(), run_id),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    async def execute(
        self,
        run_id: str,
        api_key: str,
        max_usd: float,
        concurrency: int = 4,
        requests_per_minute: float = 600,
        timeout_seconds: float = 45,
        max_attempts: int = 3,
        transport: Any = None,
        *,
        openrouter_api_key: str | None = None,
        openrouter_max_usd: float = 10.0,
        openrouter_concurrency: int = 256,
        openrouter_requests_per_minute: float = 30_000.0,
        openrouter_transport: Any = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a nonempty string")
        (
            max_usd,
            concurrency,
            requests_per_minute,
            timeout_seconds,
            max_attempts,
        ) = self._validate_runtime(
            max_usd, concurrency, requests_per_minute, timeout_seconds, max_attempts
        )
        (
            openrouter_max_usd,
            openrouter_concurrency,
            openrouter_requests_per_minute,
            _,
            _,
        ) = self._validate_runtime(
            openrouter_max_usd,
            openrouter_concurrency,
            openrouter_requests_per_minute,
            timeout_seconds,
            max_attempts,
        )
        openrouter_enabled = openrouter_api_key is not None
        if openrouter_enabled and (
            not isinstance(openrouter_api_key, str) or not openrouter_api_key.strip()
        ):
            raise ValueError("openrouter_api_key must be a nonempty string")
        run = self._run_row(run_id)
        if run["configuration_key"] != self._configuration_key:
            raise ValueError(
                "run configuration_key does not match current taxonomy/policy"
            )
        stored_manifest = json.loads(run["manifest_json"])
        if stored_manifest.get("configuration_key") != self._configuration_key:
            raise ValueError(
                "run manifest configuration_key does not match current taxonomy/policy"
            )
        self._ensure_route_budgets(
            run_id,
            max_usd=max_usd,
            concurrency=concurrency,
            requests_per_minute=requests_per_minute,
            openrouter_enabled=openrouter_enabled,
            openrouter_max_usd=openrouter_max_usd,
            openrouter_concurrency=openrouter_concurrency,
            openrouter_requests_per_minute=openrouter_requests_per_minute,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        self._active_run_id = run_id
        self._stop_event.clear()
        self._stop_reason = None
        self._route_stop_events = {
            "typesafe": threading.Event(),
            "openrouter": threading.Event(),
        }
        direct_pacer = _Pacer(requests_per_minute)
        openrouter_rate = (
            self._openrouter_rate
            if self._openrouter_rate is not None
            else openrouter_requests_per_minute
        )
        openrouter_pacer = _Pacer(openrouter_rate)
        self._route_pacers = {
            "typesafe": direct_pacer,
            "openrouter": openrouter_pacer,
        }
        workers: list[asyncio.Task[Any]] = []

        async def drive(direct_client: Any, or_client: Any | None) -> None:
            nonlocal workers
            workers = [
                asyncio.create_task(
                    self._run_worker(
                        direct_client,
                        run_id,
                        max_usd,
                        max_attempts,
                        direct_pacer,
                        "typesafe",
                    )
                )
                for _ in range(concurrency)
            ]
            if openrouter_enabled and or_client is not None:
                workers.extend(
                    asyncio.create_task(
                        self._run_worker(
                            or_client,
                            run_id,
                            openrouter_max_usd,
                            max_attempts,
                            openrouter_pacer,
                            "openrouter",
                        )
                    )
                    for _ in range(openrouter_concurrency)
                )
            results = await asyncio.gather(*workers, return_exceptions=True)
            route_results = ["typesafe"] * concurrency
            if openrouter_enabled:
                route_results.extend(["openrouter"] * openrouter_concurrency)
            for route, result in zip(route_results, results):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self._stop_route(run_id, route, "worker_error", None, None)
                    self._route_stop_events[route].set()

        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=timeout_seconds,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as direct_client:
                if openrouter_enabled:
                    if openrouter_transport is None:
                        from jev_openrouter import OpenRouterClient

                        openrouter_transport = OpenRouterClient(
                            api_key=openrouter_api_key,
                            timeout_seconds=timeout_seconds,
                            concurrency=openrouter_concurrency,
                        )
                    async with _client_context(openrouter_transport) as or_client:
                        await drive(direct_client, or_client)
                else:
                    await drive(direct_client, None)
        finally:
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
            self._active_run_id = None

        current = self._run_row(run_id)
        if current["status"] == "running":
            remaining = self._ensure_open().execute(
                "SELECT COUNT(*) FROM items WHERE run_id=? AND stage!='done'",
                (run_id,),
            ).fetchone()[0]
            if remaining == 0:
                self._mark_routes_completed(run_id)
                self._update_run_state(run_id, "completed", None, None, None)
            elif self._stop_event.is_set():
                self._update_run_state(
                    run_id, "paused", self._stop_reason or "operator_stop", None, None
                )
            else:
                routes = self.route_status(run_id)
                stopped = [
                    state for state in routes.values() if state["status"] == "stopped"
                ]
                exhausted = self._ensure_open().execute(
                    """
                    SELECT COUNT(*) FROM attempts
                    WHERE run_id=? AND attempt_no>=?
                      AND state IN ('retryable_error','invalid_response')
                    """,
                    (run_id, max_attempts),
                ).fetchone()[0]
                if stopped:
                    state = stopped[0]
                    reason = state["reason"]
                    retry_after = state["retry_after"]
                    resume_after = state["resume_after_epoch"]
                else:
                    reason = "max_attempts_exhausted" if exhausted else "no_progress"
                    retry_after = None
                    resume_after = None
                self._update_run_state(
                    run_id, "paused", reason, retry_after, resume_after
                )
        return self.status(run_id)

    async def _run_worker(
        self,
        client: Any,
        run_id: str,
        max_usd: float,
        max_attempts: int,
        pacer: _Pacer,
        route: str = "typesafe",
    ) -> None:
        route_event = self._route_stop_events.setdefault(route, threading.Event())
        while not self._stop_event.is_set() and not route_event.is_set():
            claimed = self._claim_item(run_id)
            if claimed is None:
                return
            content_version, stage, text, company_ids = claimed
            pending = (
                "categorization_pending"
                if stage == "categorization_inflight"
                else "sentiment_pending"
            )
            try:
                if stage == "categorization_inflight":
                    request = classification_request(text, self.taxonomy, self.policy)
                    kind = "categorization"
                else:
                    request = sentiment_request(
                        text, company_ids, self.taxonomy, self.policy
                    )
                    kind = "sentiment"
                req_hash = request_key(request)
                self._set_request_hash(run_id, content_version, stage, req_hash)
                cached = self._cache_row(req_hash)
                if cached is not None:
                    raw_cached = cached["response_json"]
                    try:
                        response = json.loads(raw_cached)
                        from jev_openrouter import validate_jev_response, classify_jev_response

                        usage = validate_jev_response(request, response)
                        usage = {
                            "input_tokens": int(usage["input_tokens"]),
                            "output_tokens": int(usage["output_tokens"]),
                        }
                        result = (
                            classify_jev_response(response, self.policy)
                            if kind == "categorization"
                            else self._sentiment_result(response, company_ids)
                        )
                    except Exception:
                        tx = self._tx()
                        try:
                            tx.execute(
                                "DELETE FROM response_cache WHERE request_hash=?",
                                (req_hash,),
                            )
                            self._commit(tx)
                        except Exception:
                            self._rollback(tx)
                            raise
                    else:
                        if kind == "categorization":
                            self._apply_category_result(
                                run_id,
                                content_version,
                                req_hash,
                                response,
                                usage,
                                result,
                                route=route,
                                raw_response=raw_cached,
                            )
                        else:
                            self._apply_sentiment_result(
                                run_id,
                                content_version,
                                req_hash,
                                response,
                                usage,
                                result,
                                route=route,
                                raw_response=raw_cached,
                            )
                        continue
                try:
                    attempt_id = self._budget_begin_attempt(
                        run_id,
                        content_version,
                        stage,
                        req_hash,
                        max_usd,
                        route,
                        request,
                    )
                except _BudgetWait:
                    self._set_item_stage(run_id, content_version, pending)
                    # The lifetime allowance only shrinks. Existing funded
                    # workers will recycle their reservations; retire excess
                    # workers instead of churning durable claims while waiting.
                    return
                except _BudgetUnavailable:
                    self._set_item_stage(
                        run_id,
                        content_version,
                        pending,
                        error="lifetime budget exhausted",
                    )
                    self._stop_route(run_id, route, "budget_exhausted", None, None)
                    route_event.set()
                    return
                except _RouteUnavailable:
                    self._set_item_stage(
                        run_id, content_version, pending, error="route stopped"
                    )
                    return
                if self._stop_event.is_set() or route_event.is_set():
                    self._finish_attempt(
                        attempt_id,
                        state="canceled",
                        error_kind="operator_stop",
                        diagnostic="dispatch stopped before request was sent",
                    )
                    self._set_item_stage(run_id, content_version, pending)
                    return
                await pacer.wait()
                if self._stop_event.is_set() or route_event.is_set():
                    self._finish_attempt(
                        attempt_id,
                        state="canceled",
                        error_kind="operator_stop",
                        diagnostic="dispatch stopped before request was sent",
                    )
                    self._set_item_stage(run_id, content_version, pending)
                    return
                await self._send_one(
                    client,
                    run_id,
                    content_version,
                    stage,
                    kind,
                    company_ids,
                    request,
                    req_hash,
                    attempt_id,
                    max_attempts,
                    route=route,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stop_route(run_id, route, "local_validation_error", None, None)
                route_event.set()
                self._set_item_stage(
                    run_id, content_version, pending, error=self._safe_error(exc)
                )
                return
    def _claim_item(self, run_id: str) -> tuple[str, str, str, list[str]] | None:
        tx = self._tx()
        try:
            row = tx.execute(
                """
                SELECT i.content_version, i.stage, c.text, i.categorization_result_json
                FROM items i JOIN contents c USING(content_version)
                WHERE i.run_id=? AND i.stage IN ('categorization_pending','sentiment_pending')
                  AND NOT EXISTS (
                    SELECT 1 FROM attempts a
                    WHERE a.run_id=i.run_id AND a.content_version=i.content_version
                      AND a.stage=CASE i.stage
                        WHEN 'categorization_pending' THEN 'categorization_inflight'
                        ELSE 'sentiment_inflight' END
                      AND a.route='openrouter'
                      AND (a.billing_uncertain=1 OR a.state='invalid_response')
                  )
                ORDER BY i.content_version LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                self._commit(tx)
                return None
            old_stage = row["stage"]
            inflight = (
                "categorization_inflight"
                if old_stage == "categorization_pending"
                else "sentiment_inflight"
            )
            tx.execute(
                "UPDATE items SET stage=?, updated_at=? WHERE run_id=? AND content_version=?",
                (inflight, time.time(), run_id, row["content_version"]),
            )
            company_ids: list[str] = []
            if old_stage == "sentiment_pending" and row["categorization_result_json"]:
                with contextlib.suppress(Exception):
                    result = json.loads(row["categorization_result_json"])
                    company_ids = [str(x) for x in result.get("companies", [])]
            self._commit(tx)
            return row["content_version"], inflight, row["text"], company_ids
        except Exception:
            self._rollback(tx)
            raise

    def _set_request_hash(
        self, run_id: str, content_version: str, stage: str, req_hash: str
    ) -> None:
        column = (
            "categorization_request_hash"
            if stage == "categorization_inflight"
            else "sentiment_request_hash"
        )
        tx = self._tx()
        try:
            tx.execute(
                f"UPDATE items SET {column}=?, updated_at=? WHERE run_id=? AND content_version=?",
                (req_hash, time.time(), run_id, content_version),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    async def _send_one(
        self,
        client: Any,
        run_id: str,
        content_version: str,
        stage: str,
        kind: str,
        company_ids: list[str],
        request: Mapping[str, Any],
        req_hash: str,
        attempt_id: int,
        max_attempts: int,
        *,
        route: str = "typesafe",
    ) -> None:
        pending = (
            "categorization_pending"
            if kind == "categorization"
            else "sentiment_pending"
        )
        route_event = self._route_stop_events.setdefault(route, threading.Event())

        def stop_route(
            reason: str, retry_raw: str | None = None, retry_epoch: float | None = None
        ) -> None:
            self._stop_route(run_id, route, reason, retry_raw, retry_epoch)
            route_event.set()

        endpoint = API_ENDPOINT
        if route == "openrouter":
            from jev_openrouter import OPENROUTER_ENDPOINT

            endpoint = OPENROUTER_ENDPOINT
        started_http = False
        try:
            self._mark_http_start(attempt_id)
            started_http = True
            try:
                response = await client.post(endpoint, json=request)
            except asyncio.CancelledError:
                self._finish_attempt(
                    attempt_id,
                    state="ambiguous",
                    error_kind="cancelled",
                    diagnostic="local request cancellation; provider billing is unknown",
                    uncertain=True,
                )
                self._set_item_stage(
                    run_id,
                    content_version,
                    pending,
                    error="request cancelled; provider billing is unknown",
                )
                if route == "openrouter":
                    stop_route("request_cancelled")
                raise
            except Exception as exc:
                attempt_no = self._attempt_no(attempt_id)
                if route == "openrouter":
                    self._finish_attempt(
                        attempt_id,
                        state="ambiguous",
                        error_kind="network",
                        diagnostic=self._safe_error(exc),
                        uncertain=True,
                    )
                    self._set_item_stage(
                        run_id, content_version, pending, error=self._safe_error(exc)
                    )
                    stop_route("provider_transport_error")
                    return
                self._finish_attempt(
                    attempt_id,
                    state=(
                        "retryable_error"
                        if attempt_no < max_attempts
                        else "ambiguous"
                    ),
                    error_kind="network",
                    diagnostic=self._safe_error(exc),
                    uncertain=True,
                )
                if attempt_no >= max_attempts:
                    self._set_item_stage(
                        run_id, content_version, pending, error=self._safe_error(exc)
                    )
                    stop_route("max_attempts_exhausted")
                else:
                    await asyncio.sleep(min(2.0 ** max(0, attempt_no - 1), 8.0))
                    self._set_item_stage(
                        run_id, content_version, pending, error=self._safe_error(exc)
                    )
                return
            finally:
                if started_http:
                    self._mark_http_end(attempt_id)
                    started_http = False

            status = int(response.status_code)
            retry_raw, retry_epoch = self._parse_retry_after(
                response.headers.get("Retry-After")
            )
            body = self._response_diagnostic(response)
            raw_body = self._raw_response(response)
            if self._is_credit_error(body):
                self._finish_attempt(
                    attempt_id,
                    state="provider_stop",
                    raw_response=raw_body,
                    http_status=status,
                    error_kind="credit",
                    diagnostic=body,
                    retry_after=retry_raw,
                    uncertain=status >= 500,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error=body
                )
                stop_route("credit_exhausted", retry_raw, retry_epoch)
                return
            if status in (429, 529, 402):
                error_kind = (
                    "rate_limit"
                    if status == 429
                    else ("credit" if status == 402 else "provider_busy")
                )
                self._finish_attempt(
                    attempt_id,
                    raw_response=raw_body,
                    state="provider_stop",
                    http_status=status,
                    error_kind=error_kind,
                    diagnostic=body,
                    retry_after=retry_raw,
                    uncertain=status == 529,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error=body
                )
                stop_route(
                    {
                        429: "rate_limited",
                        529: "provider_busy",
                        402: "credit_exhausted",
                    }[status],
                    retry_raw,
                    retry_epoch,
                )
                return
            if 400 <= status < 500:
                self._finish_attempt(
                    attempt_id,
                    raw_response=raw_body,
                    state="provider_stop",
                    http_status=status,
                    error_kind="api_client_error",
                    diagnostic=body,
                    uncertain=False,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error=body
                )
                stop_route("api_client_error", retry_raw, retry_epoch)
                return
            if status >= 500:
                attempt_no = self._attempt_no(attempt_id)
                if route == "openrouter":
                    self._finish_attempt(
                        attempt_id,
                        raw_response=raw_body,
                        state="ambiguous",
                        http_status=status,
                        error_kind="api_server_error",
                        diagnostic=body,
                        retry_after=retry_raw,
                        uncertain=True,
                    )
                    self._set_item_stage(
                        run_id, content_version, pending, error=body
                    )
                    stop_route(
                        "provider_busy" if status == 529 else "provider_server_error",
                        retry_raw,
                        retry_epoch,
                    )
                    return
                self._finish_attempt(
                    attempt_id,
                    raw_response=raw_body,
                    state=(
                        "retryable_error"
                        if status != 529 and attempt_no < max_attempts
                        else "ambiguous"
                    ),
                    http_status=status,
                    error_kind="api_server_error",
                    diagnostic=body,
                    retry_after=retry_raw,
                    uncertain=True,
                )
                if status == 529 or attempt_no >= max_attempts:
                    self._set_item_stage(run_id, content_version, pending, error=body)
                    stop_route(
                        "provider_busy" if status == 529 else "max_attempts_exhausted",
                        retry_raw,
                        retry_epoch,
                    )
                else:
                    await asyncio.sleep(min(2.0 ** max(0, attempt_no - 1), 8.0))
                    self._set_item_stage(run_id, content_version, pending, error=body)
                return

            try:
                payload = response.json()
            except Exception as exc:
                self._finish_attempt(
                    attempt_id,
                    state="invalid_response",
                    http_status=status,
                    error_kind="invalid_json",
                    diagnostic=self._safe_error(exc),
                    raw_response=raw_body,
                    uncertain=True,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error="provider returned invalid JSON"
                )
                if route == "openrouter":
                    stop_route("local_validation_error")
                elif self._attempt_no(attempt_id) >= max_attempts:
                    stop_route("max_attempts_exhausted")
                return

            usage: dict[str, int] | None = None
            try:
                if route == "openrouter":
                    from jev_openrouter import OPENROUTER_RESOLVED_MODEL

                    if payload.get("model") != OPENROUTER_RESOLVED_MODEL:
                        raise ValueError("OpenRouter returned an unexpected resolved Jev model")
                raw_usage = self._route_validation(route, request, payload)
                if not isinstance(raw_usage, Mapping):
                    raise ValueError("protocol validator did not return usage mapping")
                usage = {
                    "input_tokens": int(raw_usage["input_tokens"]),
                    "output_tokens": int(raw_usage["output_tokens"]),
                }
                result = (
                    self._route_classification(route, payload)
                    if kind == "categorization"
                    else self._sentiment_result(payload, company_ids)
                )
            except Exception as exc:
                fallback_usage = self._extract_usage(payload)
                self._finish_attempt(
                    attempt_id,
                    state="invalid_response",
                    response=payload,
                    raw_response=raw_body,
                    usage=fallback_usage,
                    http_status=status,
                    error_kind="protocol_validation",
                    diagnostic=self._safe_error(exc),
                    uncertain=route == "openrouter" or fallback_usage is None,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error=self._safe_error(exc)
                )
                if route == "openrouter":
                    stop_route("local_validation_error")
                elif self._attempt_no(attempt_id) >= max_attempts:
                    stop_route("max_attempts_exhausted")
                return

            assert usage is not None
            try:
                actual_cost = self._route_cost(
                    route, payload, usage["input_tokens"]
                )
            except Exception as exc:
                self._finish_attempt(
                    attempt_id,
                    state="invalid_response",
                    response=payload,
                    raw_response=raw_body,
                    usage=usage,
                    actual_cost_usd=None,
                    http_status=status,
                    error_kind="unknown_cost",
                    diagnostic=self._safe_error(exc),
                    uncertain=True,
                )
                self._set_item_stage(
                    run_id, content_version, pending, error=self._safe_error(exc)
                )
                if route == "openrouter":
                    stop_route("unknown_cost")
                else:
                    stop_route("max_attempts_exhausted")
                return
            self._complete_success(
                attempt_id,
                req_hash,
                kind,
                request,
                payload,
                usage,
                result,
                actual_cost_usd=actual_cost,
                raw_response=raw_body,
            )
        finally:
            if started_http:
                with contextlib.suppress(Exception):
                    self._mark_http_end(attempt_id)
    @staticmethod
    def _sentiment_result(response: Mapping[str, Any], company_ids: list[str]) -> Any:
        answers = response["answers"]
        return {company: answers[company] for company in company_ids}

    def _attempt_no(self, attempt_id: int) -> int:
        row = (
            self._ensure_open()
            .execute(
                "SELECT attempt_no FROM attempts WHERE attempt_id=?", (attempt_id,)
            )
            .fetchone()
        )
        return int(row[0]) if row else 1

    @staticmethod
    def _response_diagnostic(response: httpx.Response) -> str:
        text = response.text
        if len(text) > 500:
            text = text[:500] + "…"
        return f"HTTP {response.status_code}: {text}"

    @staticmethod
    def _is_credit_error(diagnostic: str) -> bool:
        text = diagnostic.casefold()
        markers = (
            "insufficient credit",
            "insufficient_credit",
            "insufficient_quota",
            "quota exhausted",
            "quota_exceeded",
            "credit exhausted",
            "credit_exhausted",
            "out of credits",
            "payment required",
            "billing limit",
            "billing_limit",
        )
        return any(marker in text for marker in markers)

    def _pause_with_retry(
        self,
        run_id: str,
        reason: str,
        retry_raw: str | None,
        retry_epoch: float | None,
    ) -> None:
        # Several in-flight workers may discover a stop condition at once.
        # Never replace a longer provider backoff with a shorter/missing one.
        tx = self._tx()
        try:
            current = tx.execute(
                "SELECT retry_after, resume_after FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            old_epoch = current["resume_after"] if current else None
            old_raw = current["retry_after"] if current else None
            if old_epoch is not None and (
                retry_epoch is None or float(old_epoch) >= float(retry_epoch)
            ):
                chosen_epoch = old_epoch
                chosen_raw = old_raw
            else:
                chosen_epoch = retry_epoch
                chosen_raw = retry_raw
            tx.execute(
                """
                UPDATE runs SET status='paused', reason=?, retry_after=?,
                    resume_after=?, updated_at=? WHERE run_id=?
                """,
                (reason, chosen_raw, chosen_epoch, time.time(), run_id),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def _update_run_state(
        self,
        run_id: str,
        status: str,
        reason: str | None,
        retry_after: str | None,
        resume_after: float | None,
    ) -> None:
        tx = self._tx()
        try:
            tx.execute(
                "UPDATE runs SET status=?, reason=?, retry_after=?, resume_after=?, updated_at=? WHERE run_id=?",
                (status, reason, retry_after, resume_after, time.time(), run_id),
            )
            self._commit(tx)
        except Exception:
            self._rollback(tx)
            raise

    def set_openrouter_rate(self, requests_per_minute: float) -> None:
        """Change only the live OpenRouter pacer and persist its setting."""
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, (int, float))
            or not math.isfinite(float(requests_per_minute))
            or float(requests_per_minute) <= 0
        ):
            raise ValueError("requests_per_minute must be a finite positive number")
        rate = float(requests_per_minute)
        self._openrouter_rate = rate
        pacer = self._route_pacers.get("openrouter")
        if pacer is not None:
            pacer.set_rate(rate)
        run_id = self._active_run_id
        if run_id is not None and self._conn is not None:
            tx = self._tx()
            try:
                tx.execute(
                    """
                    UPDATE route_budgets
                    SET requests_per_minute=?, updated_at=?
                    WHERE run_id=? AND route='openrouter'
                    """,
                    (rate, time.time(), run_id),
                )
                self._commit(tx)
            except Exception:
                self._rollback(tx)
                raise

    def request_openrouter_stop(self, reason: str = "operator_stop") -> None:
        """Stop only OpenRouter dispatch; TypeSafe workers continue draining."""
        safe_reason = self._safe_error(reason, 160) or "operator_stop"
        event = self._route_stop_events.get("openrouter")
        if event is not None:
            event.set()
        run_id = self._active_run_id
        if run_id is not None and self._conn is not None:
            self._stop_route(run_id, "openrouter", safe_reason, None, None)

    def request_stop(self, reason: str = "operator_stop") -> None:
        """Stop dispatching new requests on every route; drain in-flight calls."""
        self._stop_reason = self._safe_error(reason, 160) or "operator_stop"
        self._stop_event.set()
        for event in self._route_stop_events.values():
            event.set()
        run_id = self._active_run_id
        if run_id is not None and self._conn is not None:
            for route in ("typesafe", "openrouter"):
                with contextlib.suppress(Exception):
                    self._stop_route(run_id, route, self._stop_reason, None, None)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export(self, run_id: str, path: Path) -> int:
        run = self._run_row(run_id)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cursor = self._ensure_open().execute(
            """
            SELECT i.* FROM items i
            WHERE i.run_id=? ORDER BY i.content_version
            """,
            (run_id,),
        )
        # Write beside the destination and link it into place.  link(2) fails
        # instead of replacing a user file, unlike os.replace.
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                for row in cursor:
                    cat_result = (
                        json.loads(row["categorization_result_json"])
                        if row["categorization_result_json"]
                        else None
                    )
                    sent_result = (
                        json.loads(row["sentiment_result_json"])
                        if row["sentiment_result_json"]
                        else None
                    )
                    if row["stage"] == "done" and isinstance(cat_result, Mapping):
                        if (
                            cat_result.get("status") == "accepted"
                            and sent_result is not None
                        ):
                            export_status = "ready"
                        elif cat_result.get("status") == "others":
                            export_status = "others"
                        else:
                            export_status = row["stage"]
                    else:
                        export_status = row["stage"]
                    record = {
                        "run_id": run_id,
                        "content_version": row["content_version"],
                        "status": export_status,
                        "configuration_key": run["configuration_key"],
                        "last_error": row["last_error"],
                        "categorization": {
                            "request_hash": row["categorization_request_hash"],
                            "result": cat_result,
                            "response": (
                                json.loads(row["categorization_response_json"])
                                if row["categorization_response_json"]
                                else None
                            ),
                            "usage": (
                                json.loads(row["categorization_usage_json"])
                                if row["categorization_usage_json"]
                                else None
                            ),
                        },
                        "company_sentiment": {
                            "request_hash": row["sentiment_request_hash"],
                            "result": sent_result,
                            "response": (
                                json.loads(row["sentiment_response_json"])
                                if row["sentiment_response_json"]
                                else None
                            ),
                            "usage": (
                                json.loads(row["sentiment_usage_json"])
                                if row["sentiment_usage_json"]
                                else None
                            ),
                        },
                    }
                    output.write(self._json(record) + "\n")
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise FileExistsError(
                    f"refusing to overwrite existing export: {destination}"
                )
            os.unlink(temporary)
            return count
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise


def read_status(db_path: Path, run_id: str) -> dict[str, Any]:
    """Read a checkpoint without taking the engine's exclusive writer lock.

    This is intentionally read-only: unlike ``Engine.__enter__`` it does not
    create the database, enable WAL, or recover interrupted attempts.  SQLite
    WAL readers can therefore serve the CLI while an execution holds the
    writer lock.
    """
    path = Path(db_path).resolve()
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        return Engine._status(conn, run_id)
    finally:
        conn.close()


__all__ = [
    "Engine",
    "read_status",
    "API_ENDPOINT",
    "INPUT_PRICE_PER_TOKEN",
    "RESERVATION_TOKENS",
]
