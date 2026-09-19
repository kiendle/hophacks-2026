# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4"]
# ///
"""Run: python -m uv run harness/tests/test_state.py

The confirmation gate is the only thing standing between model output and a
created project, so every way it can be abused gets a case: replay, expiry,
decline, an edited draft, a forged id and a path-traversal id.
"""
import atexit
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state as ST  # noqa: E402

SPEC = {"spec_version": 1, "name": "AI safety backlash", "filter": {"any_terms": ["anthropic"]}}
OTHER = {"spec_version": 1, "name": "AI safety backlash", "filter": {"any_terms": ["anthropic", "claude"]}}
T0 = 1_760_000_000_000
CASES = []


def case(function):
    CASES.append(function)
    return function


def session():
    directory = tempfile.mkdtemp(prefix="harness-state-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return ST.SessionState(directory)


def validated(spec=SPEC):
    """A session driven to VALIDATED, the only state that can ask for a confirmation."""
    session_state = session()
    digest = session_state.write_draft(spec)
    session_state.mark_previewed(digest)
    session_state.mark_validated(digest)
    return session_state, digest


def raises(code, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except ST.StateError as error:
        assert error.code == code, f"expected {code}, got {error.code}: {error.message}"
        return error
    raise AssertionError(f"expected StateError {code}, nothing was raised")


@case
def happy_path_to_submitted():
    session_state, digest = validated()
    record = session_state.request_confirmation(now_ms=T0)
    assert session_state.read_state()["state"] == "AWAITING_CONFIRMATION"
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0 + 2_000)
    assert session_state.read_state()["state"] == "CONFIRMED"
    consumed = session_state.consume_confirmation(now_ms=T0 + 3_000)
    assert consumed["consumed"] is True and consumed["spec_hash"] == digest
    session_state.mark_submitted("proj-1")
    state = session_state.read_state()
    assert state["state"] == "SUBMITTED" and state["project_id"] == "proj-1"
    assert state["previewed_hash"] == state["validated_hash"] == digest
    session_state.mark_ready()
    assert session_state.read_state()["state"] == "READY"


@case
def confirmation_file_shape_is_the_contract():
    session_state, digest = validated()
    record = session_state.request_confirmation(now_ms=T0)
    assert set(record) == {"confirmation_id", "spec_hash", "created_ms", "expires_ms",
                           "decision", "decided_ms", "consumed"}, record
    assert ST.CONFIRMATION_ID_RE.match(record["confirmation_id"]), record["confirmation_id"]
    assert len(record["confirmation_id"]) == 16
    assert record["spec_hash"] == digest and record["created_ms"] == T0
    assert record["expires_ms"] == T0 + 300_000
    assert record["decision"] is None and record["decided_ms"] is None and record["consumed"] is False
    on_disk = json.loads((session_state.confirmations_dir / f"{record['confirmation_id']}.json").read_text("utf-8"))
    assert on_disk == record


@case
def edit_after_validation_drops_to_drafting():
    session_state, _ = validated()
    state = session_state.read_state()
    assert state["state"] == "VALIDATED" and state["previewed_hash"] == state["validated_hash"]
    digest = session_state.write_draft(OTHER)
    state = session_state.read_state()
    assert state["state"] == "DRAFTING"
    assert state["previewed_hash"] is None and state["validated_hash"] is None
    assert session_state.draft_hash() == digest
    raises("NOT_VALIDATED", session_state.request_confirmation, now_ms=T0)


@case
def edit_after_approval_deletes_the_confirmation():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0)
    session_state.write_draft(OTHER)
    assert session_state.confirmations() == []
    assert list(session_state.confirmations_dir.glob("*")) == []
    raises("NO_CONFIRMATION", session_state.consume_confirmation, now_ms=T0 + 1_000)


@case
def edited_draft_behind_our_back_voids_the_confirmation():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0)
    session_state.draft_path.write_text(json.dumps(OTHER), encoding="utf-8")  # not through write_draft
    raises("HASH_MISMATCH", session_state.consume_confirmation, now_ms=T0 + 1_000)


@case
def expired_confirmation_cannot_be_decided_or_consumed():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    error = raises("CONFIRMATION_EXPIRED", session_state.record_decision,
                   record["confirmation_id"], True, now_ms=T0 + 300_000)
    assert isinstance(error, ST.Expired)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0 + 299_999)
    error = raises("CONFIRMATION_EXPIRED", session_state.consume_confirmation, now_ms=T0 + 300_001)
    assert isinstance(error, ST.Expired)


@case
def a_decision_cannot_be_changed():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0 + 10)
    error = raises("ALREADY_DECIDED", session_state.record_decision,
                   record["confirmation_id"], False, now_ms=T0 + 20)
    assert isinstance(error, ST.AlreadyDecided)
    assert session_state.read_state()["state"] == "CONFIRMED"


