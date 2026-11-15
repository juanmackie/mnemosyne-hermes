"""Aggregate per-probe grades into the scorecard's reported numbers.

The scorecard reports exactly the four pass criteria in the task plus the
diagnostics needed to explain them. Nothing here is a projection: every number
is a mean (or share) over the probes that were actually run.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .stats import paired_bootstrap_ci, paired_differences


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def per_probe_rows(results: list[dict[str, Any]], surface: str) -> dict[str, dict[str, Any]]:
    """Index one surface's per-probe grades by probe id."""
    return {row["probe_id"]: row for row in results if row.get("surface") == surface}


def summarize_surface(
    probes: list[dict[str, Any]], results: list[dict[str, Any]], surface: str
) -> dict[str, Any]:
    """Summarize one surface over all probes.

    `probe_row` values are the *bad* outcome counts (1 = bad) so the scorecard's
    pass criteria are all "must be zero" or "must be at most", and differences
    are readable as "arm A was worse by X".
    """
    rows = per_probe_rows(results, surface)
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for probe in probes:
        row = rows.get(probe["probe_id"])
        if row is not None:
            by_kind[probe["kind"]].append(row)

    def counts(kind: str) -> dict[str, Any]:
        return {
            "probes": len(by_kind[kind]),
            "answerable_hits": sum(1 for row in by_kind[kind] if row["answer_hit"]),
            "rows_returned": sum(row.get("returned_rows") or 0 for row in by_kind[kind]),
            "fabricated_probes_with_rows": sum(
                1 for row in by_kind[kind] if row.get("answered_fabricated")
            ),
            "fabricated_rows_total": sum(row.get("fabricated_rows", 0) for row in by_kind[kind]),
            "expired_served_total": sum(row.get("expired_served", 0) for row in by_kind[kind]),
            "expired_probes_served": sum(
                1 for row in by_kind[kind] if row.get("expired_served", 0)
            ),
            "expired_present_total": sum(1 for row in by_kind[kind] if row.get("expired_present")),
            "stale_above_current": sum(
                1 for row in by_kind[kind] if row.get("stale_ranked_above_current")
            ),
            "stale_present": sum(1 for row in by_kind[kind] if row.get("stale_present")),
            "answer_present": sum(1 for row in by_kind[kind] if row.get("answer_present")),
        }

    current = counts("answerable_current")
    historical = counts("answerable_historical")
    fabricated = counts("fabricated")
    expired = counts("expired")
    answerable_total = current["probes"] + historical["probes"]
    answerable_hits = current["answerable_hits"] + historical["answerable_hits"]

    return {
        "surface": surface,
        "probes": sum(len(group) for group in by_kind.values()),
        "by_kind": {
            "answerable_current": current,
            "answerable_historical": historical,
            "fabricated": fabricated,
            "expired": expired,
        },
        "answerable_recall": answerable_hits / answerable_total if answerable_total else None,
        "answerable_probes": answerable_total,
        "answerable_hits": answerable_hits,
        "fabricated_entity_queries_returning_rows": fabricated["fabricated_probes_with_rows"],
        "fabricated_entity_queries": fabricated["probes"],
        "expired_facts_served_as_current": expired["expired_served_total"],
        "expired_probes_served": expired["expired_probes_served"],
        "expired_probes": expired["probes"],
        "stale_outranking_current": current["stale_above_current"],
    }


def compare_surfaces(
    probes: list[dict[str, Any]],
    arm_a: list[dict[str, Any]],
    arm_b: list[dict[str, Any]],
    surface: str,
    *,
    resamples: int = 10_000,
) -> dict[str, Any]:
    """Paired comparison of two arms on one surface.

    Reported per-criterion, because the pass criteria answer different
    questions: an abstention fix should move abstention and leave recall alone.
    """
    rows_a = per_probe_rows(arm_a, surface)
    rows_b = per_probe_rows(arm_b, surface)
    blocks: dict[str, Any] = {}

    def series(kind: str, field: str) -> tuple[list[float], list[float], list[dict[str, Any]]]:
        picked = [
            probe
            for probe in probes
            if probe["kind"] == kind and probe["probe_id"] in rows_a and probe["probe_id"] in rows_b
        ]
        left = [float(rows_a[probe["probe_id"]].get(field) or 0) for probe in picked]
        right = [float(rows_b[probe["probe_id"]].get(field) or 0) for probe in picked]
        return left, right, picked

    # Abstention: fabricated probes that returned at least one row. Also
    # reported as a raw row count: the count is noisier per probe but far more
    # sensitive to a partial fix, which matters for the CI gate's power.
    left, right, picked = series("fabricated", "fabricated_rows")
    blocks["fabricated_rows_returned"] = {
        "n": len(picked),
        "a": sum(left),
        "b": sum(right),
        "difference": paired_bootstrap_ci(paired_differences(left, right), resamples=resamples),
    }
    # Abstention: fabricated probes that returned at least one row.
    left, right, picked = series("fabricated", "answered_fabricated")
    blocks["fabricated_entity_probes_returning_rows"] = {
        "n": len(picked),
        "a": sum(left),
        "b": sum(right),
        "difference": paired_bootstrap_ci(paired_differences(left, right), resamples=resamples),
    }
    # Expired: number of expired versions served (0..n_versions).
    left, right, picked = series("expired", "expired_served")
    blocks["expired_versions_served"] = {
        "n": len(picked),
        "a": sum(left),
        "b": sum(right),
        "difference": paired_bootstrap_ci(paired_differences(left, right), resamples=resamples),
    }
    # Answerable recall (both answerable kinds pooled, as measured).
    left_all, right_all = [], []
    picked_all = []
    for kind in ("answerable_current", "answerable_historical"):
        left, right, picked = series(kind, "answer_hit")
        left_all.extend(left)
        right_all.extend(right)
        picked_all.extend(picked)
    blocks["answerable_recall"] = {
        "n": len(picked_all),
        "a": _mean(left_all),
        "b": _mean(right_all),
        "a_hits": int(sum(left_all)),
        "b_hits": int(sum(right_all)),
        "difference": paired_bootstrap_ci(
            paired_differences(left_all, right_all), resamples=resamples
        ),
    }
    # Stale above current, only meaningful on current-truth probes.
    left, right, picked = series("answerable_current", "stale_ranked_above_current")
    blocks["stale_ranked_above_current"] = {
        "n": len(picked),
        "a": sum(left),
        "b": sum(right),
        "difference": paired_bootstrap_ci(paired_differences(left, right), resamples=resamples),
    }
    blocks["surface"] = surface
    return blocks
