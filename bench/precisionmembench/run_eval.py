"""PrecisionMemBench single-turn suite, replayed against the real provider.

The upstream suite drives an HTTP adapter (``/add``, ``/search``,
``/reset``) and its own TypeScript assertion runner. This module implements the
same adapter semantics in Python — ``seedMetadata``, ``beliefToText``,
``buildContext`` including pinned-fact exclusion, per-scope fan-out, the
``excludeIds`` filter and the ``maxBeliefs`` cap — and grades with the same
assertion set. Fixtures are the pinned upstream files, byte-hashed here.

For this provider replay, `canonical_name` and `aliases` are also copied into
engine metadata so P48 can use the structured fields sent separately by the
upstream seed path. Reports set `structuredLabelMetadata: true`; do not compare
them with older runs that omitted these fields as if ingestion were identical.

Why re-implement rather than shell out to node: the provider is a Python object,
and the interesting failure mode (an engine recall that returns the wrong rows)
only appears when ``/search`` is answered by the real engine. The adapter
semantics are transcribed rather than approximated, and the transcription is
gated by tests that replay the published ``tenure`` numbers' *shape* (43 active,
77 total, 9 trivially empty).

The pass criteria this module reports are the ones the work order names: active
passes out of 43, and missing required beliefs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CASES = HERE / "retrieval.cases.json"
BELIEFS = HERE / "beliefs.seed.json"
ROOT = HERE.parents[1]

# Recorded upstream digests. The pinned commit ships CRLF, so the digest is of
# the CRLF form; the checked-in fixture is stored LF by `.gitattributes`
# (`* -text`) and this module reconstructs the CRLF form before hashing.
EXPECTED = {
    "retrieval.cases.json": "6ab5a626356f4a6e947bee8e2e9a95af9ead7cfd58237e4aae6f02ae490db0ba",
    "beliefs.seed.json": "d6ae1c270faba0e8bf2bc0eb4527cd008d481c451a9cd557b5ab85f918fe33f3",
}
UPSTREAM_COMMIT = "61a712a1754e7d27b6dee42da32a2c214b003ece"

USER_ID = "test-user"
DEFAULT_BUDGET = {"maxBeliefs": 20, "maxPinnedFacts": 10, "maxQuestions": 15}

EVAL_PERSONA_UNIVERSAL = (
    "You prefer direct answers without preamble. You push back when plans have "
    "problems rather than defaulting to agreement. You edit AI output; you do not "
    "let AI edit your prose."
)
EVAL_PERSONA_SCOPED = {
    "domain:code": (
        "You work in TypeScript with strict mode, Fastify for HTTP, and MongoDB with "
        "the raw driver — never an ORM. You prefer composition over inheritance and "
        "Go-style explicit error returns."
    ),
    "domain:writing": (
        "You write close third-person, present tense, set in 1970s Lisbon. No "
        "omniscient asides, no reconciliation arcs."
    ),
}


def _crlf_digest(path: Path) -> str:
    body = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return hashlib.sha256(body).hexdigest()


def verify_fixtures() -> dict[str, str]:
    """Refuse to score against fixtures that are not the pinned upstream bytes."""
    actual = {}
    for name, expected in EXPECTED.items():
        digest = _crlf_digest(HERE / name)
        if digest != expected:
            raise ValueError(
                f"{name} does not match the pinned upstream fixture: "
                f"expected {expected}, computed {digest}"
            )
        actual[name] = digest
    return actual


def load_fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    verify_fixtures()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    beliefs = json.loads(BELIEFS.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not isinstance(beliefs, list):
        raise ValueError("fixtures must be JSON arrays")
    return cases, beliefs


def belief_to_text(belief: dict[str, Any]) -> str:
    """`BaseAdapter.beliefToText` with the default `canonical_name_aliases`."""
    parts = [
        belief.get("canonical_name") or "",
        *(belief.get("aliases") or []),
        belief.get("content") or "",
        belief.get("why_it_matters") or "",
    ]
    return " ".join(str(part) for part in parts if part)


def seed_metadata(belief: dict[str, Any]) -> dict[str, Any]:
    """Map upstream fields and preserve structured query labels in metadata."""
    return {
        "beliefId": belief["_id"],
        "scope": (belief.get("scope") or [None])[0],
        "type": belief.get("type"),
        "pinned": belief.get("pinned"),
        "superseded_by": belief.get("superseded_by"),
        "canonical_name": belief.get("canonical_name"),
        "aliases": belief.get("aliases") or [],
    }


class Adapter:
    """The upstream adapter's reader interface, answered by the real provider.

    ``search`` filters to seed-known ids and honours ``excludeIds`` exactly as
    the upstream adapter does, because that filter — not the engine — is what
    keeps pinned facts out of ``relevantBeliefs``.
    """

    def __init__(
        self,
        store: Any,
        seed_index: dict[str, dict[str, Any]],
        alias_index: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.seed_index = seed_index
        self.alias_index = alias_index or {}

    def search_text(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
        exclude_ids: set[str] | None = None,
        scope: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """`BaseAdapter.searchText`, including its scope contract.

        The upstream adapter forwards `scope` to the provider's `/search`, and
        the provider is responsible for returning only that scope. Mnemosyne's
        engine `scope` is session/global *visibility*, not a benchmark domain
        tag, so the provider cannot answer this — the filter is applied here,
        against the belief's own `scope` list, which is exactly the predicate
        the benchmark's expectations are written against. Leaving it out (as an
        earlier revision of this harness did) reports every cross-domain row as
        an over-return failure and understates the provider.
        """
        if not query.strip():
            return []
        scope_values = (
            None if scope is None else ([scope] if isinstance(scope, str) else list(scope))
        )
        payload = self.store.recall(query, limit=limit)
        if payload.get("error"):
            raise RuntimeError(f"provider recall failed: {payload['error']}")
        rows = payload.get("results") or []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            belief_id = _belief_id_from_row(row, self.seed_index, self.alias_index)
            if not belief_id or belief_id in seen:
                continue
            seen.add(belief_id)
            if exclude_ids and belief_id in exclude_ids:
                continue
            belief = self.seed_index.get(belief_id)
            if belief is None:
                continue
            if belief.get("user_id") != user_id:
                continue
            if not _is_current(belief):
                continue
            # Open questions have their own context channel. Letting recall
            # copy one into relevantBeliefs violates the adapter contract and
            # duplicates it beside the pinned/open-question result.
            if belief.get("type") == "open_question":
                continue
            if scope_values is not None and not _scope_overlaps(belief, scope_values):
                continue
            out.append(belief)
        return out

    def list_pinned_facts(self, user_id: str, scope: list[str]) -> list[dict[str, Any]]:
        return [
            belief
            for belief in self.seed_index.values()
            if belief.get("user_id") == user_id
            and belief.get("pinned") is True
            and belief.get("type") != "open_question"
            and _is_current(belief)
            and _scope_overlaps(belief, scope)
        ]

    def list_pinned_open_questions(self, user_id: str, scope: list[str]) -> list[dict[str, Any]]:
        return [
            belief
            for belief in self.seed_index.values()
            if belief.get("user_id") == user_id
            and belief.get("type") == "open_question"
            and belief.get("pinned") is True
            and _is_current(belief)
            and _scope_overlaps(belief, scope)
        ]

    def expand_relation_participants(
        self,
        user_id: str,
        relation_beliefs: list[dict[str, Any]],
        scope: list[str],
        exclude_ids: set[str],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for relation in relation_beliefs:
            if relation.get("type") != "relation":
                continue
            for participant in relation.get("participants") or []:
                if participant in exclude_ids:
                    continue
                belief = self.seed_index.get(participant)
                if belief is None:
                    continue
                if belief.get("type") == "open_question":
                    continue
                if belief.get("user_id") != user_id:
                    continue
                if not _is_current(belief):
                    continue
                if scope and not _scope_overlaps(belief, scope):
                    continue
                results.append(belief)
        return results

    def build_context(
        self,
        user_id: str,
        scope: list[str],
        raw_query: str,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`BaseAdapter.buildContext`, without the HTTP hop.

        Mnemosyne recall is unscoped here (the engine's `scope` is
        session/global visibility, not benchmark domain tags). The external
        adapter therefore runs one recall per active scope and interleaves the
        ranked results, matching its unsupported-multi-scope path.
        """
        merged = {**DEFAULT_BUDGET, **(budget or {})}
        pinned_facts = self.list_pinned_facts(user_id, scope)
        questions = self.list_pinned_open_questions(user_id, scope)
        pinned_ids = {belief["_id"] for belief in pinned_facts}

        raw_results: list[dict[str, Any]] = []
        if raw_query.strip() and merged["maxBeliefs"] > 0:
            if len(scope) > 1:
                scoped_results = [
                    self.search_text(
                        raw_query,
                        user_id=user_id,
                        limit=merged["maxBeliefs"],
                        exclude_ids=pinned_ids,
                        scope=scope_value,
                    )
                    for scope_value in scope
                ]
                seen: set[str] = set()
                max_length = max((len(results) for results in scoped_results), default=0)
                for rank in range(max_length):
                    for results in scoped_results:
                        if rank >= len(results):
                            continue
                        belief = results[rank]
                        belief_id = belief["_id"]
                        if belief_id in seen:
                            continue
                        seen.add(belief_id)
                        raw_results.append(belief)
                        if len(raw_results) >= merged["maxBeliefs"]:
                            break
                    if len(raw_results) >= merged["maxBeliefs"]:
                        break
            else:
                raw_results = self.search_text(
                    raw_query,
                    user_id=user_id,
                    limit=merged["maxBeliefs"],
                    exclude_ids=pinned_ids,
                    scope=scope[0] if scope else None,
                )

        expansions: list[dict[str, Any]] = []
        if raw_results:
            expansions = self.expand_relation_participants(
                user_id,
                raw_results,
                scope,
                exclude_ids={*pinned_ids, *(belief["_id"] for belief in raw_results)},
            )

        cap = merged["maxBeliefs"]
        capped_pinned = pinned_facts[:cap]
        capped_relevant = (raw_results + expansions)[: max(0, cap - len(capped_pinned))]
        persona = EVAL_PERSONA_UNIVERSAL if user_id == USER_ID else ""
        return {
            "personaPrelude": persona,
            "pinnedFacts": capped_pinned,
            "relevantBeliefs": capped_relevant,
            "openQuestions": questions[: merged["maxQuestions"]],
        }


