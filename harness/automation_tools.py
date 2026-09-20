"""Chat-authored v2 proposals. Offline validation only; no collection or inference."""
import functools
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import steps

ROOT = Path(__file__).resolve().parent / "automation_config"
TTL_MS = 15 * 60 * 1000
MAX_JSON_BYTES = 200_000


class ProposalError(ValueError):
    pass


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _directory():
    return Path(os.environ["HARNESS_SESSION_DIR"])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate(config):
    from automation_config.configure_automation import _build_result
    return _build_result(config)


def _card(proposal):
    config = proposal["configuration"]
    return {"kind": "automation_proposal", "id": proposal["proposal_hash"], "title": proposal["title"],
            "revision": proposal["revision"], "status": proposal["status"], "configuration": config,
            "open_questions": proposal["open_questions"], "validation": proposal["validation"],
            "target_labels": [target["label"] for target in config["targets"]],
            "rules": config["categorization"]["rules"], "cutoff": config["categorization"]["accept_probability"]}


def _result(proposal):
    return {"ok": True, "proposal": proposal, "_card": _card(proposal),
            "execution_started": False}


def with_errors(function):
    @functools.wraps(function)
    def wrapped(reason=None, *args, **kwargs):
        if not isinstance(reason, str) or not reason.strip():
            return {"error": {"code": "no_reason", "message": "Explain why you are taking this step."}}
        try:
            return function(reason, *args, **kwargs)
        except (ValueError, TypeError, KeyError, OSError) as error:
            return {"error": {"code": "invalid_proposal", "message": str(error)[:8000],
                              "hint": "Correct the configuration and save it again. The previous valid revision is unchanged."}}
    return wrapped


def get_automation_contract(reason: str, example: str = "ai_companies") -> dict:
    """Read the exact automation v2 schema and a complete seed example before drafting.

    example: ai_companies or public_policy. Adapt the seed to the user's choices. No data or
    providers are queried. Fixed semantics and model must remain unchanged. Runtime/source/date
    settings are not fields of this configuration. Reference metadata is not classifier guidance.
    """
    names = {"ai_companies": "automation-config.current.json", "public_policy": "automation-config.public-policy.json"}
    if example not in names:
        raise ProposalError("Choose ai_companies or public_policy as the example.")
    return {"schema": _read(ROOT / "automation-config.schema.json"), "example": _read(ROOT / names[example]),
            "note": "Draft targets, retrieval terms and shared relevance rules. Fixed sentiment has five labels. No execution is authorized."}


def get_automation_proposal(reason: str) -> dict:
    """Read the latest saved proposal, including questions still to resolve, before revising it."""
    proposal = _read(_directory() / "automation-proposal.json")
    return _result(proposal) if proposal else {"ok": True, "proposal": None, "next": "Discuss the user's goal and load get_automation_contract."}


def save_automation_proposal(reason: str, title: str, config_json: str, open_questions: list[str]) -> dict:
    """Save a complete, compiler-validated v2 automation proposal and show a review card.

    Send the complete revised configuration as config_json, not a patch or an old project spec.
    Put remaining user-facing choices in open_questions, or [] when resolved. Each revision
    invalidates previous approvals. Validation constructs native requests but never runs models.
    Do not use this as evidence that an automation exists or classification quality is calibrated.
    """
    from automation_config.configure_automation import _parse_json_bytes
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise ProposalError("Use a proposal title between 1 and 120 characters.")
    if not isinstance(config_json, str) or len(config_json.encode("utf-8")) > MAX_JSON_BYTES:
        raise ProposalError("The configuration must be JSON text under 200 KB.")
    if not isinstance(open_questions, list) or len(open_questions) > 12 or any(not isinstance(q, str) or not q.strip() or len(q) > 500 for q in open_questions):
        raise ProposalError("Open questions must be a list of up to twelve short, nonblank questions.")
    config = _parse_json_bytes(config_json.encode("utf-8"), Path("proposal.json"))
    validation = _validate(config)
    content = {"title": title.strip(), "configuration": config, "open_questions": [q.strip() for q in open_questions]}
    directory = _directory()
    previous = _read(directory / "automation-proposal.json")
    digest = _digest(content)
    if previous and previous["proposal_hash"] == digest:
        return _result(previous)
    proposal = {**content, "proposal_hash": digest, "revision": (previous["revision"] if previous else 0) + 1,
                "validation": validation, "status": "draft" if open_questions else "ready", "updated_ms": int(time.time() * 1000)}
    _write(directory / "automation-proposal.json", proposal)
    # The approved handoff never survives a different proposal revision.
    (directory / "automation-final.json").unlink(missing_ok=True)
    for path in (directory / "confirmations").glob("*.json"):
        if (_read(path) or {}).get("kind") == "automation_proposal":
            path.unlink(missing_ok=True)
    return _result(proposal)


