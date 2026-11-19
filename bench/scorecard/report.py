"""Report builders: arm reports and paired comparisons.

An arm report is self-describing: it carries the corpus digest, the engine
attestation (which files are patched, original, or unknown), the provider tree
hash, the environment knobs, and the per-probe records the aggregation needs.
That is what makes a number in a document checkable later.

A comparison report adds paired bootstrap CIs per criterion and an explicit
verdict block: which pass criteria the candidate met, and which it missed.
"""

from __future__ import annotations

import importlib.metadata
import platform
import sys
from typing import Any

from .aggregate import summarize_surface
from .runner import engine_attestation, provider_tree_hashes

SCHEMA = "mnemosyne-scorecard/v1"

# The four pass criteria from the work order. Every report states them, and a
# comparison says pass/fail per criterion — including when it fails.
CRITERIA = {
    "fabricated_entity_zero_rows": (
        "fabricated-entity queries return 0 rows at <=1 point answerable-recall cost"
    ),
    "expired_facts_zero_served": "0 expired facts served as current",
    "secrets_zero_stored": "0 planted secrets stored",
    "precisionmembench_active_passes": (
        "PrecisionMemBench active passes >=35/43 with <=8 missing beliefs"
    ),
}

INFORMATIONAL_CRITERIA: set[str] = set()


def _environment(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
    }
    try:
        payload["mnemosyne_memory"] = importlib.metadata.version("mnemosyne-memory")
    except importlib.metadata.PackageNotFoundError:
        payload["mnemosyne_memory"] = None
    if extra:
        payload.update(extra)
    return payload