def _scope_overlaps(belief: dict[str, Any], scope: list[str]) -> bool:
    belief_scopes = belief.get("scope") or []
    return any(value in belief_scopes for value in scope)


def _is_current(belief: dict[str, Any]) -> bool:
    return not belief.get("superseded_by") and not belief.get("resolved_at")


def _belief_id_from_row(
    row: dict[str, Any],
    seed_index: dict[str, dict[str, Any]],
    alias_index: dict[str, str] | None = None,
) -> str | None:
    """Map a provider result back to a benchmark belief id.

    The engine assigns its own memory id at capture time, so the id it returns is
    a *store* id; `alias_index` maps it to the benchmark belief. Content is
    deliberately NOT used (two beliefs can share text), and an unmappable row is
    dropped by the caller rather than guessed at.
    """
    candidate = row.get("id")
    if isinstance(candidate, str):
        if alias_index and candidate in alias_index:
            return alias_index[candidate]
        if candidate in seed_index:
            return candidate
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict):
        belief_id = metadata.get("beliefId")
        if isinstance(belief_id, str) and belief_id in seed_index:
            return belief_id
    return None


def score_case(
    case: dict[str, Any],
    built: dict[str, Any],
    pinned_in_seed: set[str],
) -> dict[str, Any]:
    """The upstream assertion set, plus the two reported pass criteria."""
    pinned_ids = {belief["_id"] for belief in built["pinnedFacts"]}
    relevant_ids = {belief["_id"] for belief in built["relevantBeliefs"]}
    question_ids = {belief["_id"] for belief in built["openQuestions"]}
    union_ids = pinned_ids | relevant_ids
    expect = case.get("expect") or {}
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    relevant_expect = expect.get("relevantBeliefs") or {}
    for belief_id in relevant_expect.get("mustInclude") or []:
        check(belief_id in union_ids, f"missing expected belief: {belief_id}")
    for belief_id in relevant_expect.get("mustExclude") or []:
        check(belief_id not in union_ids, f"forbidden belief surfaced: {belief_id}")
    for belief_id in relevant_expect.get("shouldInclude") or []:
        check(belief_id in union_ids, f"expected belief missing (shouldInclude): {belief_id}")

    only_expected = relevant_expect.get("shouldOnlyInclude")
    if only_expected is not None:
        expected_set = set(only_expected)
        for belief_id in relevant_ids:
            check(
                belief_id in expected_set,
                f"unexpected belief in relevantBeliefs: {belief_id}",
            )
        for belief_id in expected_set:
            check(belief_id in relevant_ids, f"missing expected belief: {belief_id}")

    max_count = relevant_expect.get("maxCount")
    if max_count is not None:
        check(
            len(relevant_ids) <= max_count,
            f"relevantBeliefs count {len(relevant_ids)} > maxCount {max_count}",
        )
    min_count = relevant_expect.get("minCount")
    if min_count is not None:
        check(
            len(relevant_ids) >= min_count,
            f"relevantBeliefs count {len(relevant_ids)} < minCount {min_count}",
        )

    relevant_order = [belief["_id"] for belief in built["relevantBeliefs"]]
    for pair in relevant_expect.get("orderedBefore") or []:
        first, second = pair
        first_index = relevant_order.index(first) if first in relevant_order else -1
        second_index = relevant_order.index(second) if second in relevant_order else -1
        check(first_index != -1, f"orderedBefore: {first} not in relevantBeliefs")
        check(second_index != -1, f"orderedBefore: {second} not in relevantBeliefs")
        if first_index != -1 and second_index != -1:
            check(
                first_index < second_index,
                f"ranking: {first} (idx {first_index}) should precede {second} (idx {second_index})",
            )

    pinned_expect = expect.get("pinnedFacts") or {}
    for belief_id in pinned_expect.get("mustInclude") or []:
        check(belief_id in pinned_ids, f"missing pinned belief: {belief_id}")
    for belief_id in pinned_expect.get("mustExclude") or []:
        check(belief_id not in pinned_ids, f"forbidden belief in pinnedFacts: {belief_id}")

    question_expect = expect.get("openQuestions") or {}
    for belief_id in question_expect.get("mustInclude") or []:
        check(belief_id in question_ids, f"missing expected question: {belief_id}")
    for belief_id in question_expect.get("mustExclude") or []:
        check(belief_id not in question_ids, f"forbidden question surfaced: {belief_id}")

    persona_expect = expect.get("personaPrelude")
    if persona_expect:
        prelude = built["personaPrelude"]
        if persona_expect.get("nonEmpty"):
            check(len(prelude) > 0, "personaPrelude empty")
        if persona_expect.get("isNull"):
            check(prelude == "", "personaPrelude not empty")
        for fragment in persona_expect.get("contains") or []:
            check(fragment in prelude, f'personaPrelude missing "{fragment}"')
        for fragment in persona_expect.get("mustNotContain") or []:
            check(fragment not in prelude, f'personaPrelude contains "{fragment}"')

    if only_expected is not None:
        expected_relevant = set(only_expected)
    else:
        expected_relevant = {
            belief_id
            for belief_id in (relevant_expect.get("mustInclude") or [])
            if belief_id not in pinned_in_seed
        }
    hits = {belief_id for belief_id in expected_relevant if belief_id in relevant_ids}
    missing = sorted(expected_relevant - relevant_ids)

    if not relevant_ids and not expected_relevant:
        precision: float | None = None
    elif not relevant_ids:
        precision = 0.0
    else:
        precision = len(hits) / len(relevant_ids)
    recall = None if not expected_relevant else len(hits) / len(expected_relevant)

    return {
        "caseId": case["caseId"],
        "category": case.get("category"),
        "active": bool(expected_relevant),
        "pinnedBeliefs": sorted(pinned_ids),
        "relevantBeliefs": sorted(relevant_ids),
        "retrievedQuestions": sorted(question_ids),
        "retrievalPrecision": precision,
        "retrievalRecall": recall,
        "missingRelevant": missing,
        "passed": not failures,
        "failures": failures,
    }


