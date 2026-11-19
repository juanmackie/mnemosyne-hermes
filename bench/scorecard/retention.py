"""Staged retention scorecard helpers.

The cohort of a current-answer probe is the stage in which its labelled value
was first stored. Historical probes are excluded because prefetch has no
historical query-time parameter. A timestamp group is never split across stages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def assert_scratch_database(db_path: Path, scratch: Path) -> None:
    if not Path(db_path).resolve().is_relative_to(scratch.resolve()):
        raise ValueError(f"database outside scratch directory: {db_path}")


def stage_events(events: list[dict[str, Any]], count: int = 4) -> list[list[dict[str, Any]]]:
    if count < 1:
        raise ValueError("stage count must be positive")
    times = sorted({event["transaction_at"] for event in events})
    stages: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    for event in sorted(events, key=lambda row: (row["transaction_at"], row["event_id"])):
        stage = times.index(event["transaction_at"]) * count // len(times)
        stages[stage].append(event)
    return stages


def assign_cohorts(
    stages: list[list[dict[str, Any]]], probes: list[dict[str, Any]]
) -> dict[str, int]:
    cohorts: dict[str, int] = {}
    for probe in probes:
        if probe["kind"] != "answerable_current":
            continue
        for index, stage in enumerate(stages):
            if any(
                event["disposition"] == "store"
                and event.get("entity") == probe["entity"]
                and event.get("attribute") == probe["attribute"]
                and event.get("value") in probe["answers"]
                for event in stage
            ):
                cohorts[probe["probe_id"]] = index
                break
    return cohorts


def _cluster_ci(values: dict[str, list[float]], resamples: int) -> dict[str, Any]:
    """Bootstrap independent lives, retaining all paired observations per life."""
    import random

    lives = sorted(values)
    if not lives:
        return {"lives": 0, "n": 0, "mean": None, "ci_low": None, "ci_high": None}
    if resamples < 1:
        raise ValueError("resamples must be positive")
    rng = random.Random(719)
    all_values = [x for life in lives for x in values[life]]
    means = []
    for _ in range(resamples):
        sample = [rng.choice(lives) for _ in lives]
        observations = [x for life in sample for x in values[life]]
        means.append(sum(observations) / len(observations))
    means.sort()
    return {
        "lives": len(lives),
        "n": len(all_values),
        "mean": sum(all_values) / len(all_values),
        "ci_low": means[int(0.025 * (resamples - 1))],
        "ci_high": means[int(0.975 * (resamples - 1))],
        "resamples": resamples,
    }


def summarize(records: list[dict[str, Any]], *, resamples: int = 2000) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for surface in ("tool", "prefetch"):
        rows = [row for row in records if row["surface"] == surface]
        if not rows:
            continue
        index: dict[tuple[str, str, int], dict[str, Any]] = {}
        for row in rows:
            key = (row["life_id"], row["probe_id"], row["stage"])
            if key in index:
                raise ValueError(f"duplicate observation: {key}")
            index[key] = row
        matrix = []
        for stage in range(4):
            cells = []
            for cohort in range(4):
                hits = [
                    int(row["answer_hit"])
                    for row in rows
                    if row["stage"] == stage and row["cohort"] == cohort
                ]
                cells.append({"n": len(hits), "recall": sum(hits) / len(hits)} if hits else None)
            matrix.append(cells)
        differences: dict[str, list[float]] = {}
        for row in rows:
            cohort = row["cohort"]
            if cohort is None or cohort >= 3 or row["stage"] != cohort:
                continue
            key = (row["life_id"], row["probe_id"], 3)
            if key not in index:
                raise ValueError(f"missing final observation: {key}")
            differences.setdefault(row["life_id"], []).append(
                int(index[key]["answer_hit"]) - int(row["answer_hit"])
            )
        drops = []
        for stage in range(4):
            line = []
            for cohort in range(4):
                initial, current = matrix[cohort][cohort], matrix[stage][cohort]
                line.append(
                    initial["recall"] - current["recall"]
                    if initial is not None and current is not None
                    else None
                )
            drops.append(line)
        report[surface] = {
            "retention": matrix,
            "backward_transfer": _cluster_ci(differences, resamples),
            "fabricated_rows": sum(r["fabricated_rows"] for r in rows),
            "expired_served": sum(r["expired_served"] for r in rows),
            "fabricated_probes": sum(r.get("kind") == "fabricated" for r in rows),
            "expired_probes": sum(r.get("kind") == "expired" for r in rows),
            "invariants_pass": any(r.get("kind") == "fabricated" for r in rows)
            and any(r.get("kind") == "expired" for r in rows)
            and all(r["fabricated_rows"] == 0 and r["expired_served"] == 0 for r in rows),
            "stage_drop": drops,
        }
    return report


def measure_life(
    life: dict[str, Any],
    probes: list[dict[str, Any]],
    arm: str,
    *,
    verify_sleep: bool = False,
    demote_sources: bool = False,
) -> dict[str, Any]:
    """Run one arm with the real provider on a new temporary database."""
    import importlib.util
    import json
    import os
    import tempfile
    from pathlib import Path

    from .grade import grade, grade_prefetch
    from .runner import LifeStore, clean_environment, engine_attestation

    if arm not in (
        "no-sleep",
        "sleep",
        "sleep+degrade",
        "sleep+degrade-verified",
        "sleep+degrade-additive",
        "delete-originals",
        "sleep-delete-originals",
        "sleep-loss-control",
    ):
        raise ValueError(f"unknown arm {arm}")
    stages = stage_events(life["events"])
    cohorts = assign_cohorts(stages, probes)
    target = min((cohort for cohort in cohorts.values() if cohort < 3), default=None)
    records: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    # See parallel._measure_life: leftover handles must not fail the run.
    with tempfile.TemporaryDirectory(
        prefix="mnemosyne-retention-", ignore_cleanup_errors=True
    ) as folder:
        scratch = Path(folder)
        previous = dict(os.environ)
        # Build the environment before clearing: clean_environment reads
        # os.environ, so calling it after the clear keeps nothing but its own
        # defaults and drops HOME/USERPROFILE/PATH for the engine import.
        resolved = clean_environment(
            scratch,
            {
                "MNEMOSYNE_LLM_ENABLED": "false",
                "MNEMOSYNE_HOST_LLM_ENABLED": "false",
                **({"MNEMOSYNE_SLEEP_VERIFY": "1"} if verify_sleep else {}),
                **({"MNEMOSYNE_SLEEP_DEMOTE_SOURCES": "1"} if demote_sources else {}),
                **({"MNEMOSYNE_DEGRADE_VERIFY": "1"} if arm == "sleep+degrade-verified" else {}),
                **({"MNEMOSYNE_DEGRADE_ADDITIVE": "1"} if arm == "sleep+degrade-additive" else {}),
            },
        )
        os.environ.clear()
        os.environ.update(resolved)
        store = None
        try:
            attestation = engine_attestation()
            spec = importlib.util.find_spec("mnemosyne")
            expected = Path(attestation["package_root"]) / "__init__.py"
            if (
                spec is None
                or spec.origin is None
                or Path(spec.origin).resolve() != expected.resolve()
            ):
                raise RuntimeError("imported engine path differs from attested distribution")
            store = LifeStore.create(scratch, life["life_id"])
            beam = store.provider._beam
            assert_scratch_database(Path(beam.db_path), scratch)
            for stage, events in enumerate(stages):
                for event in events:
                    if event["disposition"] == "store":
                        payload = {
                            "content": event["sentence"],
                            "importance": 0.6,
                            "source": "user",
                            "scope": "session",
                        }
                        if event.get("valid_to_stated_in_text"):
                            payload["valid_until"] = event["valid_to"]
                        result = json.loads(
                            store.provider.handle_tool_call("mnemosyne_remember", payload)
                        )
                        if not isinstance(result, dict) or result.get("error"):
                            raise RuntimeError(f"remember failed: {result}")

                if arm in (
                    "sleep",
                    "sleep+degrade",
                    "sleep+degrade-verified",
                    "sleep+degrade-additive",
                    "sleep-delete-originals",
                    "sleep-loss-control",
                ):
                    degrade = beam.degrade_episodic
                    beam.degrade_episodic = lambda **kwargs: {"status": "suppressed"}
                    try:
                        sleep_result = beam.sleep(force=True)
                    finally:
                        beam.degrade_episodic = degrade
                    if sleep_result.get("status") not in ("consolidated", "no_op"):
                        raise RuntimeError(f"sleep failed: {sleep_result}")
                    operation: dict[str, Any] = {"stage": stage, "sleep": sleep_result}
                    if arm == "sleep+degrade-additive":
                        # Additive copies leave originals at their tier, so the in-place
                        # transition check does not apply. Record eligibility and the row count.
                        beam.conn.execute(
                            "UPDATE episodic_memory SET created_at = ?", ("2000-01-01T00:00:00",)
                        )
                        beam.conn.commit()
                        transitions = []
                        for _tier in (1, 2):
                            eligible = beam.conn.execute(
                                "SELECT COUNT(*) FROM episodic_memory WHERE tier = ? AND degraded_at IS NULL",
                                (_tier,),
                            ).fetchone()[0]
                            transitions.append({"eligible": eligible, "result": degrade()})
                        operation["degradation"] = transitions
                        operation["episodic_rows"] = beam.conn.execute(
                            "SELECT COUNT(*) FROM episodic_memory"
                        ).fetchone()[0]
                    elif arm in ("sleep+degrade", "sleep+degrade-verified"):
                        guarded = arm == "sleep+degrade-verified"
                        beam.conn.execute(
                            "UPDATE episodic_memory SET created_at = ?", ("2000-01-01T00:00:00",)
                        )
                        beam.conn.commit()
                        transitions = []
                        for tier in (1, 2):
                            before = {
                                row[0]
                                for row in beam.conn.execute(
                                    "SELECT id FROM episodic_memory WHERE tier = ?", (tier,)
                                )
                            }
                            result = degrade()
                            after = {
                                row[0]: row[1]
                                for row in beam.conn.execute("SELECT id, tier FROM episodic_memory")
                            }
                            failed = sorted(mid for mid in before if after.get(mid) != tier + 1)
                            transitions.append(
                                {"eligible": len(before), "failed_ids": failed, "result": result}
                            )
                            if guarded:
                                # The guard may hold a row at its tier; any other tier is a bug.
                                moved_wrongly = sorted(
                                    mid for mid in before if after.get(mid) not in (tier, tier + 1)
                                )
                                if moved_wrongly:
                                    raise RuntimeError(
                                        f"guarded tier {tier} degradation moved rows wrongly: {moved_wrongly}"
                                    )
                            elif failed:
                                raise RuntimeError(
                                    f"incomplete tier {tier} degradation: {transitions[-1]}"
                                )
                        operation["degradation"] = transitions
                    operations.append(operation)
                for probe in probes:
                    cohort = cohorts.get(probe["probe_id"])
                    control = probe["kind"] in ("fabricated", "expired") and stage == 3
                    if not control and (cohort is None or cohort > stage):
                        continue
                    payload = store.recall(probe["query"], limit=10, query_time=probe["as_of"])
                    if payload.get("error") or not isinstance(payload.get("results"), list):
                        raise RuntimeError(f"invalid recall response: {payload}")
                    texts = [str(row.get("content") or "") for row in payload["results"]]
                    # Match answers only in rows about the requested entity;
                    # values recur across unrelated entities in the corpus.
                    relevant = [
                        text if probe.get("entity", "").lower() in text.lower() else ""
                        for text in texts
                    ]
                    tool = grade(probe, texts)
                    if cohort is not None:
                        entity_grade = grade(probe, relevant)
                        tool["answer_hit"] = entity_grade["answer_hit"]
                        tool["answer_rank"] = entity_grade["answer_rank"]
                        tool["stale_ranked_above_current"] = entity_grade[
                            "stale_ranked_above_current"
                        ]
                    context = store.prefetch(probe["query"])
                    prefetched = grade_prefetch(probe, context)
                    if cohort is not None and probe["entity"].lower() not in context.lower():
                        prefetched["answer_hit"] = False
                    for surface, result in (("tool", tool), ("prefetch", prefetched)):
                        records.append(
                            {
                                **result,
                                "surface": surface,
                                "life_id": life["life_id"],
                                "stage": stage,
                                "cohort": cohort,
                                "control_target": target is not None and cohort == target,
                            }
                        )
                if (
                    arm in ("delete-originals", "sleep-delete-originals", "sleep-loss-control")
                    and stage == target
                ):
                    query = (
                        "SELECT id FROM working_memory WHERE consolidated_at IS NOT NULL"
                        if arm in ("sleep-delete-originals", "sleep-loss-control")
                        else "SELECT id FROM working_memory"
                    )
                    ids = [row[0] for row in beam.conn.execute(query).fetchall()]
                    deleted = sum(bool(beam.forget_working(mid)) for mid in ids)
                    if deleted != len(ids):
                        raise RuntimeError("failed to delete control originals")
                    operation = {"stage": stage, "deleted_originals": deleted}
                    if arm == "sleep-loss-control":
                        store.provider._read_config_key = lambda key: (
                            ["*"] if key == "tools" else None
                        )
                        episode_ids = [
                            row[0] for row in beam.conn.execute("SELECT id FROM episodic_memory")
                        ]
                        for episode_id in episode_ids:
                            result = json.loads(
                                store.provider.handle_tool_call(
                                    "mnemosyne_update",
                                    {
                                        "memory_id": episode_id,
                                        "content": "Synthetic control: summary content removed.",
                                    },
                                )
                            )
                            if result.get("status") != "updated":
                                raise RuntimeError(f"could not erase synthetic summary: {result}")
                        operation["erased_summaries"] = len(episode_ids)
                    operations.append(operation)
        finally:
            if store is not None:
                store.close()
            os.environ.clear()
            os.environ.update(previous)
    return {"records": records, "operations": operations, "cohorts": len(cohorts)}


def compare_control(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    surface: str,
    *,
    resamples: int = 2000,
) -> dict[str, Any]:
    """Compare final recall for the cohort targeted by the deletion control."""

    def select(rows: list[dict[str, Any]]) -> dict[tuple[str, str], bool]:
        selected = [
            r for r in rows if r["surface"] == surface and r["stage"] == 3 and r["control_target"]
        ]
        indexed = {(r["life_id"], r["probe_id"]): bool(r["answer_hit"]) for r in selected}
        if len(indexed) != len(selected):
            raise ValueError("duplicate control observations")
        return indexed

    left, right = select(baseline), select(candidate)
    if left.keys() != right.keys():
        raise ValueError("control arms have different observations")
    clusters: dict[str, list[float]] = {}
    for (life_id, probe_id), value in left.items():
        clusters.setdefault(life_id, []).append(int(right[(life_id, probe_id)]) - int(value))
    return _cluster_ci(clusters, resamples)


def summary_loss_control(
    arms: dict[str, list[dict[str, Any]]], surface: str, *, resamples: int = 2000
) -> dict[str, Any]:
    return compare_control(
        arms["sleep-delete-originals"], arms["sleep-loss-control"], surface, resamples=resamples
    )


def result_exit_code(controls_ok: bool, quality_ok: bool) -> int:
    return 2 if not controls_ok else (0 if quality_ok else 1)


def aa_control(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], surface: str
) -> dict[str, Any]:
    def select(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int], tuple[Any, ...]]:
        result = {}
        for row in rows:
            if row["surface"] == surface:
                key = (row["life_id"], row["probe_id"], row["stage"])
                if key in result:
                    raise ValueError(f"duplicate observation: {key}")
                result[key] = (row["answer_hit"], row["fabricated_rows"], row["expired_served"])
        return result

    left, right = select(baseline), select(candidate)
    if left.keys() != right.keys():
        raise ValueError("A/A arms have different observations")
    changed = sum(left[key] != right[key] for key in left)
    tolerance = max(1, len(left) // 100)
    return {
        "n": len(left),
        "different": changed,
        "tolerance": tolerance,
        "passed": bool(left) and changed <= tolerance,
    }


def compare_bwt(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    surface: str,
    *,
    resamples: int = 2000,
) -> dict[str, Any]:
    def changes(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
        indexed = {
            (r["life_id"], r["probe_id"], r["stage"]): r for r in rows if r["surface"] == surface
        }
        result = {}
        for (life, probe, stage), row in indexed.items():
            cohort = row["cohort"]
            if cohort is not None and cohort < 3 and stage == cohort:
                final = indexed.get((life, probe, 3))
                if final is None:
                    raise ValueError("missing final observation")
                result[(life, probe)] = int(final["answer_hit"]) - int(row["answer_hit"])
        return result

    left, right = changes(baseline), changes(candidate)
    if left.keys() != right.keys():
        raise ValueError("BWT arms have different observations")
    clusters: dict[str, list[float]] = {}
    for key, value in left.items():
        clusters.setdefault(key[0], []).append(right[key] - value)
    return _cluster_ci(clusters, resamples)


def _job(args: tuple[Any, ...]) -> dict[str, Any]:
    life, probes, arm, *options = args
    return measure_life(
        life,
        probes,
        arm,
        verify_sleep=options[0] if len(options) > 0 else False,
        demote_sources=options[1] if len(options) > 1 else False,
    )


def main() -> int:
    import argparse
    import importlib
    import json
    import multiprocessing
    import sys
    from pathlib import Path

    from .corpus import load_or_build
    from .runner import (
        PROVIDER_ROOT,
        engine_attestation,
        provider_tree_hashes,
        require_vendored_provider,
    )

    parser = argparse.ArgumentParser(
        description="Measure staged retention in synthetic scratch stores"
    )
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--max-lives", type=int, default=None)
    parser.add_argument(
        "--sleep-verify", action="store_true", help="enable experimental sleep coverage guard"
    )
    parser.add_argument(
        "--sleep-demote",
        action="store_true",
        help="enable experimental source demotion after verified consolidation",
    )
    args = parser.parse_args()
    if (
        args.workers < 1
        or args.resamples < 1
        or (args.max_lives is not None and args.max_lives < 1)
    ):
        parser.error("workers, resamples and max-lives must be positive")
    attestation = engine_attestation()
    if attestation.get("version") != attestation.get("manifest_match_expected", {}).get(
        "version"
    ) or not attestation.get("all_patched"):
        raise RuntimeError(f"expected the audited patched engine: {attestation}")
    sys.path.insert(0, str(PROVIDER_ROOT))
    corpus = load_or_build(args.corpus)
    by_life: dict[str, list[dict[str, Any]]] = {}
    for probe in corpus["probes"]:
        if probe["split"] == "held_out":
            by_life.setdefault(probe["life_id"], []).append(probe)
    lives = [life for life in corpus["lives"] if life["life_id"] in by_life][: args.max_lives]
    if not lives:
        raise ValueError("no held-out lives")
    results: dict[str, Any] = {}
    for label, arm in (
        ("no-sleep", "no-sleep"),
        ("aa", "no-sleep"),
        ("sleep", "sleep"),
        ("sleep+degrade", "sleep+degrade"),
        ("sleep+degrade-verified", "sleep+degrade-verified"),
        ("sleep+degrade-additive", "sleep+degrade-additive"),
        ("delete-originals", "delete-originals"),
        ("sleep-delete-originals", "sleep-delete-originals"),
        ("sleep-loss-control", "sleep-loss-control"),
    ):
        jobs = [
            (life, by_life[life["life_id"]], arm, args.sleep_verify, args.sleep_demote)
            for life in lives
        ]
        print(f"Measuring {label}: {len(jobs)} lives", file=sys.stderr, flush=True)
        if args.workers == 1:
            measured = list(map(_job, jobs))
        else:
            with multiprocessing.get_context("spawn").Pool(args.workers) as pool:
                measured = pool.map(_job, jobs, chunksize=1)
        records = [record for item in measured for record in item["records"]]
        results[label] = {
            "records": records,
            "summary": summarize(records, resamples=args.resamples),
            "operations": [item["operations"] for item in measured],
        }
    gates: dict[str, Any] = {}
    for surface in ("tool", "prefetch"):
        aa = aa_control(results["no-sleep"]["records"], results["aa"]["records"], surface)
        deletion = compare_control(
            results["no-sleep"]["records"],
            results["delete-originals"]["records"],
            surface,
            resamples=args.resamples,
        )
        sleep_deletion = compare_control(
            results["sleep"]["records"],
            results["sleep-delete-originals"]["records"],
            surface,
            resamples=args.resamples,
        )
        sleep_loss = summary_loss_control(
            {name: result["records"] for name, result in results.items()},
            surface,
            resamples=args.resamples,
        )
        gates[surface] = {
            "sleep_loss": sleep_loss,
            "sleep_loss_detected": sleep_loss["n"] > 0 and sleep_loss["ci_high"] < 0,
            "sleep_deletion": sleep_deletion,
            "sleep_deletion_detected": sleep_deletion["n"] > 0 and sleep_deletion["ci_high"] < 0,
            "aa": aa,
            "deletion": deletion,
            "aa_pass": aa["passed"],
            "deletion_detected": deletion["n"] > 0 and deletion["ci_high"] < 0,
        }
    report = {
        "format": "mnemosyne-retention/v1",
        "corpus_digest": corpus["digest"],
        "engine": attestation,
        "provider_hashes": provider_tree_hashes(),
        "provider_import": require_vendored_provider(
            importlib.import_module("hermes_memory_provider")
        ),
        "settings": {
            "ingest": "tool",
            "optional_llm": "disabled",
            "sleep_verify": args.sleep_verify,
            "sleep_demote": args.sleep_demote,
            "stages": 4,
            "resamples": args.resamples,
        },
        "lives": len(lives),
        "arms": results,
        "controls": gates,
        "bwt_vs_no_sleep": {
            arm: {
                surface: compare_bwt(
                    results["no-sleep"]["records"],
                    results[arm]["records"],
                    surface,
                    resamples=args.resamples,
                )
                for surface in ("tool", "prefetch")
            }
            for arm in (
                "sleep",
                "sleep+degrade",
                "sleep+degrade-verified",
                "sleep+degrade-additive",
            )
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps(gates, indent=2))
    controls_ok = all(
        g["aa_pass"] and g["deletion_detected"] and g["sleep_loss_detected"] for g in gates.values()
    )
    quality_ok = all(
        results[arm]["summary"].get(surface, {}).get("invariants_pass", False)
        for arm in (
            "no-sleep",
            "sleep",
            "sleep+degrade",
            "sleep+degrade-verified",
            "sleep+degrade-additive",
        )
        for surface in ("tool", "prefetch")
    )
    print(f"invariants: {'PASS' if quality_ok else 'FAIL'}")
    return result_exit_code(controls_ok, quality_ok)


def entrypoint() -> int:
    import sys

    try:
        return main()
    except Exception as error:
        print(f"retention measurement failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(entrypoint())
