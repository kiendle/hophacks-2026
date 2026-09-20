from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import compile_config
from jev_protocol import _validate_choice_answer


_STATUS_VALUES = {"ready", "others"}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not JSON-compatible: {exc}") from exc


def _copy_json(value: Any) -> Any:
    return json.loads(_canonical(value))


def _load_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _required_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _valid_seq(value: Any, name: str = "seq") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _valid_limit(value: Any, default: int, maximum: int) -> int:
    if value is None:
        value = default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise ValueError(f"limit must be an integer from 1 through {maximum}")
    return value


def _valid_money(value: Any, name: str = "max_usd") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return str(value)
    return None


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("$type")
    return value if isinstance(value, str) else ""


def _did_from_uri(uri: str | None) -> str | None:
    if not isinstance(uri, str):
        return None
    if uri.startswith("at://"):
        parts = uri[5:].split("/")
        return parts[0] if parts and parts[0] else None
    return None


class Store:
    """Durable source capture, normalization, routing, and result state.

    All methods run synchronously on the event-loop thread.  The connection is
    deliberately kept small and uses explicit IMMEDIATE transactions for every
    mutating operation so a source event, its normalized state, and the cursor
    are one durable unit.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def __enter__(self) -> "Store":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is closed")
        return self._conn

    def _write(self, callback: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._ensure_open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = callback(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _create_schema_on(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_events (
                seq INTEGER PRIMARY KEY,
                event_hash TEXT NOT NULL,
                event_json TEXT NOT NULL,
                event_type TEXT NOT NULL,
                did TEXT,
                event_time TEXT,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS source_events_type_idx ON source_events(event_type);

            CREATE TABLE IF NOT EXISTS notices (
                notice_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS notices_created_idx ON notices(notice_id DESC);

            CREATE TABLE IF NOT EXISTS posts (
                post_uri TEXT PRIMARY KEY,
                did TEXT NOT NULL,
                rkey TEXT NOT NULL,
                current_cid TEXT,
                current_content_version TEXT,
                current_text TEXT,
                current_text_hash TEXT,
                current_created_at TEXT,
                current_event_seq INTEGER,
                current_observed_at TEXT,
                current_deleted INTEGER NOT NULL DEFAULT 0 CHECK(current_deleted IN (0,1)),
                current_record_json TEXT
            );
            CREATE INDEX IF NOT EXISTS posts_did_idx ON posts(did);

            CREATE TABLE IF NOT EXISTS post_versions (
                post_uri TEXT NOT NULL,
                content_version TEXT NOT NULL,
                cid TEXT,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                published_at TEXT,
                observed_at TEXT,
                first_seq INTEGER NOT NULL,
                last_seq INTEGER NOT NULL,
                record_json TEXT,
                PRIMARY KEY(post_uri, content_version)
            );
            CREATE INDEX IF NOT EXISTS post_versions_cid_idx
                ON post_versions(post_uri, cid);
            CREATE INDEX IF NOT EXISTS post_versions_content_idx
                ON post_versions(content_version);

            CREATE TABLE IF NOT EXISTS post_mutations (
                mutation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_seq INTEGER NOT NULL REFERENCES source_events(seq) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                post_uri TEXT NOT NULL,
                did TEXT NOT NULL,
                rkey TEXT NOT NULL,
                cid TEXT,
                content_version TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                publication_time TEXT,
                event_time TEXT,
                observed_at TEXT,
                operation TEXT NOT NULL,
                deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
                record_json TEXT,
                UNIQUE(source_seq, ordinal)
            );
            CREATE INDEX IF NOT EXISTS post_mutations_cursor_idx
                ON post_mutations(source_seq, mutation_id);
            CREATE INDEX IF NOT EXISTS post_mutations_version_idx
                ON post_mutations(post_uri, content_version, source_seq);
            CREATE INDEX IF NOT EXISTS post_mutations_cid_idx
                ON post_mutations(post_uri, cid, source_seq);

            CREATE TABLE IF NOT EXISTS like_ledger (
                like_uri TEXT PRIMARY KEY,
                actor_did TEXT,
                subject_uri TEXT,
                subject_cid TEXT,
                content_version TEXT,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                last_seq INTEGER NOT NULL,
                last_operation TEXT NOT NULL,
                event_time TEXT,
                observed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS like_ledger_subject_idx
                ON like_ledger(subject_uri, subject_cid);
            CREATE INDEX IF NOT EXISTS like_ledger_actor_idx
                ON like_ledger(actor_did);

            CREATE TABLE IF NOT EXISTS like_deltas (
                delta_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_seq INTEGER NOT NULL REFERENCES source_events(seq) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                like_uri TEXT,
                actor_did TEXT,
                subject_uri TEXT,
                subject_cid TEXT,
                content_version TEXT,
                delta INTEGER NOT NULL CHECK(delta IN (-1,1)),
                operation TEXT NOT NULL,
                event_time TEXT,
                observed_at TEXT,
                resolved INTEGER NOT NULL CHECK(resolved IN (0,1)),
                unresolved_reason TEXT,
                UNIQUE(source_seq, ordinal)
            );
            CREATE INDEX IF NOT EXISTS like_deltas_cursor_idx
                ON like_deltas(source_seq, delta_id);
            CREATE INDEX IF NOT EXISTS like_deltas_subject_idx
                ON like_deltas(subject_uri, subject_cid, resolved);

            CREATE TABLE IF NOT EXISTS automations (
                automation_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                compiled_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                configuration_key TEXT NOT NULL,
                max_usd REAL NOT NULL CHECK(max_usd >= 0),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                start_cursor INTEGER NOT NULL,
                routing_cursor INTEGER NOT NULL,
                worker_state TEXT NOT NULL DEFAULT 'idle',
                last_error TEXT,
                engine_status_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS automations_enabled_idx
                ON automations(enabled, automation_id);

            CREATE TABLE IF NOT EXISTS automation_matches (
                automation_id TEXT NOT NULL REFERENCES automations(automation_id) ON DELETE CASCADE,
                post_uri TEXT NOT NULL,
                content_version TEXT NOT NULL,
                groups_json TEXT NOT NULL,
                discovery INTEGER NOT NULL CHECK(discovery IN (0,1)),
                first_seq INTEGER,
                last_seq INTEGER,
                PRIMARY KEY(automation_id, post_uri, content_version)
            );
            CREATE INDEX IF NOT EXISTS automation_matches_content_idx
                ON automation_matches(automation_id, content_version);

            CREATE TABLE IF NOT EXISTS automation_work (
                automation_id TEXT NOT NULL REFERENCES automations(automation_id) ON DELETE CASCADE,
                content_version TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                classification_json TEXT,
                sentiment_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(automation_id, content_version),
                CHECK(status IN ('pending','ready','others'))
            );
            CREATE INDEX IF NOT EXISTS automation_work_pending_idx
                ON automation_work(automation_id, status, content_version);
            """
        )
        defaults = {
            "source_endpoint": None,
            "source_cursor": None,
            "source_status": "stopped",
            "source_error": None,
        }
        for key, value in defaults.items():
            encoded = _canonical(value)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, encoded)
            )

    def _create_schema(self) -> None:
        self._create_schema_on(self._ensure_open())

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return default if row is None else _load_json(row[0], default)

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _canonical(value)),
        )

    @staticmethod
    def _source_cursor(conn: sqlite3.Connection) -> int | None:
        value = Store._meta(conn, "source_cursor")
        return None if value is None else int(value)

    @staticmethod
    def _latest_seq(conn: sqlite3.Connection) -> int:
        """Retained history survives an explicitly reset subscription cursor."""
        return conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM source_events"
        ).fetchone()[0]

    def _automation_row(self, automation_id: str) -> sqlite3.Row:
        automation_id = _required_text(automation_id, "automation_id")
        row = (
            self._ensure_open()
            .execute(
                "SELECT * FROM automations WHERE automation_id=?", (automation_id,)
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(f"unknown automation {automation_id!r}")
        return row

    @staticmethod
    def _automation_dict(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": row["automation_id"],
            "name": row["name"],
            "config_hash": row["config_hash"],
            "configuration_key": row["configuration_key"],
            "max_usd": float(row["max_usd"]),
            "enabled": bool(row["enabled"]),
            "start_cursor": int(row["start_cursor"]),
            "routing_cursor": int(row["routing_cursor"]),
            "worker_state": row["worker_state"],
            "last_error": row["last_error"],
            "engine_status": _load_json(row["engine_status_json"]),
            "created_at": row["created_at"],
        }
        if full:
            result["config"] = _load_json(row["config_json"], {})
            result["compiled"] = _load_json(row["compiled_json"], {})
        return result

    # ------------------------------------------------------------------
    # Source status, notices, and raw event capture
    # ------------------------------------------------------------------
    def set_source(self, endpoint: str) -> None:
        endpoint = _required_text(endpoint, "endpoint")

        def mutate(conn: sqlite3.Connection) -> None:
            current = self._meta(conn, "source_endpoint")
            if current is not None and current != endpoint:
                event_count = conn.execute(
                    "SELECT COUNT(*) FROM source_events"
                ).fetchone()[0]
                cursor = self._source_cursor(conn)
                if event_count or cursor is not None:
                    raise ValueError(
                        "source endpoint cannot change after capture state exists"
                    )
            self._set_meta(conn, "source_endpoint", endpoint)

        self._write(mutate)

    def source_status(self) -> dict[str, Any]:
        conn = self._ensure_open()
        endpoint = self._meta(conn, "source_endpoint")
        cursor = self._source_cursor(conn)
        event_count = int(
            conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0]
        )
        post_count = int(
            conn.execute("SELECT COUNT(*) FROM post_mutations").fetchone()[0]
        )
        like_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM like_deltas WHERE delta IN (-1,1)"
            ).fetchone()[0]
        )
        unresolved = int(
            conn.execute(
                "SELECT COUNT(*) FROM like_deltas WHERE resolved=0"
            ).fetchone()[0]
        )
        notices = [
            {
                "id": int(row["notice_id"]),
                "kind": row["kind"],
                "message": row["message"],
                "payload": _load_json(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in conn.execute(
                "SELECT * FROM notices ORDER BY notice_id DESC LIMIT 100"
            ).fetchall()
        ]
        return {
            "endpoint": endpoint,
            "cursor": cursor,
            "status": self._meta(conn, "source_status", "stopped"),
            "error": self._meta(conn, "source_error"),
            "event_count": event_count,
            "post_count": post_count,
            "like_count": like_count,
            "unresolved_like_count": unresolved,
            "notices": notices,
        }

    def set_source_status(self, status: str, error: str | None = None) -> None:
        status = _required_text(status, "status")
        if error is not None:
            error = _required_text(error, "error")

        def mutate(conn: sqlite3.Connection) -> None:
            self._set_meta(conn, "source_status", status)
            self._set_meta(conn, "source_error", error)

        self._write(mutate)

    def record_notice(self, kind: str, message: str, payload: Any = None) -> None:
        kind = _required_text(kind, "kind")
        message = _required_text(message, "message")
        encoded = None if payload is None else _canonical(payload)

        def mutate(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO notices(kind,message,payload_json,created_at) VALUES(?,?,?,?)",
                (kind, message, encoded, _utc_now()),
            )

        self._write(mutate)

    def reset_cursor(self) -> None:
        def mutate(conn: sqlite3.Connection) -> None:
            now = _utc_now()
            conn.execute(
                "INSERT INTO notices(kind,message,payload_json,created_at) VALUES(?,?,?,?)",
                (
                    "gap",
                    "source cursor reset by explicit operator approval",
                    _canonical({"previous_cursor": self._source_cursor(conn)}),
                    now,
                ),
            )
            # Missing activity may have changed a like record's subject.
            # Preserve historical deltas, but require newly observed subjects.
            conn.execute("DELETE FROM like_ledger")
            self._set_meta(conn, "source_cursor", None)
            self._set_meta(conn, "source_status", "retrying")
            self._set_meta(conn, "source_error", None)

        self._write(mutate)

    # ------------------------------------------------------------------
    # Event normalization
    # ------------------------------------------------------------------
    def _normalize_event(
        self, conn: sqlite3.Connection, source_seq: int, event: Mapping[str, Any]
    ) -> None:
        event_time = _timestamp(event.get("time"))
        event_kind = _event_type(event).split("#")[-1]
        did = event.get("did")
        if event_kind == "commit":
            collection = event.get("collection")
            rkey = event.get("rkey")
            if collection == "app.bsky.feed.post":
                self._normalize_post(conn, source_seq, 0, did, rkey, event, event_time)
            elif collection == "app.bsky.feed.like":
                self._normalize_like(conn, source_seq, 0, did, rkey, event, event_time)
        elif (
            event_kind == "account"
            and event.get("active") is False
            and event.get("status") == "deleted"
        ):
            conn.execute("UPDATE posts SET current_deleted=1 WHERE did=?", (did,))
        elif event_kind == "sync":
            conn.execute("DELETE FROM like_ledger WHERE actor_did=?", (did,))
            conn.execute(
                "INSERT INTO notices(kind,message,payload_json,created_at) VALUES(?,?,?,?)",
                (
                    "repository_sync",
                    "Repository sync observed; captured history was not rebuilt.",
                    _canonical({"did": did, "seq": source_seq}),
                    _utc_now(),
                ),
            )

    def _post_version(
        self,
        conn: sqlite3.Connection,
        post_uri: str,
        cid: str | None,
        text: str,
        text_hash: str,
    ) -> str:
        if cid:
            row = conn.execute(
                "SELECT text_hash FROM post_mutations WHERE post_uri=? AND cid=? ORDER BY source_seq LIMIT 1",
                (post_uri, cid),
            ).fetchone()
            if row is not None and row["text_hash"] != text_hash:
                raise ValueError(f"post CID {cid!r} is bound to conflicting text")
        return text_hash

    def _normalize_post(
        self,
        conn: sqlite3.Connection,
        source_seq: int,
        ordinal: int,
        did: str | None,
        rkey: Any,
        operation: Mapping[str, Any],
        event_time: str | None,
    ) -> None:
        record = operation.get("record")
        record_map = record if isinstance(record, Mapping) else {}
        uri = operation.get("uri")
        if not isinstance(uri, str) or not uri:
            if isinstance(did, str) and isinstance(rkey, str):
                uri = f"at://{did}/app.bsky.feed.post/{rkey}"
            else:
                return
        did = did or _did_from_uri(uri)
        if not did:
            return
        rkey = rkey if isinstance(rkey, str) else uri.rsplit("/", 1)[-1]
        op = operation.get("operation")
        if not isinstance(op, str):
            op = (
                "update"
                if conn.execute(
                    "SELECT 1 FROM posts WHERE post_uri=?", (uri,)
                ).fetchone()
                else "create"
            )
        if op not in {"create", "update", "delete"}:
            return
        current = conn.execute(
            "SELECT * FROM posts WHERE post_uri=?", (uri,)
        ).fetchone()
        previous_text = (
            current["current_text"]
            if current is not None and current["current_text"] is not None
            else ""
        )
        previous_pub = current["current_created_at"] if current is not None else None
        previous_version = (
            current["current_content_version"] if current is not None else None
        )
        text_value = record_map.get("text")
        text = (
            text_value
            if isinstance(text_value, str)
            else (previous_text if op == "delete" else "")
        )
        text_hash = _sha256_text(text)
        cid_value = operation.get("cid")
        if not isinstance(cid_value, str):
            cid_value = (
                record_map.get("cid")
                if isinstance(record_map.get("cid"), str)
                else None
            )
        cid = cid_value
        publication_time = _timestamp(record_map.get("createdAt")) or previous_pub
        observed_at = event_time
        if op == "delete" and previous_version:
            content_version = previous_version
        else:
            content_version = self._post_version(conn, uri, cid, text, text_hash)
        record_json = _canonical(record_map) if record_map else None
        conn.execute(
            """
            INSERT INTO post_mutations(
                source_seq,ordinal,post_uri,did,rkey,cid,content_version,text,text_hash,
                publication_time,event_time,observed_at,operation,deleted,record_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_seq,
                ordinal,
                uri,
                did,
                rkey,
                cid,
                content_version,
                text,
                text_hash,
                publication_time,
                event_time,
                observed_at,
                op,
                1 if op == "delete" else 0,
                record_json,
            ),
        )
        if op != "delete":
            conn.execute(
                """
                INSERT INTO post_versions(
                    post_uri,content_version,cid,text,text_hash,published_at,observed_at,
                    first_seq,last_seq,record_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_uri,content_version) DO UPDATE SET
                    last_seq=MAX(post_versions.last_seq,excluded.last_seq)
                """,
                (
                    uri,
                    content_version,
                    cid,
                    text,
                    text_hash,
                    publication_time,
                    observed_at,
                    source_seq,
                    source_seq,
                    record_json,
                ),
            )
        old_seq = current["current_event_seq"] if current is not None else None
        if old_seq is None or source_seq >= int(old_seq):
            conn.execute(
                """
                INSERT INTO posts(
                    post_uri,did,rkey,current_cid,current_content_version,current_text,
                    current_text_hash,current_created_at,current_event_seq,current_observed_at,
                    current_deleted,current_record_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_uri) DO UPDATE SET
                    did=excluded.did,rkey=excluded.rkey,current_cid=excluded.current_cid,
                    current_content_version=excluded.current_content_version,current_text=excluded.current_text,
                    current_text_hash=excluded.current_text_hash,current_created_at=excluded.current_created_at,
                    current_event_seq=excluded.current_event_seq,current_observed_at=excluded.current_observed_at,
                    current_deleted=excluded.current_deleted,current_record_json=excluded.current_record_json
                """,
                (
                    uri,
                    did,
                    rkey,
                    cid or (current["current_cid"] if current is not None else None),
                    content_version,
                    text,
                    text_hash,
                    publication_time,
                    source_seq,
                    observed_at,
                    1 if op == "delete" else 0,
                    record_json,
                ),
            )
        if op != "delete" and cid:
            self._resolve_post_context(conn, uri, cid, content_version)

    def _resolve_post_context(
        self,
        conn: sqlite3.Connection,
        post_uri: str,
        cid: str,
        content_version: str,
    ) -> None:
        conn.execute(
            """
            UPDATE like_deltas
            SET content_version=?,resolved=1,unresolved_reason=NULL
            WHERE subject_uri=? AND subject_cid=?
              AND resolved=0 AND unresolved_reason='missing_post'
            """,
            (content_version, post_uri, cid),
        )
        conn.execute(
            """
            UPDATE like_ledger
            SET content_version=?
            WHERE subject_uri=? AND subject_cid=? AND content_version IS NULL
            """,
            (content_version, post_uri, cid),
        )

    def _subject_from_operation(
        self, operation: Mapping[str, Any]
    ) -> tuple[str | None, str | None]:
        record = operation.get("record")
        record_map = record if isinstance(record, Mapping) else {}
        subject = record_map.get("subject")
        if not isinstance(subject, Mapping):
            subject = (
                operation.get("subject")
                if isinstance(operation.get("subject"), Mapping)
                else {}
            )
        uri = subject.get("uri") if isinstance(subject, Mapping) else None
        cid = subject.get("cid") if isinstance(subject, Mapping) else None
        return (
            uri if isinstance(uri, str) and uri else None,
            cid if isinstance(cid, str) and cid else None,
        )

    def _subject_context(
        self,
        conn: sqlite3.Connection,
        subject_uri: str | None,
        subject_cid: str | None,
    ) -> tuple[str | None, int, str | None]:
        if not subject_uri or not subject_cid:
            return None, 0, "missing_subject"
        row = conn.execute(
            "SELECT content_version FROM post_mutations WHERE post_uri=? AND cid=? AND operation!='delete' ORDER BY source_seq LIMIT 1",
            (subject_uri, subject_cid),
        ).fetchone()
        if row is None:
            return None, 0, "missing_post"
        return row["content_version"], 1, None

    def _add_like_delta(
        self,
        conn: sqlite3.Connection,
        source_seq: int,
        ordinal: int,
        like_uri: str | None,
        actor_did: str | None,
        subject_uri: str | None,
        subject_cid: str | None,
        delta: int,
        operation: str,
        event_time: str | None,
        observed_at: str | None,
        *,
        unknown_unlike: bool = False,
    ) -> None:
        content_version, resolved, reason = self._subject_context(
            conn, subject_uri, subject_cid
        )
        if unknown_unlike:
            resolved = 0
            reason = "unknown_unlike"
        conn.execute(
            """
            INSERT INTO like_deltas(
                source_seq,ordinal,like_uri,actor_did,subject_uri,subject_cid,content_version,
                delta,operation,event_time,observed_at,resolved,unresolved_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_seq,
                ordinal,
                like_uri,
                actor_did,
                subject_uri,
                subject_cid,
                content_version,
                delta,
                operation,
                event_time,
                observed_at,
                resolved,
                reason,
            ),
        )

    def _normalize_like(
        self,
        conn: sqlite3.Connection,
        source_seq: int,
        ordinal: int,
        did: str | None,
        rkey: Any,
        operation: Mapping[str, Any],
        event_time: str | None,
    ) -> None:
        op = operation.get("operation")
        if not isinstance(op, str) or op not in {"create", "update", "delete"}:
            return
        rkey = rkey if isinstance(rkey, str) else None
        like_uri = operation.get("uri")
        if not isinstance(like_uri, str) or not like_uri:
            if did and rkey:
                like_uri = f"at://{did}/app.bsky.feed.like/{rkey}"
            else:
                like_uri = None
        actor = did
        subject_uri, subject_cid = self._subject_from_operation(operation)
        previous = (
            conn.execute(
                "SELECT * FROM like_ledger WHERE like_uri=?", (like_uri,)
            ).fetchone()
            if like_uri
            else None
        )
        previous_active = bool(previous["active"]) if previous is not None else False
        previous_uri = previous["subject_uri"] if previous is not None else None
        previous_cid = previous["subject_cid"] if previous is not None else None
        observed_at = event_time
        next_ordinal = ordinal * 10
        if op in {"create", "update"}:
            if previous_active and (
                (subject_uri is None and previous_uri is None)
                or (subject_uri == previous_uri and subject_cid == previous_cid)
            ):
                pass  # Inclusive replay or a same-subject update: already active.
            else:
                if previous_active:
                    self._add_like_delta(
                        conn,
                        source_seq,
                        next_ordinal,
                        like_uri,
                        actor,
                        previous_uri,
                        previous_cid,
                        -1,
                        "update_retract" if op == "update" else "retract",
                        event_time,
                        observed_at,
                    )
                    next_ordinal += 1
                self._add_like_delta(
                    conn,
                    source_seq,
                    next_ordinal,
                    like_uri,
                    actor,
                    subject_uri,
                    subject_cid,
                    1,
                    "update_add" if op == "update" else "create",
                    event_time,
                    observed_at,
                )
            if like_uri:
                content_version, _, _ = self._subject_context(
                    conn, subject_uri, subject_cid
                )
                conn.execute(
                    """
                    INSERT INTO like_ledger(
                        like_uri,actor_did,subject_uri,subject_cid,content_version,active,
                        last_seq,last_operation,event_time,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(like_uri) DO UPDATE SET
                        actor_did=excluded.actor_did,subject_uri=excluded.subject_uri,
                        subject_cid=excluded.subject_cid,content_version=excluded.content_version,
                        active=excluded.active,last_seq=excluded.last_seq,last_operation=excluded.last_operation,
                        event_time=excluded.event_time,observed_at=excluded.observed_at
                    """,
                    (
                        like_uri,
                        actor,
                        subject_uri,
                        subject_cid,
                        content_version,
                        1,
                        source_seq,
                        op,
                        event_time,
                        observed_at,
                    ),
                )
            return
        if previous_active:
            subject_uri = previous_uri
            subject_cid = previous_cid
            self._add_like_delta(
                conn,
                source_seq,
                next_ordinal,
                like_uri,
                actor or (previous["actor_did"] if previous is not None else None),
                subject_uri,
                subject_cid,
                -1,
                "delete",
                event_time,
                observed_at,
            )
        elif previous is None:
            self._add_like_delta(
                conn,
                source_seq,
                next_ordinal,
                like_uri,
                actor,
                subject_uri,
                subject_cid,
                -1,
                "delete",
                event_time,
                observed_at,
                unknown_unlike=True,
            )
        # A second delete for an already inactive URI is idempotent.
        if like_uri:
            content_version, _, _ = self._subject_context(
                conn, subject_uri, subject_cid
            )
            conn.execute(
                """
                INSERT INTO like_ledger(
                    like_uri,actor_did,subject_uri,subject_cid,content_version,active,
                    last_seq,last_operation,event_time,observed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(like_uri) DO UPDATE SET
                    active=excluded.active,last_seq=excluded.last_seq,last_operation=excluded.last_operation,
                    event_time=excluded.event_time,observed_at=excluded.observed_at
                """,
                (
                    like_uri,
                    actor,
                    subject_uri,
                    subject_cid,
                    content_version,
                    0,
                    source_seq,
                    op,
                    event_time,
                    observed_at,
                ),
            )

    # ------------------------------------------------------------------
    # Ingestion entrypoint
    # ------------------------------------------------------------------
    def ingest(self, events: list[dict[str, Any]]) -> int:
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        validated: list[tuple[int, dict[str, Any], str, str]] = []
        seen: dict[int, str] = {}
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("each event must be an object")
            seq = _valid_seq(event.get("seq"))
            raw_json = _canonical(event)
            event_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            prior = seen.get(seq)
            if prior is not None and prior != event_hash:
                raise ValueError(f"conflicting duplicate seq {seq}")
            if prior is not None:
                continue
            seen[seq] = event_hash
            validated.append((seq, event, event_hash, raw_json))

        if not validated:
            return 0

        def mutate(conn: sqlite3.Connection) -> int:
            new_count = 0
            max_seq = self._source_cursor(conn)
            for seq, event, event_hash, raw_json in validated:
                existing = conn.execute(
                    "SELECT event_hash FROM source_events WHERE seq=?", (seq,)
                ).fetchone()
                if existing is not None:
                    if existing["event_hash"] != event_hash:
                        raise ValueError(f"conflicting duplicate seq {seq}")
                    if max_seq is None or seq > max_seq:
                        max_seq = seq
                    continue
                conn.execute(
                    """
                    INSERT INTO source_events(seq,event_hash,event_json,event_type,did,event_time,received_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        seq,
                        event_hash,
                        raw_json,
                        _event_type(event),
                        event.get("did"),
                        _timestamp(event.get("time")),
                        _utc_now(),
                    ),
                )
                self._normalize_event(conn, seq, event)
                new_count += 1
                if max_seq is None or seq > max_seq:
                    max_seq = seq
            if max_seq is not None:
                self._set_meta(conn, "source_cursor", max_seq)
            return new_count

        return int(self._write(mutate))

    # ------------------------------------------------------------------
    # Immutable automation configuration and worker telemetry
    # ------------------------------------------------------------------
    def create_automation(
        self, name: str, config: dict[str, Any], max_usd: float = 0
    ) -> dict[str, Any]:
        name = _required_text(name, "name")
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        budget = _valid_money(max_usd)
        compiled_result = compile_config(config)
        config_json = _canonical(config)
        compiled_json = _canonical(compiled_result["compiled"])
        automation_id = str(uuid.uuid4())

        def mutate(conn: sqlite3.Connection) -> dict[str, Any]:
            start_cursor = self._latest_seq(conn)
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO automations(
                    automation_id,name,config_json,compiled_json,config_hash,configuration_key,
                    max_usd,enabled,start_cursor,routing_cursor,worker_state,last_error,
                    engine_status_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    automation_id,
                    name,
                    config_json,
                    compiled_json,
                    compiled_result["config_hash"],
                    compiled_result["configuration_key"],
                    budget,
                    0,
                    start_cursor,
                    start_cursor,
                    "idle",
                    None,
                    None,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM automations WHERE automation_id=?", (automation_id,)
            ).fetchone()
            assert row is not None
            return self._automation_dict(row, full=False)

        return self._write(mutate)

    def list_automations(self) -> list[dict[str, Any]]:
        rows = (
            self._ensure_open()
            .execute("SELECT * FROM automations ORDER BY created_at,automation_id")
            .fetchall()
        )
        return [self._automation_dict(row, full=False) for row in rows]

    def get_automation(self, automation_id: str) -> dict[str, Any]:
        return self._automation_dict(self._automation_row(automation_id), full=True)

    def set_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        automation_id = _required_text(automation_id, "automation_id")

        def mutate(conn: sqlite3.Connection) -> dict[str, Any]:
            if (
                conn.execute(
                    "SELECT 1 FROM automations WHERE automation_id=?", (automation_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"unknown automation {automation_id!r}")
            if enabled:
                conn.execute(
                    "UPDATE automations SET enabled=1,worker_state='idle',last_error=NULL WHERE automation_id=?",
                    (automation_id,),
                )
            else:
                conn.execute(
                    "UPDATE automations SET enabled=0 WHERE automation_id=?",
                    (automation_id,),
                )
            row = conn.execute(
                "SELECT * FROM automations WHERE automation_id=?", (automation_id,)
            ).fetchone()
            assert row is not None
            return self._automation_dict(row, full=False)

        return self._write(mutate)

    def set_budget(self, automation_id: str, max_usd: float) -> dict[str, Any]:
        automation_id = _required_text(automation_id, "automation_id")
        budget = _valid_money(max_usd)

        def mutate(conn: sqlite3.Connection) -> dict[str, Any]:
            if (
                conn.execute(
                    "SELECT 1 FROM automations WHERE automation_id=?", (automation_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"unknown automation {automation_id!r}")
            conn.execute(
                "UPDATE automations SET max_usd=?,worker_state='idle',last_error=NULL WHERE automation_id=?",
                (budget, automation_id),
            )
            row = conn.execute(
                "SELECT * FROM automations WHERE automation_id=?", (automation_id,)
            ).fetchone()
            assert row is not None
            return self._automation_dict(row, full=False)

        return self._write(mutate)

    def worker_status(
        self,
        automation_id: str,
        state: str,
        error: str | None = None,
        engine_status: Any = None,
    ) -> None:
        automation_id = _required_text(automation_id, "automation_id")
        state = _required_text(state, "state")
        if error is not None:
            error = _required_text(error, "error")
        engine_json = None if engine_status is None else _canonical(engine_status)

        def mutate(conn: sqlite3.Connection) -> None:
            if (
                conn.execute(
                    "SELECT 1 FROM automations WHERE automation_id=?", (automation_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"unknown automation {automation_id!r}")
            conn.execute(
                "UPDATE automations SET worker_state=?,last_error=?,engine_status_json=? WHERE automation_id=?",
                (state, error, engine_json, automation_id),
            )

        self._write(mutate)

    # ------------------------------------------------------------------
    # Router and per-automation work queue
    # ------------------------------------------------------------------
    def fetch_posts(
        self, automation_id: str, limit: int = 500
    ) -> tuple[list[dict[str, Any]], int]:
        limit = _valid_limit(limit, 500, 500)
        row = self._automation_row(automation_id)
        cursor = int(row["routing_cursor"])
        conn = self._ensure_open()
        rows = conn.execute(
            """
            SELECT m.*,p.current_deleted,p.current_content_version,p.current_text,
                   p.current_event_seq,p.current_observed_at
            FROM post_mutations m
            LEFT JOIN posts p ON p.post_uri=m.post_uri
            WHERE m.source_seq>?
            ORDER BY m.source_seq,m.mutation_id
            LIMIT ?
            """,
            (cursor, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for item in rows:
            result.append(
                {
                    "seq": int(item["source_seq"]),
                    "post_uri": item["post_uri"],
                    "did": item["did"],
                    "rkey": item["rkey"],
                    "cid": item["cid"],
                    "content_version": item["content_version"],
                    "text": item["text"],
                    "text_hash": item["text_hash"],
                    "publication_time": item["publication_time"],
                    "event_time": item["event_time"],
                    "observed_at": item["observed_at"],
                    "operation": item["operation"],
                    "deleted": bool(item["deleted"]),
                    "current_deleted": bool(item["current_deleted"])
                    if item["current_deleted"] is not None
                    else bool(item["deleted"]),
                    "current_event_seq": item["current_event_seq"],
                    "current_observed_at": item["current_observed_at"],
                }
            )
        durable = self._latest_seq(conn)
        if rows and len(rows) >= limit:
            through_seq = int(rows[-1]["source_seq"])
        else:
            through_seq = max(cursor, durable)
        return result, through_seq

    def route_posts(
        self,
        automation_id: str,
        matches: list[dict[str, Any]],
        through_seq: int,
    ) -> None:
        automation_id = _required_text(automation_id, "automation_id")
        if not isinstance(matches, list):
            raise ValueError("matches must be a list")
        if len(matches) > 5000:
            raise ValueError("matches is too large")
        through_seq = _valid_seq(through_seq, "through_seq")

        def mutate(conn: sqlite3.Connection) -> None:
            auto = conn.execute(
                "SELECT * FROM automations WHERE automation_id=?", (automation_id,)
            ).fetchone()
            if auto is None:
                raise KeyError(f"unknown automation {automation_id!r}")
            current_cursor = int(auto["routing_cursor"])
            durable = self._latest_seq(conn)
            if through_seq > durable:
                raise ValueError("through_seq is ahead of the durable source cursor")
            if through_seq <= current_cursor:
                return
            now = _utc_now()
            for match in matches:
                if not isinstance(match, dict):
                    raise ValueError("each match must be an object")
                post_uri = match.get("post_uri")
                content_version = match.get("content_version")
                post_uri = _required_text(post_uri, "match.post_uri")
                content_version = _required_text(
                    content_version, "match.content_version"
                )
                groups = match.get("groups", [])
                if not isinstance(groups, list) or any(
                    not isinstance(group, str) or not group.strip() for group in groups
                ):
                    raise ValueError("match.groups must be a list of non-empty strings")
                groups = sorted(set(groups))
                discovery = match.get("discovery", False)
                if not isinstance(discovery, bool):
                    raise ValueError("match.discovery must be a boolean")
                seq_value = match.get("seq")
                first_seq = (
                    through_seq
                    if seq_value is None
                    else _valid_seq(seq_value, "match.seq")
                )
                existing_match = conn.execute(
                    "SELECT groups_json FROM automation_matches WHERE automation_id=? AND post_uri=? AND content_version=?",
                    (automation_id, post_uri, content_version),
                ).fetchone()
                if existing_match is not None:
                    prior_groups = _load_json(existing_match["groups_json"], [])
                    if isinstance(prior_groups, list):
                        groups = sorted(
                            set(groups)
                            | {
                                group
                                for group in prior_groups
                                if isinstance(group, str)
                            }
                        )
                if first_seq > through_seq:
                    raise ValueError("match.seq is ahead of through_seq")
                text_value = match.get("text")
                text = text_value if isinstance(text_value, str) else None
                operation = match.get("operation")
                deleted = bool(match.get("deleted", False))
                conn.execute(
                    """
                    INSERT INTO automation_matches(
                        automation_id,post_uri,content_version,groups_json,discovery,first_seq,last_seq
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(automation_id,post_uri,content_version) DO UPDATE SET
                        groups_json=excluded.groups_json,
                        discovery=MAX(automation_matches.discovery,excluded.discovery),
                        first_seq=CASE
                            WHEN automation_matches.first_seq IS NULL THEN excluded.first_seq
                            WHEN excluded.first_seq IS NULL THEN automation_matches.first_seq
                            ELSE MIN(automation_matches.first_seq,excluded.first_seq)
                        END,
                        last_seq=CASE
                            WHEN automation_matches.last_seq IS NULL THEN excluded.last_seq
                            WHEN excluded.last_seq IS NULL THEN automation_matches.last_seq
                            ELSE MAX(automation_matches.last_seq,excluded.last_seq)
                        END
                    """,
                    (
                        automation_id,
                        post_uri,
                        content_version,
                        _canonical(groups),
                        1 if discovery else 0,
                        first_seq,
                        first_seq,
                    ),
                )
                if text is None:
                    version = conn.execute(
                        "SELECT text FROM post_versions WHERE post_uri=? AND content_version=?",
                        (post_uri, content_version),
                    ).fetchone()
                    text = version["text"] if version is not None else ""
                if text and not deleted and operation != "delete":
                    if _sha256_text(text) != content_version:
                        raise ValueError(
                            "matched text does not match its content_version"
                        )
                    conn.execute(
                        """
                        INSERT INTO automation_work(
                            automation_id,content_version,text,status,classification_json,sentiment_json,
                            created_at,updated_at
                        ) VALUES(?,?,?,'pending',NULL,NULL,?,?)
                        ON CONFLICT(automation_id,content_version) DO NOTHING
                        """,
                        (automation_id, content_version, text, now, now),
                    )
            conn.execute(
                "UPDATE automations SET routing_cursor=? WHERE automation_id=? AND routing_cursor<?",
                (through_seq, automation_id, through_seq),
            )

        self._write(mutate)

    def pending(self, automation_id: str, limit: int = 32) -> list[dict[str, Any]]:
        limit = _valid_limit(limit, 32, 1000)
        self._automation_row(automation_id)
        rows = (
            self._ensure_open()
            .execute(
                """
            SELECT content_version,text FROM automation_work
            WHERE automation_id=? AND status='pending'
            ORDER BY created_at,content_version
            LIMIT ?
            """,
                (automation_id, limit),
            )
            .fetchall()
        )
        return [
            {"content_version": row["content_version"], "text": row["text"]}
            for row in rows
        ]

    @staticmethod
    def _validate_sentiment(value: Any, criteria: Mapping[str, str]) -> Any:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("sentiment must be an object")
        question = {"criteria": criteria}
        for target, answer in value.items():
            if not isinstance(target, str) or not target.strip():
                raise ValueError("sentiment must map nonempty target IDs to answers")
            _validate_choice_answer(answer, question, f"sentiment[{target!r}]")
        return _copy_json(value)

    @staticmethod
    def _validate_classification(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("classification must be an object")
        status = value.get("status")
        if status is not None and status not in {"accepted", "others"}:
            raise ValueError("classification.status must be accepted or others")
        companies = value.get("companies")
        if companies is not None and (
            not isinstance(companies, list)
            or any(
                not isinstance(company, str) or not company.strip()
                for company in companies
            )
        ):
            raise ValueError(
                "classification.companies must be a list of non-empty strings"
            )
        probabilities = value.get("probabilities")
        if probabilities is not None and not isinstance(probabilities, Mapping):
            raise ValueError("classification.probabilities must be an object")
        return _copy_json(value)

    def save_results(self, automation_id: str, results: list[dict[str, Any]]) -> None:
        automation_id = _required_text(automation_id, "automation_id")
        if not isinstance(results, list):
            raise ValueError("results must be a list")
        if len(results) > 1000:
            raise ValueError("results is too large")

        def mutate(conn: sqlite3.Connection) -> None:
            automation = conn.execute(
                "SELECT compiled_json FROM automations WHERE automation_id=?",
                (automation_id,),
            ).fetchone()
            if automation is None:
                raise KeyError(f"unknown automation {automation_id!r}")
            criteria = _load_json(automation["compiled_json"])["jev-policy.json"][
                "sentiment"
            ]["criteria"]
            now = _utc_now()
            for item in results:
                if not isinstance(item, dict):
                    raise ValueError("each result must be an object")
                content_version = _required_text(
                    item.get("content_version"), "result.content_version"
                )
                status = item.get("status")
                if status not in _STATUS_VALUES:
                    raise ValueError("result.status must be ready or others")
                work = conn.execute(
                    "SELECT status FROM automation_work WHERE automation_id=? AND content_version=?",
                    (automation_id, content_version),
                ).fetchone()
                if work is None:
                    raise KeyError(
                        f"unknown pending content version {content_version!r}"
                    )
                classification = self._validate_classification(
                    item.get("classification")
                )
                if status == "others":
                    # Native export uses an empty object for Others because no
                    # sentiment request is issued; do not persist fake context.
                    sentiment = None
                else:
                    sentiment = self._validate_sentiment(
                        item.get("sentiment"), criteria
                    )
                if status == "ready" and (classification is None or sentiment is None):
                    raise ValueError(
                        "ready results require classification and fixed sentiment"
                    )
                if work["status"] in _STATUS_VALUES:
                    continue  # Completed work is immutable and never reset by a replay.
                conn.execute(
                    """
                    UPDATE automation_work
                    SET status=?,classification_json=?,sentiment_json=?,updated_at=?
                    WHERE automation_id=? AND content_version=?
                    """,
                    (
                        status,
                        None if classification is None else _canonical(classification),
                        None if sentiment is None else _canonical(sentiment),
                        now,
                        automation_id,
                        content_version,
                    ),
                )

        self._write(mutate)

    # ------------------------------------------------------------------
    # Snapshot query API
    # ------------------------------------------------------------------
    def _post_results(self, automation_id: str, limit: int) -> list[dict[str, Any]]:
        conn = self._ensure_open()
        rows = conn.execute(
            """
            SELECT
                m.post_uri,m.content_version,m.groups_json,m.discovery,m.first_seq,m.last_seq,
                v.cid,v.text,v.text_hash,v.published_at,v.observed_at,
                w.status,w.classification_json,w.sentiment_json,
                p.current_deleted,p.current_content_version,p.current_text,
                p.current_event_seq,p.current_observed_at,
                (SELECT pm.operation FROM post_mutations pm
                 WHERE pm.post_uri=m.post_uri AND pm.content_version=m.content_version
                 ORDER BY pm.source_seq,pm.mutation_id LIMIT 1) AS operation,
                (SELECT pm.event_time FROM post_mutations pm
                 WHERE pm.post_uri=m.post_uri AND pm.content_version=m.content_version
                 ORDER BY pm.source_seq,pm.mutation_id LIMIT 1) AS event_time
            FROM automation_matches m
            LEFT JOIN post_versions v
              ON v.post_uri=m.post_uri AND v.content_version=m.content_version
            LEFT JOIN automation_work w
              ON w.automation_id=m.automation_id AND w.content_version=m.content_version
            LEFT JOIN posts p ON p.post_uri=m.post_uri
            WHERE m.automation_id=?
            ORDER BY m.last_seq DESC,m.post_uri,m.content_version
            LIMIT ?
            """,
            (automation_id, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            status = row["status"] or "pending"
            groups = _load_json(row["groups_json"], [])
            if not isinstance(groups, list):
                groups = []
            result.append(
                {
                    "automation_id": automation_id,
                    "seq": row["first_seq"],
                    "post_uri": row["post_uri"],
                    "content_version": row["content_version"],
                    "cid": row["cid"],
                    "text": row["text"],
                    "text_hash": row["text_hash"],
                    "publication_time": row["published_at"],
                    "event_time": row["event_time"] or row["observed_at"],
                    "observed_at": row["observed_at"],
                    "operation": row["operation"],
                    "current_deleted": bool(row["current_deleted"])
                    if row["current_deleted"] is not None
                    else False,
                    "current_content_version": row["current_content_version"],
                    "current_text": row["current_text"],
                    "current_event_seq": row["current_event_seq"],
                    "current_observed_at": row["current_observed_at"],
                    "status": status,
                    "classification": _load_json(row["classification_json"]),
                    "sentiment": _load_json(row["sentiment_json"]),
                    "groups": groups,
                    "discovery": bool(row["discovery"]),
                }
            )
        return result

    def _like_results(self, automation_id: str, limit: int) -> list[dict[str, Any]]:
        auto = self._automation_row(automation_id)
        start_cursor = int(auto["start_cursor"])
        routing_cursor = int(auto["routing_cursor"])
        conn = self._ensure_open()
        rows = conn.execute(
            """
            SELECT
                d.delta_id,d.source_seq,d.like_uri,d.actor_did,d.subject_uri,d.subject_cid,
                d.content_version,d.delta,d.operation,d.event_time,d.observed_at,
                m.post_uri,m.groups_json,m.discovery,m.first_seq,
                d.subject_cid AS cid,v.text,v.text_hash,v.published_at,v.observed_at AS post_observed_at,
                w.status,w.classification_json,w.sentiment_json,
                p.current_deleted,p.current_content_version,p.current_text,
                p.current_event_seq,p.current_observed_at
            FROM like_deltas d
            JOIN automation_matches m
              ON m.automation_id=? AND m.post_uri=d.subject_uri AND m.content_version=d.content_version
            JOIN post_versions v
              ON v.post_uri=d.subject_uri AND v.content_version=d.content_version
            LEFT JOIN automation_work w
              ON w.automation_id=m.automation_id AND w.content_version=m.content_version
            LEFT JOIN posts p ON p.post_uri=m.post_uri
            WHERE d.source_seq>? AND d.source_seq<=? AND d.resolved=1
            ORDER BY d.source_seq DESC,d.delta_id DESC
            LIMIT ?
            """,
            (automation_id, start_cursor, routing_cursor, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            groups = _load_json(row["groups_json"], [])
            if not isinstance(groups, list):
                groups = []
            result.append(
                {
                    "automation_id": automation_id,
                    "seq": int(row["source_seq"]),
                    "like_uri": row["like_uri"],
                    "actor_did": row["actor_did"],
                    "subject_uri": row["subject_uri"],
                    "subject_cid": row["subject_cid"],
                    "post_uri": row["post_uri"],
                    "content_version": row["content_version"],
                    "cid": row["cid"],
                    "text": row["text"],
                    "text_hash": row["text_hash"],
                    "publication_time": row["published_at"],
                    "post_event_time": row["post_observed_at"],
                    "event_time": row["event_time"],
                    "observed_at": row["observed_at"],
                    "operation": row["operation"],
                    "likes_delta": int(row["delta"]),
                    "current_deleted": bool(row["current_deleted"])
                    if row["current_deleted"] is not None
                    else False,
                    "status": row["status"] or "pending",
                    "classification": _load_json(row["classification_json"]),
                    "sentiment": _load_json(row["sentiment_json"]),
                    "groups": groups,
                    "discovery": bool(row["discovery"]),
                }
            )
        return result

    def results(
        self, automation_id: str, kind: str = "posts", limit: int = 100
    ) -> list[dict[str, Any]]:
        automation_id = _required_text(automation_id, "automation_id")
        limit = _valid_limit(limit, 100, 1000)
        self._automation_row(automation_id)
        if kind not in {"posts", "likes"}:
            raise ValueError("kind must be posts or likes")
        if kind == "posts":
            return self._post_results(automation_id, limit)
        return self._like_results(automation_id, limit)


__all__ = ["Store"]
