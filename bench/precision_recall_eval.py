#!/usr/bin/env python3
"""Replay score cutoffs against PrecisionMemBench retrieval assertions.

The script accepts the public PrecisionMemBench case and seed files plus a
ranked-result JSON file. A result row contains ``caseId`` and ``ranked``
entries with ``id`` and numeric ``score`` fields. Optional ``pinnedBeliefs``,
``retrievedQuestions``, and ``personaPrelude`` fields carry the other outputs
needed by the benchmark's full assertion scorer.

This does not call a model or connect to a provider. The ranked list must come
from a separately recorded retrieval run. A relative cutoff is applied to the
best score in each row, preserving original order among survivors.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON from {path}: {error}") from error


def _ids(value: Any, name: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return set(value)


def expected_relevant(case: dict[str, Any], pinned_seed_ids: set[str]) -> set[str]:
    relevant = case.get("expect", {}).get("relevantBeliefs", {})
    if "shouldOnlyInclude" in relevant:
        return _ids(relevant["shouldOnlyInclude"], "shouldOnlyInclude")
    required = _ids(relevant.get("mustInclude", []), "mustInclude")
    return required - pinned_seed_ids


def apply_relative_cutoff(ranked: list[dict[str, Any]], ratio: float) -> list[dict[str, Any]]:
    if not 0.0 <= ratio <= 1.0 or not math.isfinite(ratio):
        raise ValueError("cutoff ratios must be finite values from 0 through 1")
    if not ranked or ratio == 0.0:
        return list(ranked)
    best = max(row["score"] for row in ranked)
    if best <= 0.0:
        return list(ranked)
    threshold = best * ratio
    return [row for row in ranked if row["score"] >= threshold]


def _check_ids(checks: list[str], actual: set[str], required: list[str], message: str) -> None:
    for item in required:
        if item not in actual:
            checks.append(f"{message}: {item}")


def score_case(
    case: dict[str, Any],
    result: dict[str, Any],
    ranked: list[dict[str, Any]],
    pinned_seed_ids: set[str],
) -> dict[str, Any]:
    """Apply the public suite's ID assertions and retrieval metrics to one row."""
    relevant_ids = [item["id"] for item in ranked]
    relevant_set = set(relevant_ids)
    pinned_set = _ids(result.get("pinnedBeliefs", []), "pinnedBeliefs")
    question_set = _ids(result.get("retrievedQuestions", []), "retrievedQuestions")
    union_set = relevant_set | pinned_set
    expect = case.get("expect", {})
    failures: list[str] = []

    rb = expect.get("relevantBeliefs", {})
    _check_ids(failures, union_set, rb.get("mustInclude", []), "missing expected belief")
    _check_ids(
        failures,
        set(),
        [item for item in rb.get("mustExclude", []) if item in union_set],
        "forbidden belief surfaced",
    )
    _check_ids(
        failures, union_set, rb.get("shouldInclude", []), "expected belief missing (shouldInclude)"
    )
    if "shouldOnlyInclude" in rb:
        only_expected = _ids(rb["shouldOnlyInclude"], "shouldOnlyInclude")
        for item in relevant_ids:
            if item not in only_expected:
                failures.append(f"unexpected belief in relevantBeliefs: {item}")
        for item in sorted(only_expected - relevant_set):
            failures.append(f"missing expected belief: {item}")
    if rb.get("maxCount") is not None and len(relevant_set) > rb["maxCount"]:
        failures.append(f"relevantBeliefs count {len(relevant_set)} > maxCount {rb['maxCount']}")
    if rb.get("minCount") is not None and len(relevant_set) < rb["minCount"]:
        failures.append(f"relevantBeliefs count {len(relevant_set)} < minCount {rb['minCount']}")
    positions = {item: index for index, item in enumerate(relevant_ids)}
    for before, after in rb.get("orderedBefore", []):
        if before not in positions:
            failures.append(f"orderedBefore: {before} not in relevantBeliefs")
        if after not in positions:
            failures.append(f"orderedBefore: {after} not in relevantBeliefs")
        if before in positions and after in positions and positions[before] >= positions[after]:
            failures.append(f"ranking: {before} (idx {positions[before]}) should precede {after}")

    pf = expect.get("pinnedFacts", {})
    _check_ids(failures, pinned_set, pf.get("mustInclude", []), "missing pinned belief")
    _check_ids(
        failures,
        set(),
        [item for item in pf.get("mustExclude", []) if item in pinned_set],
        "forbidden belief in pinnedFacts",
    )
    oq = expect.get("openQuestions", {})
    _check_ids(failures, question_set, oq.get("mustInclude", []), "missing expected question")
    _check_ids(
        failures,
        set(),
        [item for item in oq.get("mustExclude", []) if item in question_set],
        "forbidden question surfaced",
    )

    pp = expect.get("personaPrelude", {})
    prelude = result.get("personaPrelude", "")
    if pp.get("nonEmpty") and not prelude:
        failures.append("personaPrelude empty")
    if pp.get("isNull") and prelude:
        failures.append("personaPrelude not empty")
    for fragment in pp.get("contains", []):
        if fragment not in prelude:
            failures.append(f'personaPrelude missing "{fragment}"')
    for fragment in pp.get("mustNotContain", []):
        if fragment in prelude:
            failures.append(f'personaPrelude contains "{fragment}"')

    expected = expected_relevant(case, pinned_seed_ids)
    hits = len(expected & relevant_set)
    precision = (
        None
        if not relevant_set and not expected
        else (hits / len(relevant_set) if relevant_set else 0.0)
    )
    recall = None if not expected else hits / len(expected)
    return {
        "caseId": case["caseId"],
        "category": case["category"],
        "passed": not failures,
        "failures": failures,
        "relevantBeliefs": relevant_ids,
        "expectedRelevant": sorted(expected),
        "missingRelevant": sorted(expected - relevant_set),
        "removedRelevant": sorted(expected - relevant_set),
        "retrievalPrecision": precision,
        "retrievalRecall": recall,
    }


