"""Conservative planning calculations for paired bounded benchmark metrics."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _normal_quantile(p: float) -> float:
    # Acklam's inverse-normal approximation, sufficient for planning values.
    if not 0 < p < 1:
        raise ValueError("probability must be between 0 and 1")
    a = (
        -39.69683028665376,
        220.9460984245205,
        -275.9285104469687,
        138.3577518672690,
        -30.66479806614716,
        2.506628277459239,
    )
    b = (
        -54.47609879822406,
        161.5858368580409,
        -155.6989798598866,
        66.80131188771972,
        -13.28068155288572,
    )
    c = (
        -0.007784894002430293,
        -0.3223964580411365,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    low, high = 0.02425, 0.97575
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > high:
        return -_normal_quantile(1 - p)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def paired_mde(
    n: int, *, sd_difference: float = 1.0, alpha: float = 0.05, power: float = 0.8
) -> float:
    """Normal-approximation MDE for paired differences, with explicit SD."""
    if n < 2 or sd_difference < 0 or not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("n >= 2, SD >= 0, and alpha/power in (0, 1) are required")
    return (
        (_normal_quantile(1 - alpha / 2) + _normal_quantile(power)) * sd_difference / math.sqrt(n)
    )


def sample_size(
    effect: float, *, sd_difference: float = 1.0, alpha: float = 0.05, power: float = 0.8
) -> int:
    if not 0 < effect <= 1:
        raise ValueError("effect must be in (0, 1]")
    if sd_difference < 0 or not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("SD >= 0, and alpha/power in (0, 1) are required")
    if sd_difference == 0:
        return 2
    zsum = _normal_quantile(1 - alpha / 2) + _normal_quantile(power)
    return max(2, math.ceil((zsum * sd_difference / effect) ** 2))


def planning_record(n: int = 27) -> dict:
    return {
        "format": "mnemosyne-mnemobench-power/v1",
        "method": "paired normal-approximation screening; confirm with paired bootstrap",
        "assumptions": {
            "metric_range": [-1, 1],
            "paired_difference_sd_upper_bound": 1.0,
            "alpha": 0.05,
            "power": 0.8,
            "multiplicity": "Holm correction required across each registered sweep; sample sizes shown use unadjusted alpha and are lower bounds when corrected",
        },
        "current_fixture_queries": n,
        "current_fixture_mde": round(paired_mde(n), 6),
        "minimum_effect_sample_sizes": {
            str(effect): sample_size(effect) for effect in (0.10, 0.05, 0.025)
        },
    }


def render_readme(record: dict) -> str:
    rows = [
        "# MnemoBench power plan",
        "",
        "Generated from `power-record.json` by `python bench/mnemobench/power.py`.",
        "",
        "The planning approximation assumes paired differences bounded by [-1, 1], an unknown SD conservatively set to 1, two-sided alpha=0.05, and 80% power. These are screening lower bounds; use the paired bootstrap and Holm-corrected registered sweeps for decisions.",
        "",
        f"Current retrieval fixture: {record['current_fixture_queries']} queries; conservative MDE = {record['current_fixture_mde']:.3f}.",
        "",
        "| Minimum paired effect | Minimum queries (before Holm adjustment) |",
        "|---:|---:|",
    ]
    rows.extend(
        f"| {float(effect):.1%} | {count} |"
        for effect, count in record["minimum_effect_sample_sizes"].items()
    )
    rows += ["", "The 27-query replay is a smoke fixture, not a decision-grade evaluation.", ""]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=27)
    parser.add_argument("--output", type=Path, default=ROOT / "bench/mnemobench/power-record.json")
    args = parser.parse_args()
    record = planning_record(args.queries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output.parent / "README.md").write_text(render_readme(record), encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
