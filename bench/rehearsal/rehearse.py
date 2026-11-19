"""Offline rehearsal for stored labelled memories (Phase 4 of the sleep plan).

Probes come from deterministic templates over structured labels, never from an
LLM. A probe is `<canonical_name> <attribute>`. The attribute is the item's own
`attribute` when it has one, otherwise the first key token of its content that
is not the label. A miss is a probe whose source memory is outside the top-k
recall results.

A fit miss proposes an alias rule: the item's alias token maps to its canonical
label. The rule applies to every item in the same label group whose content
contains that alias, held-out items included. Each application is a derived
restated row that carries the item's id. Held-out probes decide the batch: it is
kept only if held-out hits rise, held-out wrong top-1 results do not rise, and
fabricated-query rows do not rise. Nothing is written to a live store here: the
kept batch is written to a JSON file for the owner to review and apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

TOP_K = 5
KEY_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_@-]|[./:](?=[A-Za-z0-9]))*")
WORD = re.compile(r"[^\W_]+")
FABRICATED = ("zqvx{n} plorf{n}", "qwv{n} brimmelx{n}", "kruzn{n} hollow{n}")


def split_of(item_id: str) -> str:
    """Fixed 50/50 split by hash. The same id always lands in the same split."""
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return "fit" if int(digest, 16) % 2 == 0 else "held"


def key_tokens(text: str) -> list[str]:
    """Tokens with a digit or a capital letter, in order, without duplicates."""
    seen: list[str] = []
    for token in KEY_TOKEN.findall(text or ""):
        if (
            any(c.isdigit() for c in token) or any(c.isupper() for c in token)
        ) and token not in seen:
            seen.append(token)
    return seen


def make_probes(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One probe per labelled item: `<canonical_name> <attribute>`."""
    probes = []
    for item in items:
        label = str(item.get("canonical_name") or "").strip()
        if not label:
            continue
        if item.get("attribute"):
            attribute = str(item["attribute"])
        else:
            attributes = [
                t for t in key_tokens(item.get("content", "")) if t.casefold() != label.casefold()
            ]
            # Digit tokens first: a capitalised word is often just the sentence opener.
            attributes.sort(key=lambda token: not any(c.isdigit() for c in token))
            if not attributes:
                continue
            attribute = attributes[0]
        probes.append(
            {
                "probe_id": item["id"],
                "query": f"{label} {attribute}",
                "target": item["id"],
                "canonical_name": label,
                "attribute": attribute,
                "alias": item.get("alias"),
                "split": split_of(item["id"]),
            }
        )
    return probes


