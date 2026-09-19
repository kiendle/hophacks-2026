"""Per-session harness state on disk: the draft, the state machine (§10) and single-use confirmations.

The MCP tool server and the web bridge are separate processes over the same
directory, so nothing is cached in memory: every method reads the files it needs
and every write is a temp file plus os.replace.  A torn or missing file reads as
absent, never as an exception, because a half-written draft must not wedge a session.

The confirmation record is the contract between the two processes: the bridge
writes the decision when the button is pressed, submit_project consumes it.
Model output alone can never create one.
"""
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

try:
    from harness.spec import canonical_hash
except ImportError:  # running with harness/ itself on sys.path
    from spec import canonical_hash

STATES = ("EXPLORING", "DRAFTING", "PREVIEWED", "VALIDATED", "AWAITING_CONFIRMATION",
          "CONFIRMED", "SUBMITTED", "READY", "FAILED")
CONFIRMATION_TTL_MS = 300_000
CONFIRMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")  # checked before any id reaches a path
EMPTY_STATE = {"state": "EXPLORING", "previewed_hash": None, "validated_hash": None, "project_id": None}


class StateError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class NotFound(StateError):
    pass


class Expired(StateError):
    pass


class AlreadyDecided(StateError):
    pass


def now_ms():
    return int(time.time() * 1000)


_now = now_ms  # the methods below take a `now_ms` argument, which shadows the name


def _write_json(path, payload):
    """Atomic within one directory: a reader sees either the old file or the new one."""
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value


