"""Row-score probe for the five over-return benchmark queries.

Same seeding as the PrecisionMemBench replay. Prints the raw engine rows and
their composite scores, to test whether an evidence-free score floor could
separate 'irrelevant query junk' from 'paraphrase hit'.

Run: PYTHONPATH=.:integrations/hermes-provider \
  /mnt/tank/t3code/.research/pmb-venv/bin/python research/overreturn_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "hermes-provider"))

from bench.precisionmembench.run_eval import (  # noqa: E402
    belief_to_text,
    load_fixtures,
    seed_metadata,
)
from bench.scorecard.runner import LifeStore, clean_environment  # noqa: E402

QUERIES = [
    "any open questions?",
    "any unresolved decisions on this sprint?",
    "what's our stack?",
    "Vue component architecture",
    "should you give me one recommendation or a list of options?",
]


def main() -> int:
    _, beliefs = load_fixtures()
    with tempfile.TemporaryDirectory(prefix="overreturn-probe-") as folder:
        scratch = Path(folder)
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(clean_environment(scratch))
        store = None
        try:
            store = LifeStore.create(scratch, "overreturn-probe")
            alias_index = {}
            for belief in beliefs:
                payload = {
                    "content": belief_to_text(belief),
                    "importance": float(belief.get("confidence") or 0.5),
                    "source": "precisionmembench",
                    "metadata": seed_metadata(belief),
                }
                result = json.loads(
                    store.provider.handle_tool_call("mnemosyne_remember", payload)
                )
                if isinstance(result.get("memory_id"), str):
                    alias_index[result["memory_id"]] = belief["_id"]
            provider = store.provider
            for query in QUERIES:
                rows = provider._beam.recall(
                    query, top_k=20, temporal_weight=0.0, query_time=None,
                    temporal_halflife=24, explain=False,
                )
                print(f"\nquery={query!r}")
                for row in rows:
                    print(
                        f"  {alias_index.get(row.get('id'), row.get('id')):30s} "
                        f"score={row.get('score'):.4f} "
                        f"dense={row.get('dense_score')} fts={row.get('fts_score')} "
                        f"importance={row.get('importance')}"
                    )
        finally:
            if store is not None:
                store.close()
            os.environ.clear()
            os.environ.update(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