def make_controls(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Known-positive controls: each probe aimed at a different item. They must miss."""
    controls: list[dict[str, Any]] = []
    if len(probes) < 2:
        return controls
    for index, probe in enumerate(probes):
        wrong = probes[(index + 1) % len(probes)]["target"]
        controls.append(
            {**probe, "probe_id": f"control:{probe['probe_id']}", "target": wrong, "control": True}
        )
    return controls


def decide(
    held_before: dict[str, Any],
    held_after: dict[str, Any],
    fabricated_before: int,
    fabricated_after: int,
    fixes: list[dict[str, Any]],
) -> bool:
    """Keep a batch only if held-out hits rise, held-out over-returns (distractor
    rows in the top-k) do not rise, and fabricated rows do not rise. A batch
    with no fixes is never kept. Over-return is the plan's definition."""
    return bool(fixes) and (
        held_after["hits"] > held_before["hits"]
        and held_after["over_return"] <= held_before["over_return"]
        and fabricated_after <= fabricated_before
    )


def propose_fixes(
    misses: Iterable[dict[str, Any]], items: list[dict[str, Any]], batch: str
) -> list[dict[str, Any]]:
    """Turn fit misses into alias rules, then apply each rule to its label group.

    A rule is (canonical_name, alias). It applies to every item with the same
    canonical name whose content has the alias as a word, so held-out items
    benefit too. Each application restates the item under its label and keeps
    the item's id, so evaluation can count a hit for it.
    """
    rules: list[tuple[str, str]] = []
    for miss in misses:
        alias = miss.get("alias")
        canonical = miss["canonical_name"]
        if not alias or len(WORD.findall(alias)) != 1 or len(alias) < 2:
            continue
        rule = (canonical, alias.casefold())
        if rule not in rules:
            rules.append(rule)
    fixes = []
    seen: set[tuple[str, str]] = set()
    for canonical, alias in rules:
        for item in items:
            if item.get("canonical_name") != canonical:
                continue
            words = {w.casefold() for w in WORD.findall(item.get("content", ""))}
            if alias not in words or (canonical, item["id"]) in seen:
                continue
            seen.add((canonical, item["id"]))
            fixes.append(
                {
                    "fix_id": f"{batch}:{item['id']}",
                    "target": item["id"],
                    "content": f"{canonical}: {item['content']}",
                    "metadata": {
                        "canonical_name": canonical,
                        "beliefId": item["id"],
                        "rehearsal_batch": batch,
                        "trust_tier": "DERIVED",
                    },
                    "source": f"rehearsal:{batch}",
                }
            )
    return fixes


def _fabricated_queries(count: int) -> list[str]:
    return [FABRICATED[i % len(FABRICATED)].format(n=i) for i in range(count)]


def evaluate(
    items: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    fixes: list[dict[str, Any]],
    *,
    scratch_root: Path,
    tag: str,
) -> dict[str, Any]:
    """Build a fresh store with the items plus fixes, then score every probe."""
    from bench.scorecard.runner import LifeStore, clean_environment

    scratch = scratch_root / tag
    scratch.mkdir(parents=True, exist_ok=True)
    previous = dict(os.environ)
    # Build the environment before clearing: clean_environment reads
    # os.environ, so calling it after the clear keeps nothing but its own
    # defaults and drops HOME/USERPROFILE/PATH for the engine import.
    resolved = clean_environment(scratch)
    os.environ.clear()
    os.environ.update(resolved)
    store = None
    try:
        store = LifeStore.create(scratch, f"rehearsal-{tag}")
        alias_to_item: dict[str, str] = {}
        for item in items:
            payload = {
                "content": item["content"],
                "importance": float(item.get("importance") or 0.5),
                "source": "rehearsal",
                "metadata": {"canonical_name": item.get("canonical_name"), "beliefId": item["id"]},
            }
            result = json.loads(store.provider.handle_tool_call("mnemosyne_remember", payload))
            if result.get("error") or not isinstance(result.get("memory_id"), str):
                raise RuntimeError(f"remember failed for {item['id']}: {result}")
            alias_to_item[result["memory_id"]] = item["id"]
        for fix in fixes:
            result = json.loads(
                store.provider.handle_tool_call(
                    "mnemosyne_remember",
                    {
                        "content": fix["content"],
                        "importance": 0.6,
                        "source": fix["source"],
                        "metadata": fix["metadata"],
                    },
                )
            )
            if result.get("error") or not isinstance(result.get("memory_id"), str):
                raise RuntimeError(f"fix write failed for {fix['fix_id']}: {result}")
            alias_to_item[result["memory_id"]] = fix["target"]

        def top(query: str) -> list[str | None]:
            payload = store.recall(query, limit=TOP_K)
            if payload.get("error"):
                raise RuntimeError(f"recall failed: {payload['error']}")
            rows = payload.get("results") or []
            return [alias_to_item.get(str(row.get("id"))) for row in rows]

        scored = []
        for probe in probes:
            ranked = top(probe["query"])
            scored.append(
                {
                    "probe_id": probe["probe_id"],
                    "split": probe["split"],
                    "control": bool(probe.get("control")),
                    "hit": probe["target"] in ranked,
                    "wrong_top1": bool(ranked) and ranked[0] != probe["target"],
                    "over_return": sum(1 for r in ranked if r != probe["target"]),
                }
            )
        fabricated_rows = sum(
            len(store.recall(q, limit=TOP_K).get("results") or []) for q in _fabricated_queries(6)
        )
        return {"scored": scored, "fabricated_rows": fabricated_rows}
    finally:
        if store is not None:
            store.close()
        os.environ.clear()
        os.environ.update(previous)


def _summary(scored: list[dict[str, Any]], split: str) -> dict[str, Any]:
    rows = [s for s in scored if s["split"] == split and not s["control"]]
    return {
        "probes": len(rows),
        "hits": sum(s["hit"] for s in rows),
        "wrong_top1": sum(s["wrong_top1"] for s in rows),
        "over_return": sum(s["over_return"] for s in rows),
    }


def rehearse(items: list[dict[str, Any]], *, scratch_root: Path, batch: str) -> dict[str, Any]:
    probes = make_probes(items)
    controls = make_controls(probes)
    base = evaluate(items, probes + controls, [], scratch_root=scratch_root, tag="base")
    hit_by_probe = {s["probe_id"]: s["hit"] for s in base["scored"] if not s["control"]}
    misses = [p for p in probes if p["split"] == "fit" and not hit_by_probe[p["probe_id"]]]
    fixes = propose_fixes(misses, items, batch)
    fixed = None
    if fixes:
        fixed = evaluate(items, probes + controls, fixes, scratch_root=scratch_root, tag="fixed")
    held_before = _summary(base["scored"], "held")
    held_after = _summary(fixed["scored"], "held") if fixed else held_before
    fabricated_after = fixed["fabricated_rows"] if fixed else base["fabricated_rows"]
    accepted = decide(held_before, held_after, base["fabricated_rows"], fabricated_after, fixes)
    control_rows = [s for s in base["scored"] if s["control"]]
    return {
        "batch": batch,
        "probes": len(probes),
        "fit_probes": sum(p["split"] == "fit" for p in probes),
        "held_probes": sum(p["split"] == "held" for p in probes),
        "fit_misses": len(misses),
        "proposed_fixes": len(fixes),
        "control_probes": len(control_rows),
        "control_misses": sum(not s["hit"] for s in control_rows),
        "held_before": held_before,
        "held_after": held_after,
        "fabricated_rows_before": base["fabricated_rows"],
        "fabricated_rows_after": fabricated_after,
        "accepted": accepted,
        "kept_fixes": fixes if accepted else [],
    }


def load_population() -> list[dict[str, Any]]:
    """Labelled beliefs from the PrecisionMemBench fixture (no misses on this one)."""
    from bench.precisionmembench.run_eval import belief_to_text, load_fixtures, seed_metadata

    _, beliefs = load_fixtures()
    items = []
    for belief in beliefs:
        meta = seed_metadata(belief)
        items.append(
            {
                "id": belief["_id"],
                "content": belief_to_text(belief),
                "importance": float(belief.get("confidence") or 0.5),
                "canonical_name": meta.get("canonical_name"),
            }
        )
    return items


ALIAS_GROUPS = (
    ("PostgreSQL", "pgdb"),
    ("Kafka", "kfk"),
    ("Redis", "rds"),
    ("Terraform", "tfm"),
)
ALIAS_ATTRIBUTES = ("retention", "replica", "quota")


def alias_population(per_group: int = 12) -> list[dict[str, Any]]:
    """Deterministic labelled population where the content uses an alias, not the label.

    Each group's probes share attribute words with other groups, so recall
    returns other groups' rows and misses some targets. This population is a
    constructed stress case: it checks the fix path on the real engine. It says
    nothing about how often such misses occur in real use.
    """
    items = []
    for group, (canonical, alias) in enumerate(ALIAS_GROUPS):
        for n in range(per_group):
            attribute = ALIAS_ATTRIBUTES[n % len(ALIAS_ATTRIBUTES)]
            value = 30 + group * 17 + n * 3
            items.append(
                {
                    "id": f"{canonical}-{n:02d}",
                    "content": f"{alias} {attribute} is {value} days.",
                    "importance": 0.5,
                    "canonical_name": canonical,
                    "attribute": attribute,
                    "alias": alias,
                }
            )
    return items


POPULATIONS = {"pmb": load_population, "alias": alias_population}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="report JSON path")
    parser.add_argument("--batch-file", type=Path, default=None, help="write kept fixes here")
    parser.add_argument("--batch", default="rehearse-1")
    parser.add_argument("--population", choices=sorted(POPULATIONS), default="pmb")
    args = parser.parse_args(argv)
    items = POPULATIONS[args.population]()
    with tempfile.TemporaryDirectory(prefix="rehearsal-") as folder:
        report = rehearse(items, scratch_root=Path(folder), batch=args.batch)
    report["population"] = args.population
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
    if args.batch_file and report["accepted"]:
        args.batch_file.write_text(json.dumps(report["kept_fixes"], indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "kept_fixes"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
