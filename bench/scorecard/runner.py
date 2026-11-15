"""Run the MnemoSim scorecard through the real provider path.

This is the instrument. Everything else in this package grades or aggregates
what this module records.

What "real provider path" means here, precisely:

* a fresh provider instance per synthetic life, initialized through
  ``MnemosyneMemoryProvider.initialize()`` against its own database;
* ingestion through the provider's own capture path, not direct engine calls:
  ``sync_turn`` for conversation and ``handle_tool_call("mnemosyne_remember")``
  for explicit writes (which is the only path that can carry ``valid_until``);
* measurement through the two surfaces an agent actually sees: ``prefetch()``
  (what is injected automatically) and ``handle_tool_call("mnemosyne_recall")``
  (what an explicit lookup returns);
* embeddings left ON. ``MNEMOSYNE_EMBEDDINGS_OFF`` is scrubbed from the
  environment rather than merely unset, so an operator machine with that flag
  exported cannot silently measure the keyword-only path.

Every probe records a per-candidate score dump (``--dump-candidates``), which is
what the fix work reads: rank, tier, source path, the score components the
engine reports, whether the row was kept, and — derived from the row text, not
from engine internals — whether the probe's distinctive entity token appears in
that row at all.

Report attestation: engine distribution and version, the SHA-256 of every engine
file the patch manifest covers (so the arm is provably the pinned patched
engine, or provably not), the provider source tree hash, the corpus digest, and
the environment knobs that could move a score.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
MANIFEST = ROOT / "integrations" / "engine-patches" / "manifest.json"

# Knobs the scorecard scrubs before measuring. Each one changes ranking,
# candidate generation or selection, so an arm that inherits one is not
# comparable. Arm-specific settings are applied explicitly afterwards.
SCRUBBED_PREFIXES = ("MNEMOSYNE_", "HERMES_")
# ... except these, which describe where to write, not how to rank.
KEPT_KNOBS = {
    "MNEMOSYNE_DATA_DIR",
    "MNEMOSYNE_DB_PATH",
    "HERMES_HOME",
    "MNEMOSYNE_FASTEMBED_CACHE_DIR",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provider_tree_hashes() -> dict[str, str]:
    package = PROVIDER_ROOT / "hermes_memory_provider"
    return {
        path.relative_to(package).as_posix(): _sha256_file(path)
        for path in sorted(package.rglob("*.py"))
    }


def engine_attestation() -> dict[str, Any]:
    """Distribution version plus the digest of every manifest-covered file.

    A mismatch is reported, never repaired: an arm that does not run the pinned
    patched engine has to be visible in the report, not silently "fixed".
    """
    attestation: dict[str, Any] = {"distribution": None, "version": None, "files": {}}
    try:
        distribution = importlib.metadata.distribution("mnemosyne-memory")
    except importlib.metadata.PackageNotFoundError:
        attestation["error"] = "mnemosyne-memory is not installed"
        return attestation
    attestation["distribution"] = distribution.metadata["Name"]
    attestation["version"] = distribution.version
    try:
        package_root = Path(str(distribution.locate_file("mnemosyne"))).resolve(strict=True)
    except OSError as error:
        attestation["error"] = f"cannot resolve the engine package: {error}"
        return attestation
    attestation["package_root"] = str(package_root)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    attestation["manifest_match_expected"] = {
        "distribution": manifest["distribution"],
        "version": manifest["version"],
    }
    for relative, entry in sorted(manifest.get("files", {}).items()):
        path = package_root / relative
        if not path.is_file():
            attestation["files"][relative] = {"state": "missing"}
            continue
        actual = _sha256_file(path)
        if actual == entry["patched_sha256"]:
            state = "patched"
        elif actual == entry["original_sha256"]:
            state = "original"
        elif actual in entry.get("previous_patched_sha256", []):
            state = "previous_patch"
        else:
            state = "unknown"
        attestation["files"][relative] = {"state": state, "sha256": actual}
    attestation["all_patched"] = all(
        row.get("state") == "patched" for row in attestation["files"].values()
    )
    return attestation


def activate_engine_path(sys_path: str | None) -> None:
    """Make `sys_path` the tree the engine is imported from, or fail loudly.

    Subprocesses (and any in-process arm that already touched the engine) can
    hold the real engine in `sys.modules`. Putting a path first on `sys.path` is
    not enough: an already-imported `mnemosyne` keeps winning. So the engine
    modules are dropped before the overlay is imported, and the resolved file is
    checked afterwards — measuring the wrong tree must be an error, never a
    silently different number.
    """
    if not sys_path:
        return
    resolved_root = Path(sys_path).resolve()
    current = sys.modules.get("mnemosyne")
    current_file = getattr(current, "__file__", None)
    already_right = bool(current_file) and Path(current_file).resolve().is_relative_to(
        resolved_root
    )
    if not already_right:
        for name in [n for n in sys.modules if n == "mnemosyne" or n.startswith("mnemosyne.")]:
            del sys.modules[name]
    if sys_path in sys.path:
        sys.path.remove(sys_path)
    sys.path.insert(0, sys_path)
    import mnemosyne as engine_module

    resolved = Path(engine_module.__file__).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise RuntimeError(
            f"engine overlay did not take effect: resolved {resolved}, expected under {resolved_root}"
        )


def clean_environment(scratch: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A measurement environment no operator shell setting can influence."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(SCRUBBED_PREFIXES) or key in KEPT_KNOBS
    }
    environment.update(
        {
            "HERMES_HOME": str(scratch / "hermes"),
            "MNEMOSYNE_DATA_DIR": str(scratch / "engine"),
            "MNEMOSYNE_AUTO_SLEEP_ENABLED": "0",
            "MNEMOSYNE_PREFETCH_CACHE_ENABLED": "0",
            # One embedding thread per worker. onnxruntime otherwise takes
            # os.cpu_count() threads in *every* process: 12 workers x 16 threads
            # on this host put the load average at 181 and starved the run. The
            # corpus is measured one life at a time per worker, so a single
            # thread is the correct amount of intra-op parallelism (workers
            # already use every core).
            "MNEMOSYNE_EMBEDDING_THREADS": str(environment.get("MNEMOSYNE_EMBEDDING_THREADS", "1")),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    # Embeddings ON: remove the opt-out rather than set its inverse, because the
    # engine treats any non-empty value as "off" for several of these aliases.
    for name in (
        "MNEMOSYNE_EMBEDDINGS_OFF",
        "MNEMOSYNE_FASTEMBED_OFF",
        "MNEMOSYNE_DISABLE_EMBEDDINGS",
        "MNEMOSYNE_EMBEDDINGS_DISABLED",
    ):
        environment.pop(name, None)
    if extra:
        environment.update(extra)
    return environment


class LifeStore:
    """One provider instance backed by one throwaway database."""

    def __init__(self, provider: Any, session_id: str) -> None:
        self.provider = provider
        self.session_id = session_id

    @classmethod
    def create(cls, scratch: Path, life_id: str, *, tool_exposure: list[str] | None = None):
        # Imported lazily: importing the provider is what pulls the engine in.
        import hermes_memory_provider as provider_module

        provider = provider_module.MnemosyneMemoryProvider()
        provider._init_audit_log = lambda: None
        provider._read_config_key = lambda _key: None
        provider.initialize(
            session_id=life_id,
            hermes_home=str(scratch / "hermes"),
            agent_context="primary",
            db_path=str(scratch / "life.db"),
            auto_sleep=False,
            shared_surface_path=str(scratch / "surface.db"),
        )
        if provider._beam is None:
            raise RuntimeError(f"provider initialization failed: {provider._init_error}")
        _ = tool_exposure
        return cls(provider, life_id)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            beam = self.provider._beam
            memory = self.provider._memory
            self.provider.shutdown()
            if memory is not None:
                memory.close()
            if beam is not None:
                beam.conn.close()
        for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
            local = getattr(sys.modules.get(name), "_thread_local", None)
            if local is not None:
                local.conn = None

    # --- ingestion ---------------------------------------------------------

    def ingest_turn(self, message: str) -> None:
        self.provider.sync_turn(message, "", session_id=self.session_id)

    def ingest_tool(
        self, content: str, *, importance: float = 0.6, valid_until: str | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "content": content,
            "importance": importance,
            "source": "user",
            "scope": "session",
        }
        if valid_until:
            payload["valid_until"] = valid_until
        self.provider.handle_tool_call("mnemosyne_remember", payload)

    # --- measurement -------------------------------------------------------

    def recall(self, query: str, *, limit: int, query_time: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if query_time:
            payload["query_time"] = query_time
        raw = self.provider.handle_tool_call("mnemosyne_recall", payload)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": raw}

    def recall_explain(self, query: str, *, limit: int) -> dict[str, Any]:
        raw = self.provider.handle_tool_call(
            "mnemosyne_recall", {"query": query, "limit": limit, "explain": True}
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": raw}

    def prefetch(self, query: str) -> str:
        return self.provider.prefetch(query, session_id=self.session_id)


def _entity_visible(text: str, entity: str) -> bool:
    """Whether the probe's distinctive entity token appears in a row."""
    return bool(entity) and entity.lower() in text.lower()


