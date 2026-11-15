"""Backtest the scorecard over historical engine-patch states.

A scorecard that has only ever been run against today's patch set is not
evidence that it tracks quality over time. This script rebuilds the engine as it
was at each historical revision of `integrations/engine-patches/`, replays a
fixed probe subsample through the real provider path, and prints the same four
numbers per state.

Why a subsample: the backtest is about *relative* behaviour across patch states,
so it uses a fixed, contiguous slice of the held-out probes (same probes for
every state). The full 12,559-probe sweep is the decision-grade run; this is the
trend check that a future patch regression cannot hide behind "we changed the
thresholds".

The pristine engine comes from the `.mnemosyne-hermes-original` backup that
`scripts/apply_engine_patches.py` leaves beside each patched file, so no state is
ever reconstructed from a newer patched file.

Usage (repository root, pinned engine installed):

    python -m bench.scorecard.backtest --probes 400
    python -m bench.scorecard.backtest --probes 400 --revs ec8e8da 661f217
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PATCH_DIR = ROOT / "integrations" / "engine-patches"
BACKUP_SUFFIX = ".mnemosyne-hermes-original"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8")


def patch_revisions(limit: int | None = None) -> list[dict[str, str]]:
    """Commits that changed the engine patch set, newest last."""
    output = _git("log", "--format=%H%x09%cs%x09%s", "--", str(PATCH_DIR))
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        commit, date, subject = line.split("\t", 2)
        rows.append({"commit": commit, "date": date, "subject": subject})
    rows.reverse()
    return rows[-limit:] if limit else rows


def build_state(commit: str, destination: Path) -> dict[str, Any]:
    """Materialise the engine as `commit`'s patch set describes it."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from apply_engine_patches import apply_diff, digest

    try:
        import mnemosyne

        package = Path(mnemosyne.__file__).resolve().parent
    except ImportError as error:  # pragma: no cover - gate handles this
        raise SystemExit(f"the pinned engine is not importable: {error}") from error

    sandbox = destination / "mnemosyne"
    shutil.copytree(
        package, sandbox, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*" + BACKUP_SUFFIX)
    )

    manifest_raw = _git("show", f"{commit}:integrations/engine-patches/manifest.json")
    manifest = json.loads(manifest_raw)
    applied: dict[str, str] = {}
    for relative, entry in manifest["files"].items():
        target = sandbox / relative
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if not backup.is_file():
            # Reconstruct the audited original from the installed tree's backup.
            installed = package / relative
            installed_backup = installed.with_name(installed.name + BACKUP_SUFFIX)
            if not installed_backup.is_file():
                raise SystemExit(
                    f"no verified original for {relative}; run "
                    "`python scripts/apply_engine_patches.py` first"
                )
            original = installed_backup.read_bytes()
        else:
            original = backup.read_bytes()
        if digest(original) != entry["original_sha256"]:
            raise SystemExit(f"audited original digest mismatch for {relative}")
        delta = _git("show", f"{commit}:integrations/engine-patches/{entry['patch']}").encode()
        target.write_bytes(apply_diff(original, delta))
        applied[relative] = digest(target.read_bytes())
    return {"package_dir": str(destination), "files": applied, "manifest": manifest}


def floor_of(state: dict[str, Any]) -> str:
    """Read back the admission floor that this state actually implements."""
    source = (Path(state["package_dir"]) / "mnemosyne" / "core" / "beam.py").read_text(
        encoding="utf-8"
    )
    start = source.find("def _minimum_recall_relevance(")
    if start == -1:
        return "missing"
    body = source[start : start + 2000]
    returns = [line.strip() for line in body.splitlines() if line.strip().startswith("return 0")]
    stepped = "if len(query_tokens) >= 4" in body
    return f"{'stepped ' if stepped else ''}{returns[0] if returns else '?'}"


def run_state(
    state: dict[str, Any],
    *,
    arm: str,
    probes: int,
    workers: int,
    seed: int,
) -> dict[str, Any]:
    """One state's scorecard numbers, measured in a child process."""
    payload = {
        "arm": arm,
        "package_dir": state["package_dir"],
        "probes": probes,
        "workers": workers,
        "seed": seed,
    }
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bench.scorecard import parallel
from bench.scorecard.aggregate import summarize_surface
from bench.scorecard.runner import activate_engine_path
from bench.sim.scale import make_corpus

payload = json.loads(sys.argv[2])
corpus = make_corpus(payload["seed"], probes=payload["probes"], dev_probes=0,
                     probes_per_life=20, slots=5)
probes = [p for p in corpus["probes"] if p["split"] == "held_out"]
by_life = {}
for probe in probes:
    by_life.setdefault(probe["life_id"], []).append(probe)
lives = [life for life in corpus["lives"] if life["life_id"] in by_life]
activate_engine_path(payload["package_dir"])
results = parallel.run_lives(lives, by_life, arm=payload["arm"],
                             dump_candidates=False,
                             engine_sys_path=payload["package_dir"],
                             workers=payload["workers"])
