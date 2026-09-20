"""Runtime for the local shared Bluesky firehose service.

The runtime deliberately keeps the source, routing, and model scheduler separate:
one Jetstream connection captures every event, a small DuckDB worker routes post
mutations for each enabled automation, and a fair scheduler feeds the durable
Jev engine one automation at a time.  The Store is the authority for all source
and automation state; this module contains no service API or configuration
persistence.
"""

from __future__ import annotations

import asyncio
import httpx
from collections.abc import Mapping, Sequence
import contextlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from common import DEFAULT_SOURCE


_EVENT_BATCH_LIMIT = 250
_EVENT_BATCH_SECONDS = 0.25
_QUEUE_LIMIT = 2_000
_LOOP_WAIT_SECONDS = 0.25
_RETRY_INITIAL_SECONDS = 0.5
_RETRY_MAX_SECONDS = 30.0


class _GlobalPacer:
    """One monotonic request gate shared by all native Engine executions."""

    def __init__(self, requests_per_minute: float) -> None:
        rate = float(requests_per_minute)
        self.interval = 0.0 if not math.isfinite(rate) or rate <= 0 else 60.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
            delay = at - now
            if delay > 0:
                await asyncio.sleep(delay)


class _PacingTransport(httpx.AsyncBaseTransport):
    """httpx async transport that preserves a global pacing gate."""

    def __init__(
        self, transport: Any, pacer: _GlobalPacer, close_underlying: bool
    ) -> None:
        self._transport = transport
        self._pacer = pacer
        self._close_underlying = close_underlying

    async def handle_async_request(self, request: Any) -> Any:
        await self._pacer.wait()
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        # Caller-owned transports (notably MockTransport in offline smoke runs)
        # are reused for every bounded batch and must not be closed by Engine.
        if self._close_underlying:
            close = getattr(self._transport, "aclose", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


class Runtime:
    """Run one unfiltered Jetstream collector and all local automations."""

    def __init__(
        self,
        store: Any,
        state_dir: Path,
        source_url: str = DEFAULT_SOURCE,
        api_key: str | None = None,
        concurrency: int = 4,
        requests_per_minute: float = 60,
        batch_size: int = 32,
        transport: Any = None,
    ) -> None:
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            raise ValueError("concurrency must be a positive integer")
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, (int, float))
            or not math.isfinite(float(requests_per_minute))
            or float(requests_per_minute) <= 0
        ):
            raise ValueError("requests_per_minute must be a finite positive number")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or batch_size < 1
        ):
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(source_url, str) or not source_url.strip():
            raise ValueError("source_url must be a nonempty string")
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key.strip()
        ):
            raise ValueError("api_key must be a nonempty string or None")

        self.store = store
        self.state_dir = Path(state_dir)
        self.source_url = source_url
        self.api_key = api_key
        self.concurrency = concurrency
        self.requests_per_minute = float(requests_per_minute)
        self.batch_size = batch_size
        self.transport = transport

        self._wake_event = asyncio.Event()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._active_engine: Any | None = None
        self._active_automation_id: str | None = None
        self._active_max_usd: float | None = None
        self._rr_index = 0
        self._blocked: set[str] = set()
        self._capture_only: set[str] = set()
        self._credentials_missing: set[str] = set()
        self._router_connection: Any | None = None
        self._filter_sql: dict[str, str] = {}
        self._pacer = _GlobalPacer(self.requests_per_minute)
        self._running = False
        self._loop_stop: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Public lifecycle/control surface
    # ------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """Run collector, durable ingest, routing, and fair enrichment loops."""
        if self._running:
            raise RuntimeError("Runtime.run() is already active")
        self._running = True
        self._loop_stop = stop
        self._wake_event.clear()
        self._blocked = {
            auto["id"]
            for auto in self.store.list_automations()
            if auto["worker_state"] in {"error", "budget_exhausted", "capture_only"}
            or (auto["worker_state"] == "credentials_missing" and self.api_key is None)
        }
        self._capture_only.clear()
        self._credentials_missing.clear()
        self._queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        source_bind_error: Exception | None = None
        try:
            try:
                self.store.set_source(self.source_url)
            except Exception as error:
                source_bind_error = error
                self._set_source_status("stopped", error)
                self._log("cannot bind source endpoint", error)
                return
            tasks = [
                asyncio.create_task(self._collector(stop), name="bluesky-collector"),
                asyncio.create_task(self._ingest_loop(stop), name="bluesky-ingest"),
                asyncio.create_task(self._router_loop(stop), name="bluesky-router"),
                asyncio.create_task(
                    self._scheduler_loop(stop), name="bluesky-scheduler"
                ),
            ]
            stop_task = asyncio.create_task(stop.wait(), name="bluesky-stop-wait")
            tasks.append(stop_task)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # A worker is expected to live for the daemon lifetime.  If an
            # unexpected task exits, surface its exception and stop the others
            # rather than silently running a partial service.
            if stop_task not in done:
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        self._log("runtime worker failed", error)
                        source_bind_error = error
                        raise error
                stop.set()
        finally:
            stop.set()
            if self._active_engine is not None:
                with contextlib.suppress(Exception):
                    self._active_engine.request_stop("shutdown")
            # Collector/router/scheduler are cancellation-safe.  Let ingest
            # flush already queued events briefly before cancellation so a
            # normal stop does not unnecessarily replay the same inclusive
            # source cursor on the next start.
            current = locals().get("tasks", [])
            for task in current:
                if task is not locals().get("stop_task") and not task.done():
                    if task.get_name() == "bluesky-ingest":
                        continue
                    task.cancel()
            ingest = next(
                (task for task in current if task.get_name() == "bluesky-ingest"), None
            )
            if ingest is not None and not ingest.done():
                try:
                    await asyncio.wait_for(ingest, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    ingest.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await ingest
            for task in current:
                if not task.done():
                    task.cancel()
            if current:
                await asyncio.gather(*current, return_exceptions=True)
            self._active_engine = None
            self._active_automation_id = None
            self._active_max_usd = None
            self._close_router()
            with contextlib.suppress(Exception):
                if self.store.source_status()["status"] != "gap":
                    self.store.set_source_status(
                        "stopped",
                        None if source_bind_error is None else str(source_bind_error),
                    )
            self._loop_stop = None
            self._running = False

    def wake(self) -> None:
        """Wake control loops and stop native work only for changed authorization."""
        # Controls reset only the targeted automation's durable worker_state.
        # Mirror that state instead of retrying unrelated provider failures.
        for blocked_id in tuple(self._blocked):
            try:
                automation = self.store.get_automation(blocked_id)
                state = str(automation.get("worker_state") or "").casefold()
                cap = float(automation.get("max_usd", 0.0))
            except Exception as error:  # pragma: no cover - defensive control path
                self._log(
                    f"cannot inspect blocked automation {blocked_id} during wake", error
                )
                continue
            if state in {"idle", "running"} or (state == "capture_only" and cap != 0.0):
                self._blocked.discard(blocked_id)
                self._capture_only.discard(blocked_id)
                self._credentials_missing.discard(blocked_id)
        self._wake_event.set()
        engine = self._active_engine
        automation_id = self._active_automation_id
        if engine is None or automation_id is None:
            return
        # API controls call wake after Store has committed.  Read the current
        # enabled/cap state synchronously and stop an in-flight Engine only if
        # its authorization is no longer current.  In-flight reservations stay
        # durable in Engine and are not mistaken for new authorization.
        try:
            automation = self.store.get_automation(automation_id)
            enabled = bool(automation.get("enabled", False))
            current_cap = float(automation.get("max_usd", 0.0))
        except Exception as error:  # pragma: no cover - defensive control path
            with contextlib.suppress(Exception):
                engine.request_stop("automation_state_unavailable")
            self._log("cannot inspect active automation during wake", error)
            raise
        if (
            not enabled
            or self._active_max_usd is None
            or not math.isclose(
                current_cap, self._active_max_usd, rel_tol=0.0, abs_tol=0.0
            )
        ):
            with contextlib.suppress(Exception):
                engine.request_stop("automation_control_change")

    # ------------------------------------------------------------------
    # Source collector and durable ingestion
    # ------------------------------------------------------------------
    async def _collector(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect

        retry = 0
        while not stop.is_set():
            source = self._source_status()
            if source["status"] == "gap":
                await self._wait_wake(stop)
                continue
            cursor = source["cursor"]
            endpoint = self._cursor_url(cursor)
            gap_reason = None
            try:
                self._set_source_status("retrying")
                async with connect(
                    endpoint,
                    subprotocols=["xrpc.v1.json"],
                    max_size=None,
                ) as websocket:
                    retry = 0
                    self._set_source_status("connected")
                    while not stop.is_set():
                        event, error = self._decode_frame(await websocket.recv())
                        if error is not None:
                            gap_reason = f"Jetstream frame error: {error}"
                            break
                        if event is not None:
                            await self._queue.put(event)
            except Exception as error:
                if stop.is_set():
                    break
                gap_reason = self._cursor_gap_reason(error)
                if gap_reason is None:
                    retry += 1
                    delay = min(
                        _RETRY_MAX_SECONDS,
                        _RETRY_INITIAL_SECONDS * 2 ** min(retry - 1, 8),
                    )
                    self._set_source_status("retrying", error)
                    self._log("Jetstream connection failed; retrying", error)
                    await self._wait_or_stop(stop, delay)
                    continue
            if gap_reason is not None:
                # Finish earlier accepted frames before exposing the reset
                # control; otherwise a late flush could undo its cursor reset.
                await self._queue.join()
                self._record_notice(
                    "coverage_gap",
                    gap_reason,
                    {"cursor": self._source_status()["cursor"], "endpoint": endpoint},
                )
                self._set_source_status("gap", gap_reason)

    async def _ingest_loop(self, stop: asyncio.Event) -> None:
        pending: list[dict[str, Any]] = []
        retry_delay = _RETRY_INITIAL_SECONDS
        while not stop.is_set() or not self._queue.empty() or pending:
            if not pending:
                try:
                    pending.append(
                        await asyncio.wait_for(self._queue.get(), _EVENT_BATCH_SECONDS)
                    )
                except asyncio.TimeoutError:
                    continue
            deadline = asyncio.get_running_loop().time() + _EVENT_BATCH_SECONDS
            while len(pending) < _EVENT_BATCH_LIMIT:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    pending.append(await asyncio.wait_for(self._queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            try:
                self.store.ingest(pending)
            except Exception as error:
                self._set_source_status("retrying", error)
                self._log("durable event ingest failed; retaining batch", error)
                if stop.is_set():
                    break
                await self._wait_or_stop(stop, retry_delay)
                retry_delay = min(_RETRY_MAX_SECONDS, retry_delay * 2)
                continue
            for _ in pending:
                self._queue.task_done()
            pending.clear()
            retry_delay = _RETRY_INITIAL_SECONDS

    @staticmethod
    def _decode_frame(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            frame = raw if isinstance(raw, Mapping) else json.loads(raw)
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            return None, f"invalid JSON frame: {error}"
        if not isinstance(frame, Mapping):
            return None, "invalid non-object frame"

        # xrpc.v1.json uses a message envelope.  Error frames may be either an
        # envelope payload or a top-level error object, depending on server
        # version.
        kind = str(frame.get("$type", "")).casefold()
        if kind in {"error", "err"} or frame.get("error") is not None:
            details = frame.get("error") or frame.get("message") or frame
            return None, str(details).casefold()
        if kind == "message":
            payload = frame.get("payload")
            if not isinstance(payload, Mapping):
                return None, "message envelope has no object payload"
        elif kind == "info" or kind.endswith("#info"):
            payload = frame
        else:
            return None, f"unsupported Jetstream frame type {kind or '<missing>'}"
        payload_kind = str(payload.get("$type", "")).casefold()
        payload_name = str(
            payload.get("name") or payload.get("error") or payload.get("message") or ""
        )
        normalized_name = "".join(
            character for character in payload_name.casefold() if character.isalnum()
        )
        if payload_kind in {"error", "err"} or payload.get("error") is not None:
            details = payload.get("error") or payload.get("message") or payload
            return None, str(details).casefold()
        if payload_kind == "info" or payload_kind.endswith("#info"):
            if normalized_name == "outdatedcursor":
                return None, "outdatedcursor"
            return (
                None,
                f"unsupported Jetstream info frame {payload_name or '<missing>'}",
            )
        seq = payload.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            return None, "event frame has no nonnegative integer seq"
        event = dict(payload)
        return event, None

    # ------------------------------------------------------------------
    # Routing loop
    # ------------------------------------------------------------------
    async def _router_loop(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                did_work = False
                try:
                    summaries = self.store.list_automations()
                    for summary in summaries:
                        if not bool(summary.get("enabled", False)):
                            continue
                        automation_id = str(summary["id"])
                        try:
                            automation = self.store.get_automation(automation_id)
                            rows, through_seq = self.store.fetch_posts(
                                automation_id, self.batch_size
                            )
                            if rows is None:
                                rows = []
                            if not rows and through_seq is None:
                                continue
                            matches = self._screen_posts(automation, rows)
                            self.store.route_posts(automation_id, matches, through_seq)
                            prior_cursor = automation.get("routing_cursor")
                            advances = False
                            if through_seq is not None:
                                try:
                                    advances = prior_cursor is None or int(
                                        through_seq
                                    ) > int(prior_cursor)
                                except (TypeError, ValueError):
                                    advances = True
                            did_work = did_work or bool(rows) or advances
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            self._log(
                                f"routing failed for automation {automation_id}", error
                            )
                            with contextlib.suppress(Exception):
                                self.store.worker_status(
                                    automation_id,
                                    "error",
                                    str(error),
                                    {"stage": "routing"},
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._log("automation routing loop failed", error)
                if not did_work:
                    await self._wait_or_wake(stop, _LOOP_WAIT_SECONDS)
        finally:
            self._close_router()

    def _screen_posts(
        self, automation: Mapping[str, Any], rows: Sequence[Any]
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        compiled = automation.get("compiled")
        if not isinstance(compiled, Mapping):
            raise ValueError("automation has no compiled native configuration")
        native_filter = compiled.get("filter-config.json")
        if not isinstance(native_filter, Mapping):
            raise ValueError("compiled configuration has no filter-config.json")
        config_hash = automation["config_hash"]
        sql = self._filter_sql.get(config_hash)
        if sql is None:
            import filter_keywords

            # Deletions and empty mutations advance the routing cursor but
            # never become inference work, even if a Store retains a prior
            # body while recording a delete operation.
            source = "(SELECT * FROM _runtime_posts WHERE NOT deleted AND coalesce(body, '') <> '')"
            sql = filter_keywords.screen_sql(source, native_filter)
            self._filter_sql[config_hash] = sql
        connection = self._router_connection
        if connection is None:
            import duckdb

            connection = duckdb.connect(":memory:")
            connection.execute(
                """
                CREATE TEMP TABLE _runtime_posts (
                    seq BIGINT,
                    post_uri VARCHAR,
                    uri VARCHAR,
                    content_version VARCHAR,
                    body VARCHAR,
                    operation VARCHAR,
                    event_time VARCHAR,
                    observed_at VARCHAR,
                    cid VARCHAR,
                    deleted BOOLEAN
                )
                """
            )
            self._router_connection = connection
        connection.execute("DELETE FROM _runtime_posts")
        values: list[tuple[Any, ...]] = []
        for row in rows:
            values.append(
                (
                    row["seq"],
                    row["post_uri"],
                    row["post_uri"],
                    row["content_version"],
                    row["text"],
                    row["operation"],
                    row["event_time"],
                    row["observed_at"],
                    row["cid"],
                    row["deleted"],
                )
            )
        connection.executemany(
            "INSERT INTO _runtime_posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values
        )
        cursor = connection.execute(sql)
        names = [str(item[0]) for item in cursor.description]
        output: list[dict[str, Any]] = []
        for values_row in cursor.fetchall():
            data = dict(zip(names, values_row))
            data["text"] = data.pop("body")
            data["groups"] = data.pop("company_candidates")
            data["discovery"] = data.pop("discovery_match")
            output.append(data)
        return output

    def _close_router(self) -> None:
        connection, self._router_connection = self._router_connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()

    # ------------------------------------------------------------------
    # Fair native enrichment scheduler
    # ------------------------------------------------------------------
    async def _scheduler_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                summaries = [
                    summary
                    for summary in self.store.list_automations()
                    if bool(summary.get("enabled", False))
                ]
                if not summaries:
                    await self._wait_or_wake(stop, _LOOP_WAIT_SECONDS)
                    continue
                summaries.sort(key=lambda value: str(value.get("id", "")))
                if self._rr_index >= len(summaries):
                    self._rr_index = 0
                did_work = False
                start_index = self._rr_index
                for offset in range(len(summaries)):
                    index = (start_index + offset) % len(summaries)
                    summary = summaries[index]
                    automation_id = str(summary["id"])
                    if automation_id in self._blocked:
                        continue
                    result = await self._enrich_one(automation_id)
                    self._rr_index = (index + 1) % len(summaries)
                    if result:
                        did_work = True
                        break
                if not did_work:
                    await self._wait_or_wake(stop, _LOOP_WAIT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log("enrichment scheduler failed", error)
                await self._wait_or_wake(stop, _LOOP_WAIT_SECONDS)

    async def _enrich_one(self, automation_id: str) -> bool:
        automation = self.store.get_automation(automation_id)
        if not bool(automation.get("enabled", False)):
            return False
        max_usd = float(automation.get("max_usd", 0.0))
        pending = self.store.pending(automation_id, self.batch_size)
        if not pending:
            if automation["worker_state"] != "idle":
                self.store.worker_status(
                    automation_id, "idle", None, automation["engine_status"]
                )
            return False
        if max_usd == 0.0:
            self._capture_only.add(automation_id)
            self._blocked.add(automation_id)
            self.store.worker_status(
                automation_id, "capture_only", None, {"state": "capture_only"}
            )
            return False
        if self.api_key is None:
            self._credentials_missing.add(automation_id)
            self._blocked.add(automation_id)
            self.store.worker_status(
                automation_id,
                "credentials_missing",
                "TYPESAFE_API_KEY is not configured",
                {"state": "credentials_missing"},
            )
            return False

        compiled = automation.get("compiled")
        if not isinstance(compiled, Mapping):
            self._blocked.add(automation_id)
            self.store.worker_status(
                automation_id, "error", "missing compiled configuration"
            )
            return False
        taxonomy = compiled.get("company-categories.json")
        policy = compiled.get("jev-policy.json")
        if not isinstance(taxonomy, Mapping) or not isinstance(policy, Mapping):
            self._blocked.add(automation_id)
            self.store.worker_status(
                automation_id, "error", "invalid compiled native configuration"
            )
            return False

        records: list[tuple[str, str]] = []
        content_versions: list[str] = []
        for item in pending:
            if not isinstance(item, Mapping):
                continue
            content_version = item.get("content_version")
            text = item.get("text")
            if (
                isinstance(content_version, str)
                and content_version
                and isinstance(text, str)
                and text
            ):
                records.append((content_version, text))
                content_versions.append(content_version)
        if not records:
            return False

        configuration_key = str(automation.get("configuration_key") or "")
        manifest = {
            "source": "bluesky-live",
            "automation_id": automation_id,
            "configuration_key": configuration_key,
        }
        run_id: str | None = None
        native_status: dict[str, Any] | None = None
        self._active_automation_id = automation_id
        self._active_max_usd = max_usd
        try:
            self.store.worker_status(
                automation_id, "running", None, {"stage": "preparing"}
            )
            from jev_engine import Engine

            self.state_dir.mkdir(parents=True, exist_ok=True)
            db_path = self.state_dir / "inference.sqlite"
            engine = Engine(db_path, dict(taxonomy), dict(policy))
            self._active_engine = engine
            with engine:
                run_id = engine.prepare(manifest, records)
                self.store.worker_status(
                    automation_id,
                    "running",
                    None,
                    {"run_id": run_id, "stage": "executing"},
                )
                request_transport = await self._transport_for_batch()
                native_status = await engine.execute(
                    run_id,
                    self.api_key,
                    max_usd,
                    concurrency=self.concurrency,
                    requests_per_minute=self.requests_per_minute,
                    transport=request_transport,
                )
                results = self._read_native_results(db_path, run_id, content_versions)
                if results:
                    self.store.save_results(automation_id, results)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._blocked.add(automation_id)
            self._log(f"enrichment failed for automation {automation_id}", error)
            with contextlib.suppress(Exception):
                self.store.worker_status(
                    automation_id,
                    "error",
                    str(error),
                    {"run_id": run_id, "stage": "engine"},
                )
            return True
        finally:
            self._active_engine = None
            self._active_automation_id = None
            self._active_max_usd = None

        state, reason = self._native_state(native_status)
        if state in {"budget_exhausted", "error"}:
            self._blocked.add(automation_id)
        elif state == "idle":
            self._blocked.discard(automation_id)
        with contextlib.suppress(Exception):
            self.store.worker_status(
                automation_id,
                state,
                reason,
                self._native_telemetry(native_status, run_id),
            )
        return True

    async def _transport_for_batch(self) -> _PacingTransport:
        if self.transport is not None:
            return _PacingTransport(self.transport, self._pacer, close_underlying=False)
        return _PacingTransport(
            httpx.AsyncHTTPTransport(), self._pacer, close_underlying=True
        )

    @staticmethod
    def _read_native_results(
        db_path: Path, run_id: str, content_versions: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not content_versions:
            return []
        path = Path(db_path).resolve()
        uri = path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            output: list[dict[str, Any]] = []
            # A bounded batch is normally <=32; chunking keeps this helper safe
            # if a caller raises the runtime batch size later.
            for offset in range(0, len(content_versions), 400):
                group = list(content_versions[offset : offset + 400])
                placeholders = ",".join("?" for _ in group)
                rows = connection.execute(
                    f"""
                    SELECT content_version, stage, categorization_result_json,
                           sentiment_result_json
                    FROM items
                    WHERE run_id=? AND content_version IN ({placeholders})
                    """,
                    [run_id, *group],
                ).fetchall()
                for row in rows:
                    if row["stage"] != "done":
                        continue
                    classification = json.loads(row["categorization_result_json"])
                    status = classification.get("status")
                    if status == "others":
                        output.append(
                            {
                                "content_version": row["content_version"],
                                "status": "others",
                                "classification": dict(classification),
                                "sentiment": {},
                            }
                        )
                        continue
                    if status != "accepted":
                        raise ValueError(
                            f"completed native item has invalid classification status: {status!r}"
                        )
                    sentiment = json.loads(row["sentiment_result_json"])
                    output.append(
                        {
                            "content_version": row["content_version"],
                            "status": "ready",
                            "classification": dict(classification),
                            "sentiment": dict(sentiment),
                        }
                    )
            return output
        finally:
            connection.close()

    @staticmethod
    def _native_state(status: Mapping[str, Any] | None) -> tuple[str, str | None]:
        if not isinstance(status, Mapping):
            return "error", "native engine returned no status"
        native = str(status.get("status") or "").casefold()
        reason_value = status.get("reason")
        reason = str(reason_value) if reason_value is not None else None
        reason_text = (reason or "").casefold()
        if native == "completed":
            return "idle", None
        if native == "paused":
            if any(
                token in reason_text
                for token in ("budget", "credit", "cost", "max_usd")
            ):
                return "budget_exhausted", reason
            if reason_text in {
                "operator_stop",
                "shutdown",
                "automation_control_change",
            }:
                return "idle", reason
            return "error", reason or "native engine paused"
        if native in {"prepared", "running"}:
            return "idle", reason
        return "error", reason or native or "unknown native engine state"

    @staticmethod
    def _native_telemetry(
        status: Mapping[str, Any] | None, run_id: str | None
    ) -> dict[str, Any]:
        if not isinstance(status, Mapping):
            return {"run_id": run_id, "status": None}
        return {
            "run_id": run_id or status.get("run_id"),
            "status": status.get("status"),
            "reason": status.get("reason"),
            "counts": status.get("counts"),
            "actual_cost_usd": status.get("actual_cost_usd"),
            "uncertain_charge_usd": status.get("uncertain_charge_usd"),
            "inflight_reserved_usd": status.get("inflight_reserved_usd"),
            "updated_at": status.get("updated_at"),
            "available_usd": status.get("available_usd"),
        }

    # ------------------------------------------------------------------
    # Small shared helpers
    # ------------------------------------------------------------------
    def _source_status(self) -> dict[str, Any]:
        value = self.store.source_status()
        if not isinstance(value, Mapping):
            raise TypeError("Store.source_status() must return an object")
        return dict(value)

    @staticmethod
    def _cursor_gap_reason(error: BaseException) -> str | None:
        """Extract pre-upgrade HTTP 400 CursorTooOld/OutdatedCursor responses."""
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        if status != 400:
            return None
        fragments: list[str] = []
        for value in (
            getattr(response, "body", None),
            getattr(response, "text", None),
            getattr(response, "reason", None),
            str(error),
        ):
            if value is None:
                continue
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            fragments.append(str(value))
        text = " ".join(fragments)
        normalized = "".join(
            character for character in text.casefold() if character.isalnum()
        )
        if "cursortooold" in normalized:
            return "CursorTooOld"
        if "outdatedcursor" in normalized:
            return "OutdatedCursor"
        return None

    def _set_source_status(self, status: str, error: Any = None) -> None:
        self.store.set_source_status(status, None if error is None else str(error))

    def _record_notice(self, kind: str, message: str, payload: Any = None) -> None:
        self.store.record_notice(kind, message, payload)

    def _cursor_url(self, cursor: Any) -> str:
        if cursor is None:
            return self.source_url
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError(
                "durable source cursor must be a nonnegative integer or null"
            )
        parts = urlsplit(self.source_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "cursor"
        ]
        query.append(("cursor", str(cursor)))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def _wait_wake(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self._wake_event.is_set():
                self._wake_event.clear()
                return
            stop_task = asyncio.create_task(stop.wait())
            wake_task = asyncio.create_task(self._wake_event.wait())
            done, pending = await asyncio.wait(
                (stop_task, wake_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop_task in done and stop_task.result():
                return
            self._wake_event.clear()
            return

    async def _wait_or_wake(self, stop: asyncio.Event, seconds: float) -> None:
        if stop.is_set() or self._wake_event.is_set():
            self._wake_event.clear()
            return
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        finally:
            self._wake_event.clear()

    async def _wait_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    @staticmethod
    def _log(message: str, error: Any = None) -> None:
        suffix = f": {error}" if error is not None else ""
        print(f"[bluesky-runtime] {message}{suffix}", file=sys.stderr, flush=True)


__all__ = ["Runtime"]