def candidate_dump(
    probe: dict[str, Any], explained: dict[str, Any], kept_texts: list[str]
) -> list[dict[str, Any]]:
    """Per-candidate rows for one probe, with the signal the fix work needs.

    Every row carries ``probe_id``/``entity``/``kind``, so a dump can be read,
    filtered and joined without relying on positional grouping: a dump that
    requires knowing "the summary row precedes its candidates" is a trap for
    whoever analyses it next.

    The engine exposes ids only for kept rows, so this records both: the score
    components for every candidate (up to the engine's cap) and, for kept rows,
    whether the probe's entity token is present at all.
    """
    explain = explained.get("explain") or {}
    candidates = explain.get("candidates") or []
    entity = probe.get("entity") or ""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        preview = candidate.get("content_preview") or ""
        row = {
            "probe_id": probe["probe_id"],
            "life_id": probe.get("life_id"),
            "kind": probe["kind"],
            "entity": entity,
            "attribute": probe.get("attribute"),
            "answers": probe.get("answers") or [],
            "rank": candidate.get("rank"),
            "tier": candidate.get("tier"),
            "source_path": candidate.get("source_path"),
            "kept": candidate.get("kept"),
            "drop_reason": candidate.get("drop_reason"),
            "scores": candidate.get("scores") or {},
            "content_preview": preview,
        }
        # Derived from the row text itself, never from engine internals, so the
        # dump is evidence about what was retrieved rather than what the engine
        # believes it retrieved.
        row["entity_in_content"] = _entity_visible(preview, entity)
        row["answer_in_content"] = any(
            _entity_visible(preview, answer) for answer in (probe.get("answers") or [])
        )
        row["stale_in_content"] = any(
            _entity_visible(preview, value) for value in (probe.get("stale_values") or [])
        )
        rows.append(row)
    rows.append(
        {
            "summary": {
                "probe_id": probe["probe_id"],
                "life_id": probe.get("life_id"),
                "kind": probe["kind"],
                "entity": entity,
                "attribute": probe.get("attribute"),
                "candidate_count": len(candidates),
                "kept_count": len(kept_texts),
                "entity_in_any_candidate": any(row.get("entity_in_content") for row in rows),
                "engine_truncation": explain.get("truncation"),
                "embedding": explain.get("embedding"),
                "weights": explain.get("weights"),
                "stages": explain.get("stages"),
            }
        }
    )
    return rows