merged = parallel.merge(results, arm=payload["arm"], wall_seconds=0.0)
summary = summarize_surface(probes, merged["records"], "tool")
by_id = {row["probe_id"]: row for row in merged["records"]}
vectors = {
    "fabricated": {p["probe_id"]: float(by_id[p["probe_id"]].get("answered_fabricated") or 0)
                   for p in probes if p["kind"] == "fabricated" and p["probe_id"] in by_id},
    "expired": {p["probe_id"]: float(by_id[p["probe_id"]].get("expired_served") or 0)
                for p in probes if p["kind"] == "expired" and p["probe_id"] in by_id},
    "recall": {p["probe_id"]: float(by_id[p["probe_id"]].get("answer_hit") or 0)
               for p in probes if p["kind"].startswith("answerable") and p["probe_id"] in by_id},
}
print(json.dumps({"errors": len(merged["errors"]), "summary": summary, "vectors": vectors}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), json.dumps(payload)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
        check=False,
    )
    if completed.returncode != 0:
        return {"arm": arm, "error": completed.stderr.strip().splitlines()[-1:]}
    return {"arm": arm, **json.loads(completed.stdout.strip().splitlines()[-1])}


def _paired_vs_reference(
    rows: list[dict[str, Any]],
    vectors: dict[str, dict[str, dict[str, float]]],
    *,
    resamples: int,
) -> list[dict[str, Any]]:
    """Paired bootstrap of every state against the newest measured state.

    A point-estimate table cannot say whether a historical difference is real:
    the per-state samples are the same probes, so the honest comparison is paired
    per probe. The newest state is the reference because it is the one the current
    tree claims to be.
    """
    from bench.scorecard.stats import paired_bootstrap_ci, paired_differences

    measured = [row["commit"][:8] for row in rows if row["commit"][:8] in vectors]
    if len(measured) < 2:
        return []
    reference = measured[-1]
    labels = (
        ("fabricated", "fabricated queries returning rows"),
        ("expired", "expired versions served"),
        ("recall", "answerable recall"),
    )
    out = []
    for state in measured[:-1]:
        for key, label in labels:
            left = vectors[state].get(key) or {}
            right = vectors[reference].get(key) or {}
            shared = [pid for pid in left if pid in right]
            if not shared:
                continue
            diff = paired_differences([left[p] for p in shared], [right[p] for p in shared])
            ci = paired_bootstrap_ci(diff, resamples=resamples)
            out.append(
                {
                    "commit": state,
                    "reference": reference,
                    "metric": label,
                    "n": ci["n"],
                    "mean": ci["mean"],
                    "ci_low": ci["ci_low"],
                    "ci_high": ci["ci_high"],
                    "excludes_zero": ci["excludes_zero"],
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probes", type=int, default=400, help="held-out probes per state")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument("--revs", nargs="*", help="explicit commits instead of all history")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--resamples", type=int, default=4000)
    args = parser.parse_args(argv)

    if args.revs:
        revisions = [
            {"commit": rev, "date": "", "subject": _git("log", "-1", "--format=%s", rev)}
            for rev in args.revs
        ]
    else:
        revisions = patch_revisions(limit=args.limit)

    rows = []
    vectors: dict[str, dict[str, dict[str, float]]] = {}
    with tempfile.TemporaryDirectory(prefix="scorecard-backtest-") as folder:
        for index, revision in enumerate(revisions):
            destination = Path(folder) / f"state-{index}"
            destination.mkdir()
            try:
                state = build_state(revision["commit"], destination)
            except SystemExit as error:
                rows.append({**revision, "error": str(error)})
                continue
            floor = floor_of(state)
            print(
                f"[{index + 1}/{len(revisions)}] {revision['commit'][:8]} "
                f"{revision['subject'][:52]} (floor {floor})",
                file=sys.stderr,
                flush=True,
            )
            result = run_state(
                state,
                arm=revision["commit"][:8],
                probes=args.probes,
                workers=args.workers,
                seed=args.seed,
            )
            summary = result.get("summary") or {}
            vectors[revision["commit"][:8]] = result.get("vectors") or {}
            rows.append(
                {
                    **revision,
                    "patch_floor": floor,
                    "errors": result.get("errors"),
                    "answerable_recall": summary.get("answerable_recall"),
                    "fabricated_queries_returning_rows": summary.get(
                        "fabricated_entity_queries_returning_rows"
                    ),
                    "fabricated_queries": summary.get("fabricated_entity_queries"),
                    "expired_served": summary.get("expired_facts_served_as_current"),
                    "expired_probes": summary.get("expired_probes"),
                    "stale_outranking_current": summary.get("stale_outranking_current"),
                }
            )

    header = (
        f"{'commit':10} {'date':10} {'floor':10} {'recall':>7} "
        f"{'fabricated':>12} {'expired':>9} stale"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        if "error" in row:
            print(f"{row['commit'][:8]:10} {row.get('date', ''):10} ERROR {row['error']}")
            continue
        fabricated = f"{row['fabricated_queries_returning_rows']}/{row['fabricated_queries']}"
        expired = f"{row['expired_served']}/{row['expired_probes']}"
        print(
            f"{row['commit'][:8]:10} {row['date']:10} {row['patch_floor']:10} "
            f"{row['answerable_recall']:.3f} {fabricated:>12} {expired:>9} "
            f"{row['stale_outranking_current']}"
        )

    comparisons = _paired_vs_reference(rows, vectors, resamples=args.resamples)
    if comparisons:
        print()
        print("paired difference vs the newest measured state (bootstrap CI over probes)")
        print(f"{'commit':10} {'metric':34} {'mean':>8} 95% CI")
        print("-" * 78)
        for row in comparisons:
            print(
                f"{row['commit']:10} {row['metric']:34} {row['mean']:+8.4f} "
                f"[{row['ci_low']:+.4f}, {row['ci_high']:+.4f}]"
            )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "probes_per_state": args.probes,
                    "seed": args.seed,
                    "states": rows,
                    "paired_vs_newest_state": comparisons,
                },
                sort_keys=True,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