def _error_summary(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Group arm errors so a broken run cannot read as a good one.

    A run whose probes mostly errored has `null` metrics and a non-zero
    `error_rate`; the CLI refuses to call it a measurement. Grouping by message
    keeps a single systemic failure (a broken patch state) visible as one cause
    rather than as a thousand lines.
    """
    by_message: dict[str, int] = {}
    for error in errors:
        by_message[error.get("error", "unknown")] = (
            by_message.get(error.get("error", "unknown"), 0) + 1
        )
    return {
        "count": len(errors),
        "by_message": dict(sorted(by_message.items(), key=lambda item: -item[1])[:10]),
        "examples": errors[:5],
    }


def build_arm_report(
    merged: dict[str, Any],
    *,
    corpus: dict[str, Any],
    probes: list[dict[str, Any]],
    corpus_digest_value: str,
    arm_name: str,
    engine_state: dict[str, Any],
    ingest: str,
    limit: int,
    workers: int,
    split: str,
) -> dict[str, Any]:
    tool = summarize_surface(probes, merged["records"], "tool")
    prefetch = summarize_surface(probes, merged["records"], "prefetch")
    secret = merged.get("secret_audit") or {"planted": 0, "stored": 0}
    return {
        "schema": SCHEMA,
        "arm": arm_name,
        "split": split,
        "corpus_digest": corpus_digest_value,
        "corpus_counts": corpus.get("counts"),
        "probes_in_split": len(probes),
        "probes_measured": merged["probes_measured"],
        "lives_measured": merged.get("lives_measured"),
        "engine_state": {
            "kind": engine_state.get("kind"),
            "installed_tree_untouched": engine_state.get("write_free"),
            "overlay_files": engine_state.get("files"),
        },
        "engine_attestation": engine_attestation(),
        "provider_tree_sha256": provider_tree_hashes(),
        "environment": _environment(
            {
                "ingest_path": ingest,
                "recall_limit": limit,
                "workers": workers,
                "embeddings": "on (opt-out scrubbed)",
            }
        ),
        "wall_seconds": merged["wall_seconds"],
        "probe_errors": _error_summary(merged["errors"]),
        "errors": merged["errors"],
        "surfaces": {"tool": tool, "prefetch": prefetch},
        "secret_audit": {
            "planted": secret.get("planted", 0),
            "stored": secret.get("stored", 0),
            "informational": False,
            "note": "capture-time redaction is enforced by the pinned engine patch",
        },
        "headline": {
            "arm": arm_name,
            "probes_measured": merged["probes_measured"],
            "wall_seconds": merged["wall_seconds"],
            "tool_answerable_recall": tool["answerable_recall"],
            "tool_fabricated_queries_with_rows": tool["fabricated_entity_queries_returning_rows"],
            "tool_fabricated_queries": tool["fabricated_entity_queries"],
            "tool_expired_served": tool["expired_facts_served_as_current"],
            "tool_stale_outranking_current": tool["stale_outranking_current"],
            "prefetch_answerable_recall": prefetch["answerable_recall"],
            "prefetch_expired_present": prefetch["by_kind"]["expired"]["expired_present_total"],
            # Carried lessons (probe kind `carried_lesson`); `None` when the
            # measured split holds none, so an absent measurement is never 0.
            # Each field names its surface; nothing is summed across surfaces.
            "lesson_probes": tool["lesson_probes"],
            "tool_lesson_recall": tool["lesson_recall"],
            "prefetch_lesson_recall": prefetch["lesson_recall"],
            "tool_lesson_provenance_present": tool["lesson_provenance_present"],
            "tool_lesson_wrong_model": tool["lesson_wrong_model"],
            "prefetch_lesson_wrong_model": prefetch["lesson_wrong_model"],
            "errors": len(merged["errors"]),
            "error_rate": (
                len(merged["errors"]) / (len(merged["errors"]) + len(probes)) if probes else None
            ),
            "valid": len(merged["errors"]) == 0,
        },
        # Per-probe records are the analysis input; kept so a report can be
        # re-aggregated or bootstrapped without re-running the engine.
        "records": merged["records"],
        "candidate_dumps": merged["candidate_dumps"],
    }


def _criterion_verdicts(
    tool: dict[str, Any],
    baseline_tool: dict[str, Any],
    secret: dict[str, Any],
) -> dict[str, Any]:
    recall_cost = None
    if (
        baseline_tool.get("answerable_recall") is not None
        and tool.get("answerable_recall") is not None
    ):
        recall_cost = (baseline_tool["answerable_recall"] - tool["answerable_recall"]) * 100
    fabricated_rows = tool["fabricated_entity_queries_returning_rows"]
    verdicts = {
        "fabricated_entity_zero_rows": {
            "statement": CRITERIA["fabricated_entity_zero_rows"],
            "value": fabricated_rows,
            "queries": tool["fabricated_entity_queries"],
            "answerable_recall_cost_points": recall_cost,
            "passed": fabricated_rows == 0 and (recall_cost is None or recall_cost <= 1.0),
            "informational": False,
        },
        "expired_facts_zero_served": {
            "statement": CRITERIA["expired_facts_zero_served"],
            "value": tool["expired_facts_served_as_current"],
            "expired_probes": tool["expired_probes"],
            "passed": tool["expired_facts_served_as_current"] == 0,
            "informational": False,
        },
        "secrets_zero_stored": {
            "statement": CRITERIA["secrets_zero_stored"],
            "value": secret.get("stored"),
            "planted": secret.get("planted"),
            "passed": secret.get("stored") == 0,
            "informational": False,
        },
        "precisionmembench_active_passes": {
            "statement": CRITERIA["precisionmembench_active_passes"],
            "value": None,
            "passed": None,
            "informational": False,
            "note": "measured separately by bench/precision_recall_eval.py",
        },
    }
    return verdicts


def comparison_report(
    *,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    blocks: dict[str, Any],
    split: str,
    resamples: int,
) -> dict[str, Any]:
    tool = candidate["surfaces"]["tool"]
    baseline_tool = baseline["surfaces"]["tool"]
    verdicts = _criterion_verdicts(tool, baseline_tool, candidate.get("secret_audit") or {})
    missed = [
        key
        for key, verdict in verdicts.items()
        if verdict["passed"] is False and not verdict.get("informational")
    ]
    return {
        "schema": SCHEMA,
        "kind": "comparison",
        "split": split,
        "resamples": resamples,
        "baseline": {
            "arm": baseline["arm"],
            "corpus_digest": baseline["corpus_digest"],
            "engine_state": baseline["engine_state"],
            "headline": baseline.get("headline"),
        },
        "candidate": {
            "arm": candidate["arm"],
            "corpus_digest": candidate["corpus_digest"],
            "engine_state": candidate["engine_state"],
            "headline": candidate.get("headline"),
        },
        "paired": blocks,
        "criteria": verdicts,
        "missed_targets": missed,
        "headline": {
            "baseline_arm": baseline["arm"],
            "candidate_arm": candidate["arm"],
            "probes": blocks["tool"]["fabricated_entity_probes_returning_rows"]["n"],
            "fabricated_rows_a": blocks["tool"]["fabricated_entity_probes_returning_rows"]["a"],
            "fabricated_rows_b": blocks["tool"]["fabricated_entity_probes_returning_rows"]["b"],
            "fabricated_difference_ci": (
                blocks["tool"]["fabricated_entity_probes_returning_rows"]["difference"]["ci_low"],
                blocks["tool"]["fabricated_entity_probes_returning_rows"]["difference"]["ci_high"],
            ),
            "recall_a": blocks["tool"]["answerable_recall"]["a"],
            "recall_b": blocks["tool"]["answerable_recall"]["b"],
            "recall_difference_ci": (
                blocks["tool"]["answerable_recall"]["difference"]["ci_low"],
                blocks["tool"]["answerable_recall"]["difference"]["ci_high"],
            ),
            "expired_served_a": blocks["tool"]["expired_versions_served"]["a"],
            "expired_served_b": blocks["tool"]["expired_versions_served"]["b"],
            "expired_difference_ci": (
                blocks["tool"]["expired_versions_served"]["difference"]["ci_low"],
                blocks["tool"]["expired_versions_served"]["difference"]["ci_high"],
            ),
            "missed_targets": missed,
        },
    }