def _validate_rankings(results: Any) -> dict[str, dict[str, Any]]:
    rows = results.get("cases") if isinstance(results, dict) else None
    if not isinstance(rows, list):
        raise ValueError("rankings JSON must contain a 'cases' array")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("caseId"), str):
            raise ValueError("each ranking row needs a string caseId")
        case_id = row["caseId"]
        if case_id in indexed:
            raise ValueError(f"duplicate ranking row: {case_id}")
        ranked = row.get("ranked")
        if not isinstance(ranked, list):
            raise ValueError(f"{case_id}: ranked must be a list")
        seen: set[str] = set()
        for candidate in ranked:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
                raise ValueError(f"{case_id}: each ranked result needs a string id")
            if candidate["id"] in seen:
                raise ValueError(f"{case_id}: duplicate ranked id {candidate['id']}")
            seen.add(candidate["id"])
            score = candidate.get("score")
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ValueError(f"{case_id}/{candidate['id']}: score must be finite numeric")
            candidate["score"] = float(score)
        indexed[case_id] = row
    return indexed


def _summarize(
    cases: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = [row for row in rows if row["expectedRelevant"]]
    return {
        "totalCases": len(cases),
        "totalPasses": sum(row["passed"] for row in rows),
        "totalAccuracy": sum(row["passed"] for row in rows) / len(rows) if rows else None,
        "activeCases": len(expected),
        "activePasses": sum(row["passed"] for row in expected),
        "meanPrecision": _mean(row["retrievalPrecision"] for row in expected),
        "meanRecall": _mean(row["retrievalRecall"] for row in expected),
        "missingExpectedBeliefs": sum(len(row["missingRelevant"]) for row in rows),
        "overreturnFailures": sum(
            any(
                failure.startswith("unexpected belief")
                or failure.startswith("forbidden belief surfaced")
                or failure.startswith("forbidden belief in pinnedFacts")
                for failure in row["failures"]
            )
            for row in rows
        ),
    }


def _mean(values: Any) -> float | None:
    numeric = [value for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else None


def evaluate(
    cases: list[dict[str, Any]],
    beliefs: list[dict[str, Any]],
    result_rows: dict[str, dict[str, Any]],
    ratios: list[float],
    engine_rankings: dict[float, dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    case_ids: list[str] = []
    for case in cases:
        case_id = case.get("caseId")
        if not isinstance(case_id, str):
            raise ValueError("cases must have unique string caseId values")
        case_ids.append(case_id)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("cases must have unique string caseId values")
    missing = sorted(set(case_ids) - set(result_rows))
    extra = sorted(set(result_rows) - set(case_ids))
    if missing or extra:
        raise ValueError(f"case rows do not match: missing={missing[:3]}, extra={extra[:3]}")
    pinned_seed = {
        belief["_id"]
        for belief in beliefs
        if (
            belief.get("pinned") is True
            and belief.get("user_id") == "test-user"
            and isinstance(belief.get("_id"), str)
        )
    }
    variants = []
    beliefs_by_id = {belief["_id"]: belief for belief in beliefs}
    for ratio in ratios:
        rows = []
        for case in cases:
            input_row = result_rows[case["caseId"]]
            if engine_rankings is None:
                selected = _postfilter_for_case(
                    apply_relative_cutoff(input_row["ranked"], ratio),
                    case,
                    beliefs_by_id,
                    set(input_row.get("pinnedBeliefs", [])),
                )
            else:
                # Gate runs inside BEAM on all raw candidate rows, then its
                # top-k is applied before benchmark-adapter user/pinned filters.
                engine_rows = engine_rankings[ratio][case["caseId"]]
                selected = _postfilter_for_case(
                    engine_rows,
                    case,
                    beliefs_by_id,
                    set(input_row.get("pinnedBeliefs", [])),
                )
            scored = score_case(case, input_row, selected, pinned_seed)
            baseline_ids = {candidate["id"] for candidate in input_row["ranked"]}
            selected_ids = {candidate["id"] for candidate in selected}
            expected = set(scored["expectedRelevant"])
            scored["removedRelevant"] = sorted((expected & baseline_ids) - selected_ids)
            rows.append(scored)
        variants.append(
            {"relativeCutoff": ratio, "summary": _summarize(cases, rows), "cases": rows}
        )
    return {
        "schemaVersion": 1,
        "dataset": "PrecisionMemBench single-turn",
        "caseCount": len(cases),
        "seedBeliefCount": len(beliefs),
        "pinnedSeedIds": sorted(pinned_seed),
        "variants": variants,
    }


def _case_side_outputs(
    case: dict[str, Any], beliefs_by_id: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str], str]:
    user_id = case.get("userId", "test-user")
    scopes = set(case.get("scope", []))
    pinned = []
    questions = []
    for belief in beliefs_by_id.values():
        if belief.get("user_id") != user_id or not set(belief.get("scope", [])) & scopes:
            continue
        if belief.get("resolved_at"):
            continue
        if belief.get("type") == "open_question":
            if belief.get("pinned") is True:
                questions.append(belief["_id"])
        elif belief.get("pinned") is True and not belief.get("superseded_by"):
            pinned.append(belief["_id"])
    budget = case.get("budget", {})
    max_pinned = int(budget.get("maxPinnedFacts", 10))
    max_beliefs = int(budget.get("maxBeliefs", 20))
    max_questions = int(budget.get("maxQuestions", 15))
    pinned = pinned[:max_pinned][:max_beliefs]
    questions = questions[:max_questions]
    persona = ""
    if user_id == "test-user":
        persona = (
            "You prefer direct answers without preamble. You push back when plans "
            "have problems rather than defaulting to agreement. You edit AI output; "
            "you do not let AI edit your prose."
        )
    return pinned, questions, persona


def _raw_ranked(response: Any) -> list[dict[str, Any]]:
    """Preserve the engine's actual pre-user-filter rows and their scores."""
    return [
        {"id": str(hit["id"]), "score": float(hit["score"])}
        for hit in response
        if hit.get("id") is not None and isinstance(hit.get("score"), (int, float))
    ]


def _postfilter_for_case(
    ranked: list[dict[str, Any]],
    case: dict[str, Any],
    beliefs_by_id: dict[str, dict[str, Any]],
    pinned_ids: set[str],
    *,
    engine_top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Apply adapter user/pinned filters and remaining result capacity.

    ``engine_top_k`` models the engine cap after score cutoff and before the
    benchmark adapter's user and pinned filters.
    """
    user_id = case.get("userId", "test-user")
    budget = case.get("budget", {})
    max_beliefs = int(budget.get("maxBeliefs", 20))
    capacity = max(0, max_beliefs - len(pinned_ids))
    candidates = ranked if engine_top_k is None else ranked[:engine_top_k]
    rows = []
    for hit in candidates:
        belief_id = hit["id"]
        belief = beliefs_by_id.get(belief_id)
        if not belief or belief.get("user_id") != user_id or belief_id in pinned_ids:
            continue
        rows.append(hit)
    return rows[:capacity]


def collect_engine_results(
    cases: list[dict[str, Any]],
    beliefs: list[dict[str, Any]],
    ratios: list[float],
) -> tuple[dict[str, dict[str, Any]], dict[float, dict[str, list[dict[str, Any]]]]]:
    """Run the engine in a fresh temporary store and verify its cutoff behavior.

    The store contains only the public 35-belief fixture. This run uses the
    provider's keyless keyword path with embeddings disabled. Scope metadata is
    retained for the user-side checks, while retrieval itself remains unscoped
    to preserve the benchmark adapter behavior under study.
    """
    try:
        from hermes_memory_provider import MnemosyneMemoryProvider
    except ImportError as error:
        raise ValueError(
            "--engine requires an environment with the installed Mnemosyne engine "
            "and integrations/hermes-provider on PYTHONPATH"
        ) from error

    beliefs_by_id = {belief["_id"]: belief for belief in beliefs}
    initial_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MNEMOSYNE_")
    }
    actual_by_ratio: dict[float, dict[str, list[dict[str, Any]]]] = {ratio: {} for ratio in ratios}
    ranked_by_case: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="mnemosyne-precision-eval-") as directory:
        scratch = Path(directory)
        fixed_environment = {
            **initial_environment,
            "HERMES_HOME": str(scratch / "hermes"),
            "MNEMOSYNE_DATA_DIR": str(scratch / "engine"),
            "MNEMOSYNE_EMBEDDINGS_OFF": "1",
            "MNEMOSYNE_AUTO_SLEEP_ENABLED": "0",
            "MNEMOSYNE_POLYPHONIC_RECALL": "0",
            "MNEMOSYNE_RECALL_SCORE_FLOOR": "0",
        }
        with patch.dict(os.environ, fixed_environment, clear=True):
            provider = MnemosyneMemoryProvider()
            provider._init_audit_log = lambda: None
            provider._read_config_key = lambda _key: None
            provider.initialize(
                session_id="precision-benchmark",
                hermes_home=str(scratch / "hermes"),
                agent_context="primary",
                db_path=str(scratch / "hermes" / "precision.db"),
                auto_sleep=False,
                shared_surface_path=str(scratch / "surface.db"),
            )
            try:
                if provider._beam is None:
                    raise ValueError(f"engine initialization failed: {provider._init_error}")
                for belief in beliefs:
                    text = " ".join(
                        [
                            str(belief.get("canonical_name") or ""),
                            *(str(alias) for alias in belief.get("aliases", [])),
                            str(belief.get("content") or ""),
                            str(belief.get("why_it_matters") or ""),
                        ]
                    ).strip()
                    memory_id = provider._beam.remember(
                        content=text,
                        importance=float(belief.get("confidence") or 0.5),
                        source="precisionmembench",
                        scope="session",
                        memory_id=belief["_id"],
                        metadata={"benchmark_user_id": belief.get("user_id")},
                    )
                    if memory_id != belief["_id"]:
                        raise ValueError(
                            f"engine changed fixture id {belief['_id']} to {memory_id}"
                        )
                # Fix timestamps so recency weighting does not depend on seed speed.
                connection = provider._beam.conn
                connection.execute(
                    "UPDATE working_memory SET timestamp = ?, recall_count = 0, last_recalled = NULL",
                    ("2026-10-01T00:00:00",),
                )
                connection.commit()

                for case in cases:
                    pinned, questions, persona = _case_side_outputs(case, beliefs_by_id)
                    response = provider._beam.recall(
                        case["query"], top_k=len(beliefs), explain=False
                    )
                    ranked = _raw_ranked(response)
                    ranked_by_case[case["caseId"]] = {
                        "caseId": case["caseId"],
                        "ranked": ranked,
                        "pinnedBeliefs": pinned,
                        "retrievedQuestions": questions,
                        "personaPrelude": persona,
                    }

                for ratio in ratios:
                    os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = str(ratio)
                    for case in cases:
                        connection.execute(
                            "UPDATE working_memory SET recall_count = 0, last_recalled = NULL"
                        )
                        connection.commit()
                        response = provider._beam.recall(
                            case["query"],
                            top_k=int(case.get("budget", {}).get("maxBeliefs", 20)),
                            explain=False,
                        )
                        actual_by_ratio[ratio][case["caseId"]] = _raw_ranked(response)
            finally:
                beam = provider._beam
                memory = provider._memory
                provider.shutdown()
                if memory is not None:
                    memory.close()
                if beam is not None:
                    beam.conn.close()
                for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
                    local = getattr(sys.modules.get(name), "_thread_local", None)
                    if local is not None:
                        local.conn = None
    return ranked_by_case, actual_by_ratio


def _parse_cutoffs(value: str) -> list[float]:
    try:
        ratios = [float(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("cutoffs must be comma-separated numbers") from error
    if not ratios or any(not math.isfinite(ratio) or not 0 <= ratio <= 1 for ratio in ratios):
        raise argparse.ArgumentTypeError("cutoffs must be finite values from 0 through 1")
    if len(set(ratios)) != len(ratios):
        raise argparse.ArgumentTypeError("cutoff values must be unique")
    return ratios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="official retrieval.cases.json")
    parser.add_argument("--beliefs", type=Path, required=True, help="official beliefs.seed.json")
    parser.add_argument("--rankings", type=Path, help="recorded ranked-result JSON")
    parser.add_argument(
        "--engine",
        action="store_true",
        help="run the installed engine against the public seed in a fresh temporary DB",
    )
    parser.add_argument(
        "--cutoffs", type=_parse_cutoffs, default=_parse_cutoffs("0,0.25,0.5,0.6,0.7,1")
    )
    parser.add_argument("--output", type=Path, help="write complete JSON report here")
    args = parser.parse_args(argv)
    try:
        cases = _read_json(args.cases)
        beliefs = _read_json(args.beliefs)
        if not isinstance(cases, list) or not isinstance(beliefs, list):
            raise ValueError("cases and beliefs inputs must be JSON arrays")
        if args.engine:
            if args.rankings:
                raise ValueError("pass either --engine or --rankings, not both")
            result_rows, engine_rows = collect_engine_results(cases, beliefs, args.cutoffs)
        else:
            if not args.rankings:
                raise ValueError("--rankings is required unless --engine is used")
            result_rows = _validate_rankings(_read_json(args.rankings))
            engine_rows = None
        report = evaluate(cases, beliefs, result_rows, args.cutoffs, engine_rows)
        if engine_rows is not None:
            report["engineRun"] = {
                "mode": "direct BEAM keyword-only fixture characterization",
                "embeddingMode": "disabled",
                "providerParity": False,
                "scope": (
                    "Public fixture beliefs are seeded into one isolated BEAM store. "
                    "The runner synthesizes user filtering, pinned/open-question lists, "
                    "and persona text from benchmark fixtures; it does not call the "
                    "Hermes MCP/provider retrieval surface."
                ),
                "scoreVariantTopK": (
                    "Each ratio, including 0, uses the case maxBeliefs inside BEAM after cutoff."
                ),
                "replayReferenceTopK": "all seeded beliefs before adapter filtering",
                "limitations": [
                    "This is keyless keyword retrieval with embeddings disabled, not deployment parity.",
                    "BEAM diversification can change rank order after cutoff, so plain score replay is diagnostic only.",
                ],
            }
            for variant in report["variants"]:
                ratio = variant["relativeCutoff"]
                actual = engine_rows[ratio]
                replayed = {
                    case["caseId"]: [
                        row["id"]
                        for row in apply_relative_cutoff(
                            result_rows[case["caseId"]]["ranked"], ratio
                        )
                    ]
                    for case in cases
                }
                mismatches = []
                for case_id in replayed:
                    actual_ids = [row["id"] for row in actual[case_id]]
                    # Candidate BEAM applies the cutoff before its own top-k
                    # and may diversify the selected list for long queries.
                    expected_ids = replayed[case_id]
                    if actual_ids != expected_ids[: len(actual_ids)]:
                        mismatches.append(case_id)
                variant["engineCutoffCheck"] = {
                    "matchesScoreReplay": not mismatches,
                    "mismatchCount": len(mismatches),
                    "mismatchCaseIds": mismatches,
                    "interpretation": (
                        "Diagnostic only: assertion scores above use actual gated BEAM "
                        "rows before adapter filters. Differences can result from "
                        "BEAM's diversification and candidate top-k ordering."
                    ),
                }
                variant["engineCutoffCheck"]["status"] = (
                    "diagnostic-only; not a pass/fail assertion"
                )
        encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        for variant in report["variants"]:
            summary = variant["summary"]
            print(
                f"cutoff={variant['relativeCutoff']:.2f} "
                f"passes={summary['totalPasses']}/{summary['totalCases']} "
                f"active={summary['activePasses']}/{summary['activeCases']} "
                f"mean_precision={summary['meanPrecision']} "
                f"mean_recall={summary['meanRecall']} "
                f"missing={summary['missingExpectedBeliefs']}"
            )
        if not args.output:
            print(encoded, end="")
        return 0
    except (ValueError, KeyError, TypeError) as error:
        print(f"precision recall eval: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