class SessionState:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.confirmations_dir = self.directory / "confirmations"
        self.confirmations_dir.mkdir(parents=True, exist_ok=True)
        self.draft_path = self.directory / "draft.json"
        self.state_path = self.directory / "state.json"

    # --- reads -------------------------------------------------------------

    def read_state(self):
        stored = _read_json(self.state_path)
        state = dict(EMPTY_STATE)
        if isinstance(stored, dict):
            state.update(stored)
        if state.get("state") not in STATES:
            state["state"] = EMPTY_STATE["state"]
        return state

    def read_draft(self):
        return _read_json(self.draft_path)

    def draft_hash(self):
        draft = self.read_draft()
        return canonical_hash(draft) if draft is not None else None

    def read_confirmation(self, confirmation_id):
        return _read_json(self._confirmation_path(confirmation_id))

    def confirmations(self):
        records = [_read_json(path) for path in sorted(self.confirmations_dir.glob("*.json"))]
        records = [r for r in records if isinstance(r, dict) and CONFIRMATION_ID_RE.match(str(r.get("confirmation_id")))]
        return sorted(records, key=lambda r: r.get("created_ms") or 0)

    def summary(self):
        state = self.read_state()
        return {**state, "draft_hash": self.draft_hash(),
                "pending_confirmations": [r["confirmation_id"] for r in self.confirmations() if r.get("decision") is None]}

    # --- writes ------------------------------------------------------------

    def _put_state(self, **changes):
        state = self.read_state()
        state.update(changes)
        _write_json(self.state_path, state)
        return state

    def write_draft(self, spec):
        """Any edit invalidates the preview, the validation and every confirmation (§10)."""
        digest = canonical_hash(spec)
        _write_json(self.draft_path, spec)
        for path in self.confirmations_dir.glob("*.json"):
            path.unlink(missing_ok=True)
        self._put_state(state="DRAFTING", previewed_hash=None, validated_hash=None)
        return digest

    def mark_previewed(self, spec_hash):
        self._require_draft_hash(spec_hash, "preview")
        state = self.read_state()
        keep = state["validated_hash"] if state["validated_hash"] == spec_hash else None
        self._put_state(state="PREVIEWED", previewed_hash=spec_hash, validated_hash=keep)

    def mark_validated(self, spec_hash):
        self._require_draft_hash(spec_hash, "validation")
        if self.read_state()["previewed_hash"] != spec_hash:
            raise StateError("NOT_PREVIEWED", "this draft has not been previewed; run the preview before validating.")
        self._put_state(state="VALIDATED", validated_hash=spec_hash)

    def request_confirmation(self, now_ms=None):
        moment = now_ms if now_ms is not None else _now()
        state = self.read_state()
        digest = self.draft_hash()
        if state["state"] != "VALIDATED":
            raise StateError("NOT_VALIDATED", f"the session is {state['state']}, so there is nothing validated to confirm.")
        if digest is None or not digest == state["validated_hash"] == state["previewed_hash"]:
            raise StateError("HASH_MISMATCH", "the draft, the preview and the validation are not the same specification.")
        record = {
            "confirmation_id": secrets.token_urlsafe(12),
            "spec_hash": digest,
            "created_ms": moment,
            "expires_ms": moment + CONFIRMATION_TTL_MS,
            "decision": None,
            "decided_ms": None,
            "consumed": False,
        }
        _write_json(self._confirmation_path(record["confirmation_id"]), record)
        self._put_state(state="AWAITING_CONFIRMATION")
        return record

    def record_decision(self, confirmation_id, approved, now_ms=None):
        """Called by the bridge when the button is pressed; the model cannot reach it."""
        moment = now_ms if now_ms is not None else _now()
        path = self._confirmation_path(confirmation_id)
        record = _read_json(path)
        if not isinstance(record, dict):
            raise NotFound("NO_SUCH_CONFIRMATION", f"there is no confirmation {confirmation_id!r} in this session.")
        if moment >= record.get("expires_ms", 0):
            raise Expired("CONFIRMATION_EXPIRED", "this confirmation is older than five minutes; ask for a new one.")
        if record.get("decision") is not None:
            raise AlreadyDecided("ALREADY_DECIDED", f"this confirmation was already {record['decision']}.")
        record["decision"] = "approved" if approved else "declined"
        record["decided_ms"] = moment
        _write_json(path, record)
        self._put_state(state="CONFIRMED" if approved else "DRAFTING")
        return record

    def consume_confirmation(self, now_ms=None):
        """Single use: what submit_project checks before it can create anything."""
        moment = now_ms if now_ms is not None else _now()
        state = self.read_state()
        digest = self.draft_hash()
        records = self.confirmations()
        if not records:
            raise NotFound("NO_CONFIRMATION", "this session holds no confirmation; ask the user to confirm first.")
        usable = [r for r in records
                  if r.get("decision") == "approved" and not r.get("consumed")
                  and moment < r.get("expires_ms", 0) and digest is not None and r.get("spec_hash") == digest]
        if not usable:
            raise self._refusal(records[-1], digest, moment)
        record = usable[-1]
        if not record["spec_hash"] == state["validated_hash"] == state["previewed_hash"]:
            raise StateError("HASH_MISMATCH", "the confirmed specification is not the one that was previewed and validated.")
        if state["state"] != "CONFIRMED":
            raise StateError("WRONG_STATE", f"the session is {state['state']}, not CONFIRMED.")
        record["consumed"] = True
        _write_json(self._confirmation_path(record["confirmation_id"]), record)
        return record

    def mark_submitted(self, project_id):
        self._put_state(state="SUBMITTED", project_id=project_id)

    def mark_ready(self):
        self._put_state(state="READY")

    def mark_failed(self, reason):
        self._put_state(state="FAILED", failure_reason=str(reason))

    # --- helpers -----------------------------------------------------------

    def _confirmation_path(self, confirmation_id):
        if not CONFIRMATION_ID_RE.match(str(confirmation_id)):
            raise StateError("BAD_CONFIRMATION_ID", f"{confirmation_id!r} is not a confirmation id.")
        return self.confirmations_dir / f"{confirmation_id}.json"

    def _require_draft_hash(self, spec_hash, what):
        digest = self.draft_hash()
        if digest is None:
            raise StateError("NO_DRAFT", f"there is no draft to record a {what} for.")
        if digest != spec_hash:
            raise StateError("HASH_MISMATCH", f"the {what} was taken on another draft than the one on disk.")

    def _refusal(self, record, digest, moment):
        if record.get("consumed"):
            return StateError("ALREADY_CONSUMED", "this confirmation was already used to submit a project.")
        if record.get("decision") is None:
            return StateError("NOT_CONFIRMED", "the user has not pressed the button yet.")
        if record["decision"] == "declined":
            return StateError("DECLINED", "the user declined this project.")
        if moment >= record.get("expires_ms", 0):
            return Expired("CONFIRMATION_EXPIRED", "the confirmation expired before the project was submitted; ask again.")
        if digest is None:
            return StateError("NO_DRAFT", "there is no draft to submit.")
        return StateError("HASH_MISMATCH", "the draft changed after it was confirmed, so the confirmation is void.")
