"""Paired comparison statistics for the memory scorecard.

The unit of analysis is one probe, measured under two arms with the same query
and the same store. That pairing is what lets a 12,559-probe corpus resolve
small effects: the between-probe variance (which dominates an unpaired design)
cancels.

Everything here is stdlib and seeded, so a report is reproducible from its JSON.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Sequence
from typing import Any


def _seed_for(values: Sequence[float]) -> int:
    """Deterministic bootstrap seed derived from the data itself."""
    digest = hashlib.sha256(",".join(f"{value:.12g}" for value in values).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def paired_bootstrap_ci(
    differences: Sequence[float],
    *,
    resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int | None = None,
) -> dict[str, Any]:
    """Bootstrap CI for the mean of paired differences.

    Resampling is over probes (the paired unit). Returns the observed mean, the
    percentile CI, and the share of resamples on the other side of zero — the
    latter is a descriptive companion to the CI, not a p-value.
    """
    values = [float(value) for value in differences]
    if not values:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None, "resamples": resamples}
    random_source = random.Random(_seed_for(values) if seed is None else seed)
    n = len(values)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += values[random_source.randrange(n)]
        means.append(total / n)
    means.sort()

    def percentile(p: float) -> float:
        if len(means) == 1:
            return means[0]
        position = (len(means) - 1) * p
        low = int(position)
        high = min(low + 1, len(means) - 1)
        return means[low] + (position - low) * (means[high] - means[low])

    observed = sum(values) / n
    low = percentile(alpha / 2)
    high = percentile(1 - alpha / 2)
    same_side = (
        sum(1 for mean in means if mean <= 0.0)
        if observed > 0
        else sum(1 for mean in means if mean >= 0.0)
    )
    return {
        "n": n,
        "mean": observed,
        "ci_low": low,
        "ci_high": high,
        "excludes_zero": (low > 0.0) or (high < 0.0),
        "share_other_side_of_zero": same_side / len(means),
        "resamples": resamples,
        "seed": _seed_for(values) if seed is None else seed,
    }


def paired_differences(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """Element-wise a - b, refusing length mismatch (a pairing bug, not data)."""
    if len(a) != len(b):
        raise ValueError(f"paired series differ in length: {len(a)} vs {len(b)}")
    return [float(x) - float(y) for x, y in zip(a, b, strict=True)]


def holm(pairs: Sequence[tuple[str, float]]) -> list[dict[str, Any]]:
    """Holm-Bonferroni adjustment over a family of unadjusted p-values.

    The scorecard uses it to keep "we tried several flags" from inflating
    significance. Ties are handled by stable ordering.
    """
    ordered = sorted(enumerate(pairs), key=lambda item: (item[1][1], item[0]))
    count = len(pairs)
    adjusted: dict[int, float] = {}
    running = 0.0
    for step, (index, (_name, p_value)) in enumerate(ordered):
        factor = count - step
        value = min(1.0, max(running, p_value * factor))
        running = value
        adjusted[index] = value
    return [
        {"name": name, "p": p_value, "p_holm": adjusted[index]}
        for index, (name, p_value) in enumerate(pairs)
    ]


def exact_share_equal(a: Iterable[float], b: Iterable[float]) -> float | None:
    """Share of paired units that are exactly equal (the A/A expectation)."""
    left = list(a)
    right = list(b)
    if not left or len(left) != len(right):
        return None
    return sum(1 for x, y in zip(left, right, strict=True) if x == y) / len(left)