def run_arm(
    corpus: dict[str, Any],
    *,
    arm: str,
    environment: dict[str, str] | None = None,
    ingest: str = "conversation",
    limit: int = 10,
    dump_candidates: bool = True,
    dump_every: int = 40,
    probe_limit: int | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    """Measure one arm over the corpus and return the raw per-probe records."""
    from .grade import grade, grade_prefetch

    lives = corpus["lives"]
    probes_by_life: dict[str, list[dict[str, Any]]] = {}
    for probe in corpus["probes"]:
        probes_by_life.setdefault(probe["life_id"], []).append(probe)

    records: list[dict[str, Any]] = []
    dumps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    started = time.perf_counter()
    measured = 0

    for life_index, life in enumerate(lives):
        life_probes = probes_by_life.get(life["life_id"], [])
        if probe_limit is not None and measured >= probe_limit:
            break
        with tempfile.TemporaryDirectory(prefix=f"mnemosyne-scorecard-{arm}-") as folder:
            scratch = Path(folder)
            resolved = clean_environment(scratch, environment)
            previous = dict(os.environ)
            os.environ.clear()
            os.environ.update(resolved)
            store = None
            try:
                store = LifeStore.create(scratch, life["life_id"])
                if ingest == "conversation":
                    for session in life["sessions"]:
                        for message in session["messages"]:
                            store.ingest_turn(message)
                else:
                    for event in life["events"]:
                        if event["disposition"] != "store":
                            continue
                        store.ingest_tool(
                            event["sentence"],
                            valid_until=event["valid_to"]
                            if event.get("valid_to_stated_in_text")
                            else None,
                        )

                for probe_index, probe in enumerate(life_probes):
                    if probe_limit is not None and measured >= probe_limit:
                        break
                    measured += 1
                    try:
                        payload = store.recall(
                            probe["query"], limit=limit, query_time=probe["as_of"]
                        )
                        results = payload.get("results") or []
                        # Only string rows are graded; a malformed payload is an
                        # error, not an empty answer (fail loud).
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        texts = [str(row.get("content") or "") for row in results]
                        tool_grade = grade(probe, texts)
                        tool_grade["surface"] = "tool"
                        records.append(tool_grade)

                        context = store.prefetch(probe["query"])
                        prefetch_grade = grade_prefetch(probe, context)
                        prefetch_grade["surface"] = "prefetch"
                        records.append(prefetch_grade)

                        want_dump = dump_candidates and (
                            probe_index % max(dump_every, 1) == 0 or probe["kind"] == "fabricated"
                        )
                        if want_dump:
                            explained = store.recall_explain(probe["query"], limit=limit)
                            dumps.extend(
                                {"arm": arm, **row}
                                for row in candidate_dump(probe, explained, texts)
                            )
                    except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                        errors.append(
                            {
                                "arm": arm,
                                "probe_id": probe["probe_id"],
                                "life_id": life["life_id"],
                                "error": f"{type(error).__name__}: {error}",
                                "traceback": traceback.format_exc(limit=4),
                            }
                        )
            except Exception as error:  # noqa: BLE001 - a life that cannot be built is data
                errors.append(
                    {
                        "arm": arm,
                        "life_id": life["life_id"],
                        "error": f"{type(error).__name__}: {error}",
                        "traceback": traceback.format_exc(limit=4),
                    }
                )
            finally:
                if store is not None:
                    store.close()
                os.environ.clear()
                os.environ.update(previous)
            if progress and (life_index + 1) % 25 == 0:
                elapsed = time.perf_counter() - started
                print(
                    f"  [{arm}] {life_index + 1}/{len(lives)} lives, "
                    f"{len(records) // 2} probes, {elapsed:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )

    wall = time.perf_counter() - started
    return {
        "arm": arm,
        "probes_measured": len({row["probe_id"] for row in records if row["surface"] == "tool"}),
        "records": records,
        "candidate_dumps": dumps,
        "errors": errors,
        "wall_seconds": round(wall, 3),
        "measured_probes": measured,
    }


def store_texts(db_path: Path) -> list[str]:
    """Read every stored utterance from a completed run's database, read-only."""
    if not db_path.is_file():
        return []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        texts: list[str] = []
        for table, column in (("working_memory", "content"), ("episodic_memory", "content")):
            try:
                rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            texts.extend(str(row[0]) for row in rows if row[0])
        return texts
    finally:
        connection.close()


def secrets_in_texts(corpus: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """How many planted secrets appear verbatim in stored text."""
    haystack = "\n".join(texts).lower()
    stored: list[str] = []
    for life in corpus["lives"]:
        for secret in life["secrets"]:
            if secret["text"].lower() in haystack:
                stored.append(secret["event_id"])
    return {
        "planted": sum(len(life["secrets"]) for life in corpus["lives"]),
        "stored": len(stored),
        "stored_event_ids": stored,
    }