def request_automation_confirmation(reason: str) -> dict:
    """Ask the user to approve the current automation configuration for handoff, not execution.

    Only after all open questions are resolved and the proposal is saved. End your turn after
    calling. The button records the final JSON deterministically; never call submit_project.
    """
    directory = _directory()
    proposal = _read(directory / "automation-proposal.json")
    if not proposal:
        raise ProposalError("Save an automation proposal first.")
    if proposal["open_questions"]:
        raise ProposalError("Resolve the proposal's open questions before requesting confirmation.")
    _validate(proposal["configuration"])
    if proposal["status"] == "final":
        return _result(proposal)
    for path in (directory / "confirmations").glob("*.json"):
        if (_read(path) or {}).get("kind") == "automation_proposal":
            path.unlink(missing_ok=True)
    now = int(time.time() * 1000)
    record = {"kind": "automation_proposal", "confirmation_id": secrets.token_urlsafe(12),
              "spec_hash": proposal["proposal_hash"], "created_ms": now, "expires_ms": now + TTL_MS,
              "decision": None, "decided_ms": None, "consumed": False}
    _write(directory / "confirmations" / f"{record['confirmation_id']}.json", record)
    labels = ", ".join(target["label"] for target in proposal["configuration"]["targets"])
    return {**record, "summary": f"Approve revision {proposal['revision']} of {proposal['title']}, covering {labels}. This saves the final configuration. Collection and classification will not start.",
            "next": "Wait for the user's confirmation button. Do not submit or start a project."}


def decide_proposal(directory: Path, confirmation_id: str, approved: bool) -> dict:
    """Called only by the HTTP button handler. Bind approval to the exact saved revision."""
    if not isinstance(confirmation_id, str) or len(confirmation_id) != 16 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in confirmation_id):
        raise ProposalError("Invalid confirmation ID.")
    if not isinstance(approved, bool):
        raise ProposalError("A boolean decision is required.")
    path = directory / "confirmations" / f"{confirmation_id}.json"
    record = _read(path)
    proposal = _read(directory / "automation-proposal.json")
    now = int(time.time() * 1000)
    if not record or record.get("kind") != "automation_proposal" or not proposal:
        raise ProposalError("This proposal confirmation no longer exists.")
    if record["decision"] is not None or record.get("consumed"):
        raise ProposalError("This confirmation was already decided.")
    if record["expires_ms"] <= now:
        raise ProposalError("This confirmation expired. Ask for a new one.")
    content = {key: proposal[key] for key in ("title", "configuration", "open_questions")}
    if record["spec_hash"] != _digest(content) or proposal["open_questions"]:
        raise ProposalError("The proposal changed. Review and confirm its latest revision.")
    if approved:
        validation = _validate(proposal["configuration"])
        proposal.update(status="final", validation=validation)
        _write(directory / "automation-final.json", proposal["configuration"])
    else:
        proposal["status"] = "ready"
    record.update(decision="approved" if approved else "declined", decided_ms=now, consumed=True)
    _write(directory / "automation-proposal.json", proposal)
    _write(path, record)
    return _result(proposal)


TOOLS = (get_automation_contract, get_automation_proposal, save_automation_proposal, request_automation_confirmation)


def register(mcp):
    for tool in TOOLS:
        mcp.tool()(with_errors(tool))
    return TOOLS


for tool, title in (("get_automation_contract", "Checking the configuration format"),
                    ("get_automation_proposal", "Reading your proposal"),
                    ("save_automation_proposal", "Checking and saving your proposal"),
                    ("request_automation_confirmation", "Preparing your proposal for review")):
    steps.register_tool(tool, lambda fields, text=title: text,
                        lambda result, is_error: "" if is_error or result.get("error") else "Ready.")
