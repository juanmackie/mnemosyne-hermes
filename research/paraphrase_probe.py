"""P51 paraphrase-cost probe (research artifact, NOT a gate).

Seeds the pinned PrecisionMemBench beliefs into a fresh store (same path as
the benchmark), then asks label-disjoint paraphrase queries. Reports, per
query, whether the raw engine returned the target belief and what the
provider served after P48/P49 narrowing + P51 abstention + P53 restore.

No fixtures are read or written; this measures the abstention's residual
recall cost on labeled stores with zero-lexical-overlap queries.

Run: PYTHONPATH=.:integrations/hermes-provider \
  /mnt/tank/t3code/.research/pmb-venv/bin/python research/paraphrase_probe.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "hermes-provider"))

from bench.precisionmembench.run_eval import (  # noqa: E402
    USER_ID,
    belief_to_text,
    load_fixtures,
    seed_metadata,
)
from bench.scorecard.runner import LifeStore, clean_environment  # noqa: E402

# (target belief id, paraphrase). Each paraphrase avoids every label token
# (exact or one-edit fuzzy) of every seeded belief; validated below.
PARAPHRASES = [
    ("b-redis-code", "which in-memory tier holds the hot cache?"),
    ("b-ts-pref", "do we prefer strong static typing for our frontend?"),
    ("b-react-entity", "what library renders the web UI with TanStack Query?"),
    ("b-dead-letter-queue", "where do messages go after exhausting retries?"),
    ("b-mongo-raw-driver", "do we hit the database with typed collection handles or an ORM?"),
    ("b-ci-gha-entity", "what runs our builds when we merge to the main branch?"),
    ("b-kubernetes-entity", "how are our services deployed with Helm charts?"),
    ("b-linting-current", "what did we adopt for consistent formatting last year?"),
    ("b-vitest-pref", "what do we use for unit tests?"),
    ("b-db-decision", "why did we switch stores after evaluating options?"),
    ("b-error-handling", "how do functions signal that something broke?"),
    ("b-composition-inheritance", "how do we lay out our object models?"),
    ("b-comm-pushback", "do you flag problems with my plans or go along?"),
    ("b-comm-response-length", "do I skim or read carefully?"),
    ("b-comm-recommendation", "do you give me one pick or a menu?"),
    ("b-authorship-mode", "when you draft prose do I edit or does it edit me?"),
    ("b-narrator-voice", "how should the story be told?"),
    ("b-protagonist-entity", "who is the lead in this story?"),
    ("b-pov-shift-entity", "which chapter breaks the usual viewpoint?"),
    ("b-prose-style-user-edited", "how terse should our docs be?"),
    ("b-functional-pipeline", "how do we chain data transforms?"),
    ("b-fastify-pref", "how are our HTTP services implemented?"),
    ("b-zero-reinforcement-fresh", "how are apps organized in this repo?"),
]


def tokens(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", s.casefold())


def within_one_edit(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 2:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) >= 2 and len(right) >= 2 and left[:2] != right[:2]:
        if not (len(left) == len(right) and left[0] == right[1] and left[1] == right[0]):
            return False
    if len(left) == len(right):
        diffs = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            return (
                left[diffs[0]] == right[diffs[1]]
                and left[diffs[1]] == right[diffs[0]]
            )
        return False
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    i = j = 0
    skipped = False
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            i += 1
    return True


def label_tokens(belief: dict) -> list[list[str]]:
    out = []
    for label in ([belief.get("canonical_name") or ""] + list(belief.get("aliases") or [])):
        if label:
            out.append(tokens(label))
    return out


def main() -> int:
    cases, beliefs = load_fixtures()
    # Validation: zero exact or one-edit token overlap between paraphrase and
    # any seed label. Paraphrases failing this are reported and skipped.
    all_label_token_sets = [label_tokens(b) for b in beliefs]
    usable = []
    for target, query in PARAPHRASES:
        qtokens = tokens(query)
        collisions = []
        for b in beliefs:
            for lt in label_tokens(b):
                if len(lt) > len(qtokens):
                    continue
                for i in range(len(qtokens) - len(lt) + 1):
                    window = qtokens[i : i + len(lt)]
                    if window == lt:
                        collisions.append((b["_id"], " ".join(lt)))
                        continue
                    diffs = [(w, t) for w, t in zip(window, lt) if w != t]
                    if len(diffs) == 1 and within_one_edit(*diffs[0]):
                        collisions.append((b["_id"], " ".join(lt)))
        usable.append((target, query, sorted(set(collisions))))
    for target, query, collisions in usable:
        if collisions:
            print(f"SKIP {target}: collides with {collisions}")
    probes = [(t, q) for t, q, c in usable if not c]

    with tempfile.TemporaryDirectory(prefix="paraphrase-probe-") as folder:
        scratch = Path(folder)
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(clean_environment(scratch))
        store = None
        try:
            store = LifeStore.create(scratch, "paraphrase-probe")
            alias_index = {}
            for belief in beliefs:
                payload = {
                    "content": belief_to_text(belief),
                    "importance": float(belief.get("confidence") or 0.5),
                    "source": "precisionmembench",
                    "metadata": seed_metadata(belief),
                }
                raw = store.provider.handle_tool_call("mnemosyne_remember", payload)
                result = json.loads(raw)
                belief_id = result.get("memory_id")
                if isinstance(belief_id, str):
                    alias_index[belief_id] = belief["_id"]
            provider = store.provider
            print(f"\nseeded {len(beliefs)} beliefs; {len(probes)} validated probes\n")
            hits_raw = hits_provider = abstained = 0
            for target, query in probes:
                raw_reply = provider._beam.recall(
                    query, top_k=20, temporal_weight=0.0, query_time=None,
                    temporal_halflife=24, explain=False,
                )
                raw_ids = [alias_index.get(row.get("id")) for row in raw_reply]
                raw_target = target in raw_ids
                print("    raw rows:")
                for row in raw_reply[:6]:
                    rid = alias_index.get(row.get("id"))
                    print(
                        f"      {rid or row.get('id')!s:30s} "
                        f"score={row.get('score')} keys={sorted(row)}"
                    )
                reply = json.loads(
                    provider.handle_tool_call(
                        "mnemosyne_recall", {"query": query, "limit": 20}
                    )
                )
                served = [alias_index.get(row.get("id")) for row in reply.get("results", [])]
                served_target = target in served
                abstained += 1 if reply.get("count", 0) == 0 else 0
                hits_raw += 1 if raw_target else 0
                hits_provider += 1 if served_target else 0
                mark = lambda b: "*" if b == target else " "
                top = ",".join(f"{mark(b)}{(b or '?')[:18]}" for b in served[:5])
                print(
                    f"{target:28s} raw={'T' if raw_target else '-'} "
                    f"served={reply.get('count', 0):2d} {top}"
                )
            print(
                f"\nraw engine found target in top-20: {hits_raw}/{len(probes)}\n"
                f"provider served target:          {hits_provider}/{len(probes)}\n"
                f"provider returned zero rows:     {abstained}/{len(probes)}\n"
            )
        finally:
            if store is not None:
                store.close()
            os.environ.clear()
            os.environ.update(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
