"""Corpus loading and building for the scorecard.

The generator is ``bench/sim/scale.py``; this module only handles the on-disk
contract (a JSON file plus a sidecar digest) and the "rebuild or reuse" decision
so a run is reproducible from a digest rather than from a file timestamp.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench.sim.scale import corpus_digest as sim_corpus_digest
from bench.sim.scale import make_corpus


def build_corpus(
    *,
    seed: int,
    probes: int,
    dev_probes: int,
    probes_per_life: int,
    slots: int,
    lessons: bool = False,
) -> dict[str, Any]:
    corpus = make_corpus(
        seed,
        probes=probes,
        dev_probes=dev_probes,
        probes_per_life=probes_per_life,
        slots=slots,
        lessons=lessons,
    )
    corpus["digest"] = sim_corpus_digest(corpus)
    return corpus


def corpus_digest(corpus: dict[str, Any]) -> str:
    """Digest of the corpus content, excluding the stored digest field itself."""
    return sim_corpus_digest({key: value for key, value in corpus.items() if key != "digest"})


def load_or_build(path: Path, **kwargs: Any) -> dict[str, Any]:
    """Load a corpus file, verifying its recorded digest when present."""
    if not path.is_file():
        if not kwargs:
            raise FileNotFoundError(
                f"no corpus at {path}; generate one with `python -m bench.scorecard.cli corpus`"
            )
        corpus = build_corpus(**kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(corpus, sort_keys=True), encoding="utf-8")
        return corpus
    corpus = json.loads(path.read_text(encoding="utf-8"))
    recorded = corpus.get("digest")
    actual = corpus_digest(corpus)
    if recorded and recorded != actual:
        raise ValueError(
            f"corpus digest mismatch for {path}: recorded {recorded}, computed {actual}. "
            "The labels changed; regenerate instead of trusting mixed data."
        )
    corpus["digest"] = actual
    return corpus
