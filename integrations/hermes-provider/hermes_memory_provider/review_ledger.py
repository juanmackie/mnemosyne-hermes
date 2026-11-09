"""Content-free local ledger for explicit-correction review outcomes.

The ledger is a sidecar JSONL file beside the provider database. It records
what was reviewed, the verdict, content hashes and explicit operator outcomes;
it never stores memory text, transcript text, citation quotes, model reasons or
replacement text. It is a diagnostic record, not a second memory store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# LOCAL PATCH: P42 the ledger is append-only local measurement, not memory.
FORMAT = "mnemosyne-review-ledger/v1"
STATS_FORMAT = "mnemosyne-review-stats/v1"
PENDING_FORMAT = "mnemosyne-review-pending/v1"
TRIGGERS = ("explicit_correction",)
VERDICTS = ("supported", "contradicted", "insufficient")
OUTCOMES = ("accepted", "rejected", "corrected_again")
OUTCOME_SOURCES = ("explicit", "observed_rerun")
OBSERVED_STATES = (
    "matches_original",
    "matches_proposal",
    "changed_otherwise",
    "missing",
)
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_KEYS = {
    "format", "event", "review_id", "recorded_at", "trigger", "verdict",
    "proposal_id", "memory_id", "memory_store", "session_id", "all_sessions",
    "original_sha256", "evidence_sha256", "replacement_sha256",
    "correction_message_id",
}
_OUTCOME_KEYS = {
    "format", "event", "proposal_id", "recorded_at", "outcome", "source", "observed",
}


class ReviewLedgerError(RuntimeError):
    """The local review ledger cannot be read, validated or written safely."""


def ledger_path(db_path: str | Path) -> Path:
    """Return the sidecar path for a provider database."""
    path = Path(str(db_path)).expanduser()
    return path.with_name(path.name + ".review-ledger.jsonl")


def content_sha256(text: str) -> str:
    """Hash memory/replacement text without retaining the text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _review_id(report: dict[str, Any], trigger: str, session_id: str, all_sessions: bool) -> str:
    """Stable ID over the proposal inputs; rerunning the same check is idempotent."""
    payload = {
        "trigger": trigger,
        "memory_id": str(report["memory_id"]),
        "memory_store": str(report["memory_store"]),
        "session_id": session_id,
        "all_sessions": bool(all_sessions),
        "original_sha256": str(report["original_sha256"]),
        "evidence_sha256": str(report["evidence_sha256"]),
        "verdict": str(report["verdict"]),
        "replacement": report.get("replacement"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _append(path: Path, event: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise ReviewLedgerError("Review ledger directory does not exist")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReviewLedgerError("Review ledger path is not a regular file")
    line = json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ReviewLedgerError(
            f"Review ledger could not be written ({type(exc).__name__})"
        ) from exc


def _validate_review(event: dict[str, Any], number: int) -> None:
    if set(event) != _REVIEW_KEYS:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid review schema")
    if not _HEX16.fullmatch(str(event["review_id"])):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid review ID")
    if not _identifier(event["recorded_at"]):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid timestamp")
    if event["trigger"] not in TRIGGERS:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid trigger")
    if event["verdict"] not in VERDICTS:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid verdict")
    if not _identifier(event["session_id"]):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid session ID")
    if not isinstance(event["all_sessions"], bool):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid visibility flag")
    proposal_id = event["proposal_id"]
    replacement_sha256 = event["replacement_sha256"]
    contradicted = event["verdict"] == "contradicted"
    if contradicted:
        if proposal_id is None or not _HEX16.fullmatch(str(proposal_id)):
            raise ReviewLedgerError(f"Review ledger line {number} has an invalid proposal ID")
        if replacement_sha256 is None or not _HEX64.fullmatch(str(replacement_sha256)):
            raise ReviewLedgerError(
                f"Review ledger line {number} has an invalid replacement hash"
            )
    elif proposal_id is not None or replacement_sha256 is not None:
        raise ReviewLedgerError(
            f"Review ledger line {number} proposes an edit without a contradiction"
        )
    if not _identifier(event["memory_id"]):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid memory ID")
    if not _identifier(event["memory_store"]):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid memory store")
    for field in ("original_sha256", "evidence_sha256"):
        if not _HEX64.fullmatch(str(event[field])):
            raise ReviewLedgerError(f"Review ledger line {number} has an invalid {field}")
    correction_message_id = event["correction_message_id"]
    if correction_message_id is not None and not _identifier(correction_message_id):
        raise ReviewLedgerError(
            f"Review ledger line {number} has an invalid correction message ID"
        )


def _validate_outcome(event: dict[str, Any], number: int) -> None:
    if set(event) != _OUTCOME_KEYS:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid outcome schema")
    if not _HEX16.fullmatch(str(event["proposal_id"])):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid proposal ID")
    if event["outcome"] not in OUTCOMES:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid outcome")
    if event["source"] not in OUTCOME_SOURCES:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid outcome source")
    observed = event["observed"]
    if observed is not None and observed not in OBSERVED_STATES:
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid observation")
    if not _identifier(event["recorded_at"]):
        raise ReviewLedgerError(f"Review ledger line {number} has an invalid timestamp")


def _validate_event(event: Any, number: int) -> None:
    if not isinstance(event, dict):
        raise ReviewLedgerError(f"Review ledger line {number} is not an object")
    if event.get("format") != FORMAT:
        raise ReviewLedgerError(f"Review ledger line {number} has an unsupported format")
    if event.get("event") == "review":
        _validate_review(event, number)
    elif event.get("event") == "outcome":
        _validate_outcome(event, number)
    else:
        raise ReviewLedgerError(f"Review ledger line {number} has an unknown event")


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate every event; a missing ledger is an empty selection."""
    ledger = Path(path)
    if not ledger.exists() and not ledger.is_symlink():
        return []
    if ledger.is_symlink() or not ledger.is_file():
        raise ReviewLedgerError("Review ledger path is not a regular file")
    events: list[dict[str, Any]] = []
    try:
        with ledger.open("r", encoding="utf-8", newline="") as handle:
            for number, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise ReviewLedgerError(f"Review ledger line {number} is truncated")
                text = line[:-1]
                if not text.strip():
                    raise ReviewLedgerError(f"Review ledger line {number} is empty")
                try:
                    event = json.loads(text)
                except (ValueError, TypeError, RecursionError) as exc:
                    raise ReviewLedgerError(
                        f"Review ledger line {number} is not valid JSON"
                    ) from exc
                _validate_event(event, number)
                events.append(event)
    except (OSError, UnicodeError) as exc:
        raise ReviewLedgerError(
            f"Review ledger could not be read ({type(exc).__name__})"
        ) from exc
    return events


def _latest_outcomes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event"] == "outcome":
            latest[event["proposal_id"]] = event
    return latest


def find_proposal(events: list[dict[str, Any]], proposal_id: str) -> dict[str, Any]:
    """Return the most recent review event for a proposal ID."""
    if not isinstance(proposal_id, str) or not _HEX16.fullmatch(proposal_id):
        raise ReviewLedgerError("A valid proposal ID is required")
    for event in reversed(events):
        if event["event"] == "review" and event["proposal_id"] == proposal_id:
            return event
    raise ReviewLedgerError("Unknown proposal ID; record a --correction review first")


def _record_supersessions(
    path: Path,
    events: list[dict[str, Any]],
    new_event: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Mark a prior exact replacement as corrected again by this newer review.

    The new review's original text equals the prior proposal's exact replacement,
    so the prior proposal was applied; the new contradiction supersedes it.
    No other inference is made and no memory is edited.
    """
    if new_event["verdict"] != "contradicted":
        return [], []
    prior: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event"] != "review" or event["verdict"] != "contradicted":
            continue
        if (
            event["memory_id"] != new_event["memory_id"]
            or event["memory_store"] != new_event["memory_store"]
        ):
            continue
        prior.setdefault(event["review_id"], event)
    latest = _latest_outcomes(events)
    superseded: list[str] = []
    warnings: list[str] = []
    for review in prior.values():
        if review["review_id"] == new_event["review_id"]:
            continue
        if review["replacement_sha256"] != new_event["original_sha256"]:
            continue
        if review["replacement_sha256"] == new_event["replacement_sha256"]:
            continue
        if latest.get(review["proposal_id"], {}).get("outcome") == "corrected_again":
            continue
        try:
            _append(
                path,
                {
                    "format": FORMAT,
                    "event": "outcome",
                    "proposal_id": review["proposal_id"],
                    "recorded_at": _timestamp(),
                    "outcome": "corrected_again",
                    "source": "observed_rerun",
                    "observed": "matches_proposal",
                },
            )
            superseded.append(review["proposal_id"])
        except ReviewLedgerError as exc:
            warnings.append(
                f"could not record supersession for {review['proposal_id']}: {exc}"
            )
    return superseded, warnings


def record_review(
    db_path: str | Path,
    report: dict[str, Any],
    *,
    trigger: str = "explicit_correction",
    correction_message_id: str | None = None,
    session_id: str = "hermes_default",
    all_sessions: bool = False,
) -> dict[str, Any]:
    """Append one content-free review event and return its stable IDs."""
    if trigger not in TRIGGERS:
        raise ReviewLedgerError("Unsupported review trigger")
    verdict = report.get("verdict")
    if verdict not in VERDICTS:
        raise ReviewLedgerError("Review report has no valid verdict")
    if not _identifier(session_id) or not isinstance(all_sessions, bool):
        raise ReviewLedgerError("Review report has an invalid visibility selection")
    for field in ("original_sha256", "evidence_sha256"):
        if not _HEX64.fullmatch(str(report.get(field))):
            raise ReviewLedgerError(f"Review report has no valid {field}")
    for field in ("memory_id", "memory_store"):
        if not _identifier(report.get(field)):
            raise ReviewLedgerError(f"Review report has no valid {field}")
    replacement = None
    if verdict == "contradicted":
        replacement = report.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ReviewLedgerError("A contradicted review requires a replacement proposal")
    if correction_message_id is not None and not _identifier(correction_message_id):
        raise ReviewLedgerError("Correction message ID is invalid")

    path = ledger_path(db_path)
    past_events = read_events(path)
    review_id = _review_id(report, trigger, session_id, all_sessions)
    event = {
        "format": FORMAT,
        "event": "review",
        "review_id": review_id,
        "recorded_at": _timestamp(),
        "trigger": trigger,
        "verdict": verdict,
        "proposal_id": review_id if verdict == "contradicted" else None,
        "memory_id": str(report["memory_id"]),
        "memory_store": str(report["memory_store"]),
        "session_id": session_id,
        "all_sessions": all_sessions,
        "original_sha256": str(report["original_sha256"]),
        "evidence_sha256": str(report["evidence_sha256"]),
        "replacement_sha256": content_sha256(replacement) if replacement is not None else None,
        "correction_message_id": correction_message_id,
    }
    _append(path, event)
    superseded, warnings = _record_supersessions(path, past_events, event)
    return {
        "review_id": review_id,
        "proposal_id": event["proposal_id"],
        "ledger_path": str(path),
        "superseded": superseded,
        "warnings": warnings,
    }


def record_outcome(
    db_path: str | Path,
    proposal_id: str,
    outcome: str,
    *,
    observed: str | None = None,
    source: str = "explicit",
) -> dict[str, Any]:
    """Append an explicit decision for a proposal already in the ledger."""
    if outcome not in OUTCOMES:
        raise ReviewLedgerError("Unsupported review outcome")
    if source not in OUTCOME_SOURCES:
        raise ReviewLedgerError("Unsupported outcome source")
    if observed is not None and observed not in OBSERVED_STATES:
        raise ReviewLedgerError("Unsupported observation state")
    if not isinstance(proposal_id, str) or not _HEX16.fullmatch(proposal_id):
        raise ReviewLedgerError("A valid proposal ID is required")
    path = ledger_path(db_path)
    events = read_events(path)
    find_proposal(events, proposal_id)
    _append(
        path,
        {
            "format": FORMAT,
            "event": "outcome",
            "proposal_id": proposal_id,
            "recorded_at": _timestamp(),
            "outcome": outcome,
            "source": source,
            "observed": observed,
        },
    )
    return {
        "proposal_id": proposal_id,
        "outcome": outcome,
        "source": source,
        "observed": observed,
        "ledger_path": str(path),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def pending_proposals(events: list[dict[str, Any]], limit: int = 20) -> dict[str, Any]:
    """List proposals with no recorded outcome, oldest first."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ReviewLedgerError("limit must be between 1 and 100")
    reviews: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event"] == "review" and event["proposal_id"] is not None:
            reviews.setdefault(event["proposal_id"], event)
    latest = _latest_outcomes(events)
    for proposal_id in latest:
        if proposal_id not in reviews:
            raise ReviewLedgerError("Review ledger has an outcome for an unknown proposal")
    pending = [event for proposal_id, event in reviews.items() if proposal_id not in latest]
    pending.sort(key=lambda event: (event["recorded_at"], event["proposal_id"]))
    shown = pending[:limit]
    return {
        "format": PENDING_FORMAT,
        "count": len(pending),
        "shown": len(shown),
        "truncated": len(pending) > limit,
        "pending": [
            {
                "proposal_id": event["proposal_id"],
                "memory_id": event["memory_id"],
                "memory_store": event["memory_store"],
                "verdict": event["verdict"],
                "recorded_at": event["recorded_at"],
                "correction_message_id": event["correction_message_id"],
            }
            for event in shown
        ],
        "coverage": "proposals with no recorded outcome",
    }


def summarize(events: list[dict[str, Any]], path: str | Path) -> dict[str, Any]:
    """Fold review and outcome events into counts and observed-outcome rates."""
    ledger = Path(path)
    reviews: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event"] == "review":
            # The review ID hashes every semantic input; a repeated check is
            # the same review even though its timestamp differs.
            reviews.setdefault(event["review_id"], event)
    latest = _latest_outcomes(events)
    proposal_ids = {
        event["proposal_id"] for event in reviews.values() if event["proposal_id"] is not None
    }
    for proposal_id in latest:
        if proposal_id not in proposal_ids:
            raise ReviewLedgerError("Review ledger has an outcome for an unknown proposal")
    decided = [latest[proposal_id] for proposal_id in proposal_ids if proposal_id in latest]
    accepted = sum(event["outcome"] == "accepted" for event in decided)
    accepted_verified = sum(
        event["outcome"] == "accepted" and event["observed"] == "matches_proposal"
        for event in decided
    )
    rejected = sum(event["outcome"] == "rejected" for event in decided)
    corrected_again = sum(event["outcome"] == "corrected_again" for event in decided)
    return {
        "format": STATS_FORMAT,
        "ledger": str(ledger),
        "ledger_exists": ledger.exists(),
        "reviews": len(reviews),
        "triggers": {
            trigger: sum(event["trigger"] == trigger for event in reviews.values())
            for trigger in TRIGGERS
        },
        "verdicts": {
            verdict: sum(event["verdict"] == verdict for event in reviews.values())
            for verdict in VERDICTS
        },
        "proposals": len(proposal_ids),
        "proposal_rate": _rate(len(proposal_ids), len(reviews)),
        "pending": len(proposal_ids) - len(decided),
        "accepted": accepted,
        "accepted_verified": accepted_verified,
        "accepted_unverified": accepted - accepted_verified,
        "rejected": rejected,
        "corrected_again": corrected_again,
        "decided": len(decided),
        "acceptance_rate": _rate(accepted, len(decided)),
        "verified_acceptance_rate": _rate(accepted_verified, len(decided)),
        "false_correction_rate": _rate(rejected + corrected_again, len(decided)),
        "outcome_sources": {
            source: sum(event["source"] == source for event in decided)
            for source in OUTCOME_SOURCES
        },
        "observations": {
            state: sum((event["observed"] or "unobserved") == state for event in decided)
            for state in (*OBSERVED_STATES, "unobserved")
        },
        "coverage": (
            "explicit and observed outcomes only; accepted_verified requires the current "
            "memory to equal the recorded replacement hash"
        ),
    }