@case
def a_confirmation_is_single_use():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0)
    session_state.consume_confirmation(now_ms=T0 + 1_000)
    raises("ALREADY_CONSUMED", session_state.consume_confirmation, now_ms=T0 + 2_000)


@case
def declined_confirmation_cannot_be_consumed():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], False, now_ms=T0)
    assert session_state.read_state()["state"] == "DRAFTING"
    raises("DECLINED", session_state.consume_confirmation, now_ms=T0 + 1_000)


@case
def an_undecided_confirmation_cannot_be_consumed():
    session_state, _ = validated()
    session_state.request_confirmation(now_ms=T0)
    raises("NOT_CONFIRMED", session_state.consume_confirmation, now_ms=T0 + 1_000)


@case
def decision_on_an_unknown_id():
    session_state, _ = validated()
    error = raises("NO_SUCH_CONFIRMATION", session_state.record_decision, "A" * 16, True, now_ms=T0)
    assert isinstance(error, ST.NotFound)


@case
def confirmation_ids_never_reach_a_path():
    session_state, _ = validated()
    for forged in ("../../x", "../" * 4 + "state", "a/b", "a" * 15, "a" * 17, "", "x.json", "ünicode-id-1234",
                   "C:/Windows/win.ini", "..", "a" * 16 + "/../../x"):
        raises("BAD_CONFIRMATION_ID", session_state.record_decision, forged, True, now_ms=T0)
        raises("BAD_CONFIRMATION_ID", session_state.read_confirmation, forged)
    assert sorted(p.name for p in session_state.directory.iterdir()) == ["confirmations", "draft.json", "state.json"]
    assert list(session_state.confirmations_dir.glob("*")) == []


@case
def validation_needs_a_preview_of_the_same_draft():
    session_state = session()
    digest = session_state.write_draft(SPEC)
    raises("NOT_PREVIEWED", session_state.mark_validated, digest)
    session_state.mark_previewed(digest)
    raises("HASH_MISMATCH", session_state.mark_validated, "0" * 64)
    raises("HASH_MISMATCH", session_state.mark_previewed, "0" * 64)
    session_state.mark_validated(digest)
    assert session_state.read_state()["state"] == "VALIDATED"
    empty = session()
    raises("NO_DRAFT", empty.mark_previewed, "0" * 64)


@case
def torn_and_missing_files_read_as_absent():
    session_state = session()
    assert session_state.read_state() == ST.EMPTY_STATE
    assert session_state.read_draft() is None and session_state.draft_hash() is None
    session_state.write_draft(SPEC)
    session_state.state_path.write_text('{"state": "VALID', encoding="utf-8")
    assert session_state.read_state() == ST.EMPTY_STATE
    session_state.draft_path.write_text('{"spec_version": 1, "nam', encoding="utf-8")
    assert session_state.read_draft() is None and session_state.draft_hash() is None
    (session_state.confirmations_dir / "AAAAAAAAAAAAAAAA.json").write_text('{"confirmation_', encoding="utf-8")
    assert session_state.confirmations() == []


@case
def writes_are_atomic_and_leave_no_temp_files():
    session_state, _ = validated()
    record = session_state.request_confirmation(now_ms=T0)
    session_state.record_decision(record["confirmation_id"], True, now_ms=T0)
    session_state.consume_confirmation(now_ms=T0)
    session_state.mark_submitted("proj-2")
    session_state.mark_failed("the pipeline lost the run")
    leftovers = [p.name for p in list(session_state.directory.rglob("*")) if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers
    state = session_state.read_state()
    assert state["state"] == "FAILED" and state["project_id"] == "proj-2"
    assert state["failure_reason"] == "the pipeline lost the run"


@case
def two_processes_share_one_directory():
    writer, digest = validated()
    reader = ST.SessionState(writer.directory)  # the bridge; `writer` is the MCP server
    assert reader.read_draft() == SPEC and reader.draft_hash() == digest
    record = writer.request_confirmation(now_ms=T0)
    assert reader.read_state()["state"] == "AWAITING_CONFIRMATION"
    assert [r["confirmation_id"] for r in reader.confirmations()] == [record["confirmation_id"]]
    reader.record_decision(record["confirmation_id"], True, now_ms=T0 + 500)  # the button is pressed in the browser
    assert writer.read_state()["state"] == "CONFIRMED"
    consumed = writer.consume_confirmation(now_ms=T0 + 600)
    assert consumed["decided_ms"] == T0 + 500
    assert reader.read_confirmation(record["confirmation_id"])["consumed"] is True
    writer.mark_submitted("proj-3")
    assert reader.summary()["project_id"] == "proj-3" and reader.summary()["pending_confirmations"] == []


def main():
    failures = 0
    for check in CASES:
        try:
            check()
            print(f"PASS {check.__name__}")
        except Exception as error:
            failures += 1
            print(f"FAIL {check.__name__}: {type(error).__name__}: {error}")
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