def _mean(values: list[float | None]) -> float | None:
    numeric = [value for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    active = [row for row in rows if row["active"]]
    return {
        "totalCases": len(rows),
        "totalPasses": sum(1 for row in rows if row["passed"]),
        "activeCases": len(active),
        "activePasses": sum(1 for row in active if row["passed"]),
        "meanActivePrecision": _mean([row["retrievalPrecision"] for row in active]),
        "meanActiveRecall": _mean([row["retrievalRecall"] for row in active]),
        "missingExpectedBeliefs": sum(len(row["missingRelevant"]) for row in rows),
        "activeMissingExpectedBeliefs": sum(len(row["missingRelevant"]) for row in active),
        "overreturnFailures": sum(
            1
            for row in rows
            if any(
                failure.startswith("unexpected belief") or failure.startswith("forbidden belief")
                for failure in row["failures"]
            )
        ),
    }


def run(*, limit: int | None = None) -> dict[str, Any]:
    """Seed the suite into a fresh provider store and score every case."""
    from bench.scorecard.runner import LifeStore, clean_environment

    cases, beliefs = load_fixtures()
    seed_index = {belief["_id"]: belief for belief in beliefs}
    pinned_in_seed = {
        belief["_id"]
        for belief in beliefs
        if belief.get("pinned") is True and belief.get("user_id") == USER_ID
    }

    alias_index: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="precisionmembench-") as folder:
        scratch = Path(folder)
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(clean_environment(scratch))
        store = None
        try:
            store = LifeStore.create(scratch, "precisionmembench")
            ingested = 0
            for belief in beliefs:
                payload = {
                    "content": belief_to_text(belief),
                    "importance": float(belief.get("confidence") or 0.5),
                    "source": "precisionmembench",
                    "metadata": seed_metadata(belief),
                }
                raw = store.provider.handle_tool_call("mnemosyne_remember", payload)
                result = json.loads(raw)
                if result.get("error"):
                    raise RuntimeError(f"seed failed for {belief['_id']}: {result['error']}")
                belief_id = result.get("memory_id")
                if isinstance(belief_id, str) and belief_id:
                    alias_index[belief_id] = belief["_id"]
                ingested += 1
            # `seed_index` stays keyed by benchmark id; `alias_index` maps the
            # engine's generated id back to it. Both are needed because the
            # assertion set is written in benchmark ids.
            adapter = Adapter(store, seed_index, alias_index)
            rows = []
            per_case_limit = limit or DEFAULT_BUDGET["maxBeliefs"]
            for case in cases:
                budget = dict(case.get("budget") or {})
                budget.setdefault("maxBeliefs", per_case_limit)
                built = adapter.build_context(
                    case.get("userId", USER_ID),
                    case.get("scope") or [],
                    case.get("query") or "",
                    budget,
                )
                rows.append(score_case(case, built, pinned_in_seed))
        finally:
            if store is not None:
                store.close()
            os.environ.clear()
            os.environ.update(previous)

    return {
        "dataset": "PrecisionMemBench single-turn",
        "upstreamCommit": UPSTREAM_COMMIT,
        "fixtureSha256": verify_fixtures(),
        "seededBeliefs": ingested,
        "multiScopeFanout": True,
        "structuredLabelMetadata": True,
        "summary": summarize(rows),
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--require-active-passes", type=int, default=None)
    parser.add_argument("--require-missing-beliefs", type=int, default=None)
    args = parser.parse_args(argv)
    report = run(limit=args.limit)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, sort_keys=True, indent=1), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    status = 0
    if (
        args.require_active_passes is not None
        and report["summary"]["activePasses"] < args.require_active_passes
    ):
        print(
            f"FAIL: active passes {report['summary']['activePasses']} "
            f"< required {args.require_active_passes}",
            file=sys.stderr,
        )
        status = 1
    if (
        args.require_missing_beliefs is not None
        and report["summary"]["missingExpectedBeliefs"] > args.require_missing_beliefs
    ):
        print(
            f"FAIL: missing beliefs {report['summary']['missingExpectedBeliefs']} "
            f"> allowed {args.require_missing_beliefs}",
            file=sys.stderr,
        )
        status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
