"""Real-engine regression for opt-in recall score-ratio selection.

The test reconstructs an isolated copy from the audited engine originals,
applies the repository patches, and runs BEAM in a subprocess. It never writes
to the installed Hermes package or a user's memory store.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_ROOT = ROOT / "integrations" / "engine-patches"
BACKUP_SUFFIX = ".mnemosyne-hermes-original"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _engine_package_root() -> pathlib.Path | None:
    explicit = os.environ.get("MNEMOSYNE_TEST_ENGINE_ROOT")
    if explicit:
        root = pathlib.Path(explicit).resolve()
        return root if (root / "core" / "beam.py").is_file() else None
    spec = importlib.util.find_spec("mnemosyne")
    locations = spec.submodule_search_locations if spec else None
    if not locations:
        return None
    root = pathlib.Path(next(iter(locations))).resolve()
    return root if (root / "core" / "beam.py").is_file() else None


def _reconstruct_engine_overlay(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    """Copy audited originals into a temporary package and apply repo patches."""
    manifest = json.loads((PATCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    overlay_root = destination / "overlay"
    package = overlay_root / "mnemosyne"
    shutil.copytree(
        source,
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    for relative, entry in manifest["files"].items():
        original_path = source / relative
        backup = original_path.with_name(original_path.name + BACKUP_SUFFIX)
        if backup.is_file():
            original = backup.read_bytes()
            if _digest(original) != entry["original_sha256"]:
                raise AssertionError(f"audited original backup differs: {relative}")
        else:
            original = original_path.read_bytes()
            if _digest(original) != entry["original_sha256"]:
                raise AssertionError(
                    f"cannot reconstruct audited engine original for {relative}; "
                    "set MNEMOSYNE_TEST_ENGINE_ROOT to a package with verified backups"
                )
        (package / relative).write_bytes(original)

    spec = importlib.util.spec_from_file_location(
        "engine_patch_applier", ROOT / "scripts" / "apply_engine_patches.py"
    )
    assert spec is not None and spec.loader is not None
    applier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(applier)
    applier.apply_patches(package)
    return overlay_root


def _run_real_recall_regression() -> None:
    source = _engine_package_root()
    if source is None:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("real-engine recall precision test requires mnemosyne-memory")
        print("skip: mnemosyne-memory is not importable")
        return

    child = r"""
import os, pathlib, sys
overlay, scratch = map(pathlib.Path, sys.argv[1:3])
sys.path.insert(0, str(overlay))
os.environ["MNEMOSYNE_DATA_DIR"] = str(scratch / "data")
os.environ["HERMES_HOME"] = str(scratch / "hermes")
os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
os.environ["MNEMOSYNE_AUTO_MIGRATE"] = "0"
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"] = "0"
os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR", None)

import mnemosyne.core.beam as beam_module
from mnemosyne.core.beam import BeamMemory, _apply_recall_score_floor

# The option uses an inclusive relative boundary, preserves ties at ratio 1,
# and treats invalid or disabled values as no filter.
sample = [{"score": 0.8}, {"score": 0.4}, {"score": 0.1}]
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "0.5"
assert _apply_recall_score_floor(sample) == sample[:2]
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "1"
assert _apply_recall_score_floor([{"score": 0.8}, {"score": 0.8}, {"score": 0.4}]) == [
    {"score": 0.8}, {"score": 0.8}
]
for invalid in ("bad", "nan", "-0.1", "1.01"):
    os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = invalid
    assert _apply_recall_score_floor(sample) == sample, invalid
assert _apply_recall_score_floor([]) == []
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "0.5"
assert _apply_recall_score_floor([{"score": 0.0}, {"score": -0.1}]) == [
    {"score": 0.0}, {"score": -0.1}
]
os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR")

db_path = scratch / "recall.db"
owner = BeamMemory(session_id="owner", db_path=db_path, seed_config=False)
expected_working = owner.remember(
    "The Kubernetes deployment uses GitHub Actions for the production service.",
    importance=0.5,
    scope="session",
)
expected_episodic = owner.consolidate_to_episodic(
    "Kubernetes deployment uses ArgoCD to roll back a failed release.",
    [],
    source="precision-regression",
    importance=0.5,
)
weak_match = owner.remember(
    "The unrelated demo deployment schedule is weekly.",
    importance=0.5,
    scope="session",
)
other_session = BeamMemory(session_id="other", db_path=db_path, seed_config=False)
foreign = other_session.remember(
    "Kubernetes deployment with GitHub Actions and ArgoCD belongs to another session.",
    importance=1.0,
    scope="session",
)

query = "Kubernetes deployment GitHub Actions ArgoCD"
baseline = owner.recall(query, top_k=10, _cross_session=False)
baseline_ids = [row["id"] for row in baseline]
assert expected_working in baseline_ids, baseline
assert expected_episodic in baseline_ids, baseline
assert weak_match in baseline_ids, baseline
assert foreign not in baseline_ids, baseline
assert baseline[0]["score"] > baseline[-1]["score"], baseline
assert baseline[-1]["score"] / baseline[0]["score"] < 0.6, baseline

