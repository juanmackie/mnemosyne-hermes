"""Job entry: replay the pinned local retrieval workload and attest outputs."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(sys.argv[1]).resolve()


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from bench.retrieval.run_eval import main as evaluate

    sys.argv = ["run_eval.py", "--all", "--json-out", str(OUTPUT / "retrieval.json")]
    status = evaluate()
    if status != 0:
        return status
    result = OUTPUT / "retrieval.json"
    report = json.loads(result.read_text(encoding="utf-8"))
    report["corpus"] = "bench/retrieval/corpus.jsonl"
    result.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    digest = hashlib.sha256(result.read_bytes()).hexdigest()
    (OUTPUT / "manifest.json").write_text(
        json.dumps(
            {"format": "mnemosyne-foundry-output/v1", "outputs": {"retrieval.json": digest}},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