# Enhanced recall has its own opaque cache; a changed floor must miss it.
from mnemosyne.core.config import resolve_beam_runtime
def enhanced_key():
    return owner._enhanced_recall_cache_key(
        original_query=query,
        expanded_query=query,
        top_k=10,
        runtime=resolve_beam_runtime(),
        use_weibull=False,
        use_mmr=False,
        use_intent=False,
        use_synonyms=False,
        use_associative=False,
        associative_depth=1,
        mmr_lambda=0.7,
        recall_kwargs={"_cross_session": False},
    )
key_without_floor = enhanced_key()
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "0.6"
key_with_floor = enhanced_key()
assert key_without_floor != key_with_floor

# The opt-in relative floor retains both high-scoring, different-tier answers,
# removes the weak lexical neighbor, and leaves the authorized session boundary
# intact. It filters the already-ranked list and does not reorder it.
selected = owner.recall(query, top_k=10, _cross_session=False)
selected_ids = [row["id"] for row in selected]
assert set(selected_ids) == {expected_working, expected_episodic}, selected
assert [row["id"] for row in selected] == [
    row["id"] for row in baseline if row["id"] in set(selected_ids)
], selected
assert {row["tier"] for row in selected} == {"working", "episodic"}, selected
assert foreign not in selected_ids, selected
assert owner.conn.execute(
    "SELECT recall_count FROM working_memory WHERE id = ?", (expected_working,)
).fetchone()[0] == 2
assert owner.conn.execute(
    "SELECT recall_count FROM episodic_memory WHERE id = ?", (expected_episodic,)
).fetchone()[0] == 2
assert owner.conn.execute(
    "SELECT recall_count FROM working_memory WHERE id = ?", (weak_match,)
).fetchone()[0] == 1

# Enhanced recall applies the floor after its optional reranking stage too.
os.environ["MNEMOSYNE_ENHANCED_RECALL"] = "1"
enhanced_selected = owner.recall_enhanced(
    query,
    top_k=10,
    use_cache=False,
    use_weibull=False,
    use_mmr=False,
    use_intent=False,
    use_synonyms=False,
    use_associative=False,
)
assert {row["id"] for row in enhanced_selected} == {
    expected_working,
    expected_episodic,
}, enhanced_selected

# A low base-score candidate must reach enhanced scoring before the floor is
# applied. The deterministic Weibull boost lifts it within the configured
# ratio; early filtering in base recall would discard it before this stage.
from unittest.mock import patch
with patch.object(beam_module, "weibull_boost", return_value=1.0):
    boosted = owner.recall_enhanced(
        query,
        top_k=10,
        use_cache=False,
        use_weibull=True,
        use_mmr=False,
        use_intent=False,
        use_synonyms=False,
        use_associative=False,
    )
assert {row["id"] for row in boosted} == {
    expected_working,
    expected_episodic,
    weak_match,
}, boosted
boosted_scores = {row["id"]: row["score"] for row in boosted}
assert boosted_scores[weak_match] >= max(boosted_scores.values()) * 0.6, boosted
os.environ["MNEMOSYNE_ENHANCED_RECALL"] = "0"

# Polyphonic recall has a separate scoring return path. It uses the same
# selector while retaining its ranked order and session filter.
os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR")
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"] = "1"
polyphonic_baseline = owner.recall(query, top_k=10, _cross_session=False)
polyphonic_ids = [row["id"] for row in polyphonic_baseline]
assert expected_working in polyphonic_ids, polyphonic_baseline
assert expected_episodic in polyphonic_ids, polyphonic_baseline
assert foreign not in polyphonic_ids, polyphonic_baseline
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "0.6"
polyphonic_selected = owner.recall(query, top_k=10, _cross_session=False)
assert {row["id"] for row in polyphonic_selected} == {
    expected_working,
    expected_episodic,
}, polyphonic_selected
assert [row["id"] for row in polyphonic_selected] == [
    row["id"]
    for row in polyphonic_baseline
], polyphonic_selected
os.environ["MNEMOSYNE_POLYPHONIC_RECALL"] = "0"

# The floor stays opt-in, and a negative query remains a real no-match.
os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR")
again = owner.recall(query, top_k=10, _cross_session=False)
assert [row["id"] for row in again] == baseline_ids, (baseline, again)
os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = "0.6"
assert owner.recall("qzxwvv no matching stored memory", top_k=10, _cross_session=False) == []

other_session.conn.close()
owner.conn.close()
print("baseline scores:", [(r["id"], r["tier"], r["score"]) for r in baseline])
print("selected ids:", selected_ids)
"""

    with tempfile.TemporaryDirectory(prefix="mnemosyne-recall-precision-") as temp:
        scratch = pathlib.Path(temp)
        overlay = _reconstruct_engine_overlay(source, scratch)
        result = subprocess.run(
            [sys.executable, "-c", child, str(overlay), str(scratch)],
            text=True,
            capture_output=True,
            timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout, end="")


def test_real_recall_score_floor_preserves_multitier_precision_and_auth() -> None:
    _run_real_recall_regression()


if __name__ == "__main__":
    test_real_recall_score_floor_preserves_multitier_precision_and_auth()
    print("\n1 real-engine recall precision check passed")
