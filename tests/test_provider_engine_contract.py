"""Engine-backed contract lane for the vendored Hermes provider.

Drives every provider tool against the pinned real engine (mnemosyne-memory)
through the public ``handle_tool_call`` entry point, so the P19 lock and the
``has_tool`` gate are covered on every call.

Needs the real engine plus the vendored provider. In a bare venv (no engine)
every test prints a skip line and returns, so ``./test-all.sh`` stays green
without the engine. Under ``MNEMOSYNE_REQUIRE_ENGINE=1`` (the
``./test-all.sh --require-engine`` lane and the ``engine-contract`` CI job) a
missing engine is a hard failure instead of a silent skip — silent skipping is
the failure mode this lane exists to remove.

Run with:

    python tests/test_provider_engine_contract.py
    pytest tests/test_provider_engine_contract.py
    MNEMOSYNE_REQUIRE_ENGINE=1 python tests/test_provider_engine_contract.py
"""

import ast
import contextlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402

REQUIRE_ENGINE = os.environ.get("MNEMOSYNE_REQUIRE_ENGINE", "0") == "1"


def _require_engine():
    """Import BeamMemory, or skip/fail per MNEMOSYNE_REQUIRE_ENGINE.

    Returns the BeamMemory class when the engine is present. When it is absent,
    prints a skip line and returns None (bare-venv lane), unless the strict
    lane is active, in which case it raises AssertionError with a clear
    install hint.
    """
    try:
        import mnemosyne.core.beam as beam_mod

        return beam_mod.BeamMemory
    except ImportError:
        message = (
            "skip: mnemosyne-memory engine not importable "
            "(install ./integrations/hermes-provider to run the contract lane; "
            "MNEMOSYNE_REQUIRE_ENGINE=1 makes this a failure)"
        )
        if REQUIRE_ENGINE:
            raise AssertionError(
                "MNEMOSYNE_REQUIRE_ENGINE=1 but the mnemosyne-memory engine is "
                "not importable. Install it with: "
                "uv pip install ./integrations/hermes-provider"
            ) from None
        print(message)
        return None


@contextlib.contextmanager
def _contract_env(tmp):
    """Hold the contract-lane environment for a whole test body.

    The engine reads ``MNEMOSYNE_EMBEDDINGS_OFF`` on every call (not just at
    init), so it must stay set while tools run: it keeps recall on the keyword
    path, fast and network-independent. ``MNEMOSYNE_DATA_DIR`` and
    ``HERMES_HOME`` point at the throwaway dir so diagnostics and pending
    writes never touch the real home directory.
    """
    keys = ("MNEMOSYNE_DATA_DIR", "MNEMOSYNE_EMBEDDINGS_OFF", "HERMES_HOME")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "_engine_data")
    os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
    os.environ["HERMES_HOME"] = tmp
    connections = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    try:
        with patch.object(sqlite3, "connect", side_effect=tracked_connect):
            yield
    finally:
        # Engine connections are thread-local; close them before Windows
        # attempts to remove the temporary database directory.
        for conn in connections:
            conn.close()
        for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
            module = sys.modules.get(name)
            local = getattr(module, "_thread_local", None)
            if local is not None:
                local.conn = None
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _stub_hermes_constants():
    """Provide the ``hermes_constants.get_hermes_home`` helper without Hermes.

    The strict CI jobs install only the provider, the engine and pytest, so
    the ``hermes_constants`` package (shipped with hermes-agent) is absent and
    ``mnemosyne_apply_pending`` would fail with ``No module named
    'hermes_constants'``. The stub answers from ``HERMES_HOME`` (which
    ``_contract_env`` points at the throwaway dir), keeping the lane hermetic.
    Restores any pre-existing module afterwards so pytest siblings are
    unaffected.
    """
    import types

    mod = types.ModuleType("hermes_constants")

    def get_hermes_home():
        return pathlib.Path(os.environ.get("HERMES_HOME", ""))

    mod.get_hermes_home = get_hermes_home
    saved = sys.modules.get("hermes_constants")
    sys.modules["hermes_constants"] = mod
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("hermes_constants", None)
        else:
            sys.modules["hermes_constants"] = saved


def _init_contract_provider(tmp, session_id="contract_primary", **kwargs):
    """Build a real engine-backed provider with the full tool surface open.

    Mirrors the ``_init`` pattern from tests/test_provider_db_path.py (stubbed
    audit log) but does NOT patch ``_get_beam_class``: the real BeamMemory is
    constructed. ``_read_config_key`` is overridden for the provider's
    lifetime so ``tools`` keeps resolving to ``["*"]`` — ``has_tool`` re-reads
    it on every call, so removing the override after init would silently close
    the surface back to the four core tools. Every other key falls through to
    the real implementation. The shared surface is pointed inside the
    throwaway dir so no state persists in the checkout. Environment
    (``MNEMOSYNE_DATA_DIR``, ``MNEMOSYNE_EMBEDDINGS_OFF``, ``HERMES_HOME``) is
    owned by ``_contract_env``, which must wrap the whole test body.
    """
    BeamMemory = _require_engine()
    assert BeamMemory is not None, "engine guard must have failed first"
    provider = provider_mod.MnemosyneMemoryProvider()
    provider.__dict__["_init_audit_log"] = lambda: None
    real_read_config = provider._read_config_key

    def _open_all_tools(key):
        if key == "tools":
            return ["*"]
        return real_read_config(key)

    provider.__dict__["_read_config_key"] = _open_all_tools
    try:
        init_kwargs = {
            "agent_context": "primary",
            "shared_surface_path": os.path.join(tmp, "shared", "mnemosyne.db"),
            **kwargs,
        }
        provider.initialize(session_id=session_id, hermes_home=str(tmp), **init_kwargs)
    finally:
        provider.__dict__.pop("_init_audit_log", None)
    assert provider._beam is not None, (
        f"provider failed to initialize against the real engine: {provider._init_error!r}"
    )
    # Full surface must actually be open, otherwise Check B cannot cover it.
    assert provider.has_tool("mnemosyne_remember"), "core tool missing after init"
    assert provider.has_tool("mnemosyne_sync_status"), (
        "full surface not open: _read_config_key override did not yield ['*']"
    )
    return provider


def _call(provider, tool_name, args):
    """Call through the public entry point so the P19 lock applies."""
    assert provider.has_tool(tool_name), f"tool not exposed: {tool_name}"
    raw = provider.handle_tool_call(tool_name, dict(args))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise AssertionError(f"{tool_name} returned non-JSON: {raw[:300]!r}") from None
    assert isinstance(payload, dict), f"{tool_name} returned non-object JSON: {raw[:300]!r}"
    return payload


def _assert_answered(tool_name, payload, raw_hint=""):
    """Every tool must answer without the hollow-provider or crash shapes."""
    status = payload.get("status")
    assert status != "memory_unavailable", (
        f"{tool_name} returned memory_unavailable (hollow provider): {payload} {raw_hint}"
    )
    err = payload.get("error", "")
    assert "Mnemosyne unavailable" not in str(err), (
        f"{tool_name} returned an unavailable error: {payload}"
    )
    assert "Mnemosyne tool" not in str(err) or "failed" not in str(err), (
        f"{tool_name} raised an unhandled exception through the dispatcher: {payload}"
    )


def _remember_id(provider, content, scope="session", **extra):
    payload = _call(provider, "mnemosyne_remember", {"content": content, "scope": scope, **extra})
    _assert_answered("mnemosyne_remember", payload)
    assert payload.get("status") == "stored", f"remember failed: {payload}"
    memory_id = payload.get("memory_id")
    assert memory_id, f"remember returned no id: {payload}"
    return memory_id


# ---------------------------------------------------------------------------
# Check A — engine API surface (static, no hand-maintained list)
# ---------------------------------------------------------------------------


def _provider_beam_attrs():
    """Every self._beam.<attr> / self._surface_beam.<attr> in the snapshot."""
    package = PROVIDER_ROOT / "hermes_memory_provider" / "__init__.py"
    tree = ast.parse(package.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr in ("_beam", "_surface_beam")
        ):
            found.add((owner.attr, node.attr))
    assert found, "AST scan found no self._beam attribute uses"
    return found


def test_engine_api_surface_matches_snapshot():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        provider = _init_contract_provider(tmp)
        # Surface beam is lazy; force it so its attribute set is checkable.
        _call(provider, "mnemosyne_shared_remember", {"content": "surface probe"})
        assert provider._surface_beam is not None, "shared surface beam did not init"
        missing = []
        for owner, attr in sorted(_provider_beam_attrs()):
            target = provider._beam if owner == "_beam" else provider._surface_beam
            if not hasattr(target, attr):
                missing.append(f"{owner}.{attr}")
        assert not missing, (
            "provider uses engine attributes missing from this engine release "
            f"(pin bump or rename): {missing}. "
            "Re-run the contract audit before widening the pin."
        )


# ---------------------------------------------------------------------------
# Check B — every tool answers
# ---------------------------------------------------------------------------


def _check_b_args(provider, tmp, seed):
    """Minimal valid args for every name in ALL_TOOL_SCHEMAS.

    Prerequisites (seed memories, export file, persona row) are created with
    the real tools so the IDs are valid against the live engine. Callers must
    keep this table exactly in sync with ALL_TOOL_SCHEMAS: the coverage
    assertion below fails when a tool is added upstream and skipped here.
    """
    export_path = os.path.join(tmp, "contract-export.json")
    plotted = _call(provider, "mnemosyne_export", {"output_path": export_path})
    _assert_answered("mnemosyne_export(setup)", plotted)
    assert os.path.exists(export_path), f"setup export wrote nothing: {plotted}"

    def fresh(tag):
        return _remember_id(provider, f"contract check-B {tag}")

    shared_throwaway = _call(
        provider, "mnemosyne_shared_remember", {"content": "contract check-B shared"}
    )
    _assert_answered("mnemosyne_shared_remember(setup)", shared_throwaway)
    shared_throwaway_id = shared_throwaway["memory_id"]
    canonical_tag = "contract_b_tmp"
    _call(
        provider,
        "mnemosyne_remember_canonical",
        {"category": canonical_tag, "name": "probe", "body": "contract body"},
    )
    triple_subject = "contract_b_subject"
    _call(
        provider,
        "mnemosyne_triple_add",
        {
            "subject": triple_subject,
            "predicate": "contract_p",
            "object": "contract_b_object",
        },
    )
    persona_seed = fresh("persona source")
    promoted = _call(
        provider,
        "mnemosyne_persona_promote",
        {"memory_id": persona_seed, "tier": "working", "reason": "contract probe"},
    )
    _assert_answered("mnemosyne_persona_promote(setup)", promoted)
    persona_id = promoted.get("persona_id")
    assert persona_id is not None, f"setup promote returned no persona: {promoted}"

    graph_seed_a = seed
    graph_seed_b = fresh("graph target")

    def _demote_args():
        mid = fresh("demote source")
        row = _call(
            provider,
            "mnemosyne_persona_promote",
            {"memory_id": mid, "tier": "working", "reason": "contract demote setup"},
        )
        return {"persona_id": row["persona_id"], "reason": "contract probe"}

    def _reinforce_args():
        mid = fresh("reinforce source")
        row = _call(
            provider,
            "mnemosyne_persona_promote",
            {"memory_id": mid, "tier": "working", "reason": "contract reinforce setup"},
        )
        return {"persona_id": row["persona_id"]}

    return {
        # Core memory CRUD.
        "mnemosyne_remember": {"content": "contract probe memory"},
        "mnemosyne_recall": {"query": "contract probe", "limit": 5},
        "mnemosyne_shared_remember": {"content": "contract shared answer"},
        "mnemosyne_shared_recall": {"query": "contract shared", "limit": 5},
        "mnemosyne_shared_forget": {"memory_id": shared_throwaway_id},
        "mnemosyne_shared_stats": {},
        # Sleep needs no LLM when dry-run: report only, no consolidation.
        "mnemosyne_sleep": {"dry_run": True},
        "mnemosyne_stats": {},
        "mnemosyne_invalidate": {"memory_id": fresh("invalidate target")},
        "mnemosyne_validate": {
            "memory_id": fresh("validate target"),
            "action": "attest",
        },
        "mnemosyne_get": {"memory_id": seed},
        "mnemosyne_triple_add": {
            "subject": "contract_b_s2",
            "predicate": "contract_p2",
            "object": "contract_b_o2",
        },
        "mnemosyne_triple_query": {"subject": triple_subject},
        "mnemosyne_triple_end": {
            "subject": triple_subject,
            "predicate": "contract_p",
        },
        "mnemosyne_remember_canonical": {
            "category": "contract_b",
            "name": "answer",
            "body": "contract canonical body",
        },
        "mnemosyne_recall_canonical": {"category": "contract_b", "name": "answer"},
        "mnemosyne_forget_canonical": {"category": canonical_tag, "name": "probe"},
        "mnemosyne_apply_pending": {"pending_ids": []},
        "mnemosyne_model_card": {"category": "contract_b"},
        # Diagnostic-only action: never mutates, never needs an LLM.
        "mnemosyne_model_refresh": {"action": "list"},
        "mnemosyne_scratchpad_write": {"content": "contract scratch"},
        "mnemosyne_scratchpad_read": {},
        "mnemosyne_scratchpad_clear": {},
        "mnemosyne_export": {"output_path": os.path.join(tmp, "contract-export-b.json")},
        "mnemosyne_update": {
            "memory_id": fresh("update target"),
            "content": "contract updated content",
        },
        "mnemosyne_forget": {"memory_id": fresh("forget target")},
        "mnemosyne_batch": {
            "operations": [{"action": "remember", "content": "contract batch probe"}],
            "dry_run": True,
        },
        # File round-trip: the setup export above is the import source, so no
        # external fixture is needed.
        "mnemosyne_import": {"input_path": export_path},
        "mnemosyne_diagnose": {},
        "mnemosyne_recall_diagnostics": {},
        "mnemosyne_task_progress": {"action": "list"},
        "mnemosyne_graph_query": {"seed_memory_id": graph_seed_a},
        "mnemosyne_graph_link": {
            "source_id": graph_seed_a,
            "target_id": graph_seed_b,
            "relationship": "related_to",
        },
        # Sync without a remote answers with the documented contract error;
        # status answers ok even unconfigured.
        "mnemosyne_sync_push": {},
        "mnemosyne_sync_pull": {},
        "mnemosyne_sync_status": {},
        "mnemosyne_persona_promote": {
            "memory_id": fresh("promote target"),
            "tier": "working",
            "reason": "contract probe",
        },
        "mnemosyne_persona_demote": _demote_args(),
        "mnemosyne_persona_list": {},
        "mnemosyne_persona_reinforce": _reinforce_args(),
        # Keep the setup persona row for the reinforce path above; the demote
        # and reinforce entries manage their own lifecycle rows.
        "_setup_persona_id": persona_id,
    }


def test_every_tool_answers():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        provider = _init_contract_provider(tmp)
        seed = _remember_id(provider, "contract check-B seed memory")
        table = _check_b_args(provider, tmp, seed)
        table.pop("_setup_persona_id", None)

        expected = {schema["name"] for schema in provider_mod.ALL_TOOL_SCHEMAS}
        assert set(table) == expected, (
            "Check B table drifted from ALL_TOOL_SCHEMAS "
            f"(missing: {sorted(expected - set(table))}, "
            f"extra: {sorted(set(table) - expected)}). "
            "A new upstream tool must be added here, not skipped."
        )

        # Tools whose contract is "answer", even when the answer is the
        # documented unconfigured-remote error.
        json_only = {"mnemosyne_sync_push", "mnemosyne_sync_pull"}
        graph_unavailable_ok = provider._beam.episodic_graph is None

        for name in [schema["name"] for schema in provider_mod.ALL_TOOL_SCHEMAS]:
            payload = _call(provider, name, table[name])
            _assert_answered(name, payload)
            if name in json_only:
                continue
            if name == "mnemosyne_recall_diagnostics" and payload.get("status") == "disabled":
                continue
            if (
                graph_unavailable_ok
                and name in ("mnemosyne_graph_query", "mnemosyne_graph_link")
                and payload.get("error") == "Episodic graph not available"
            ):
                continue
            assert "error" not in payload, f"{name} returned an error: {payload}"
            assert payload.get("status") != "error", f"{name} returned status=error: {payload}"


# ---------------------------------------------------------------------------
# Check C — ID visibility matrix
# ---------------------------------------------------------------------------

# Known divergences: asserted strictly so they fail loudly when fixed (the fix
# then removes the entry instead of silently changing behaviour).
#
# 1. forget/episodic: the engine's forget_working is working-only, while get
#    falls back to episodic_memory. Found by the P20 investigation; whether to
#    patch deletion is a product decision, so the lane records it.
# 2. validate/episodic: mnemosyne_validate only looks in working_memory, while
#    get, update and invalidate fall back to episodic_memory. Found by the
#    first engine-backed run of this lane.
KNOWN_DIVERGENCES = {
    ("episodic", "forget"): (
        "engine forget_working deletes from working_memory only; "
        "mnemosyne_get resolves episodic rows"
    ),
    ("episodic", "validate"): (
        "mnemosyne_validate only queries working_memory; get, update and "
        "invalidate fall back to episodic_memory"
    ),
}

# Explicit per-cell expectations: True means the tool must resolve the ID.
# After P21 (validate honours the same session/global visibility as
# get/update/invalidate/forget), every private-bank tool agrees on found vs
# not_found except the two recorded episodic divergences above.
_MATRIX_TOOLS = [
    "mnemosyne_get",
    "mnemosyne_update",
    "mnemosyne_invalidate",
    "mnemosyne_validate",
    "mnemosyne_forget",
]
_EXPECTED = {}
for _tool in _MATRIX_TOOLS:
    _EXPECTED[("own", _tool)] = True
    _EXPECTED[("global-other", _tool)] = True
    _EXPECTED[("private-other", _tool)] = False
    _EXPECTED[("shared-surface", _tool)] = False
    _EXPECTED[("episodic", _tool)] = _tool in (
        "mnemosyne_get",
        "mnemosyne_update",
        "mnemosyne_invalidate",
    )
del _tool


def _other_session_id(provider, content, scope):
    """Remember a row as another session against the same DB file."""
    beam = provider._beam
    db_path = getattr(beam, "db_path", None)
    assert db_path, "real BeamMemory has no db_path"
    from mnemosyne.core.beam import BeamMemory

    other = BeamMemory(session_id="contract_other_session", db_path=db_path)
    try:
        return other.remember(content=content, scope=scope)
    finally:
        with contextlib.suppress(Exception):
            other.close()


def _consolidate_to_episodic(provider, memory_id):
    """Write one episodic row through the real engine and return its new ID.

    Engine consolidation is additive: ``consolidate_to_episodic`` writes a new
    episodic summary row (new ID) and leaves the working source row in place,
    so this never disturbs the working-visibility probes sharing the DB.
    """
    new_id = provider._beam.consolidate_to_episodic(
        summary=f"contract episodic summary for {memory_id}",
        source_wm_ids=[memory_id],
    )
    assert isinstance(new_id, str) and new_id, f"consolidate_to_episodic returned no id: {new_id!r}"
    row = provider._beam.conn.execute(
        "SELECT id FROM episodic_memory WHERE id = ?", (new_id,)
    ).fetchone()
    assert row is not None, f"consolidated episodic row {new_id} missing from episodic_memory"
    return new_id


def _is_found(tool_name, payload):
    """True when the tool resolved the ID (found), False when it missed."""
    if tool_name == "mnemosyne_get":
        return payload.get("status") == "ok"
    if tool_name == "mnemosyne_update":
        return payload.get("status") == "updated"
    if tool_name == "mnemosyne_invalidate":
        return payload.get("status") == "invalidated"
    if tool_name == "mnemosyne_validate":
        return str(payload.get("status", "")).startswith("validation_")
    if tool_name == "mnemosyne_forget":
        return payload.get("status") == "deleted"
    raise AssertionError(f"unexpected matrix tool: {tool_name}")


def test_id_visibility_matrix():
    if _require_engine() is None:
        return
    failures = []

    def _probe(provider, tool, memory_id):
        if tool == "mnemosyne_update":
            args = {"memory_id": memory_id, "content": "contract matrix edit"}
        elif tool == "mnemosyne_validate":
            args = {"memory_id": memory_id, "action": "attest"}
        else:
            args = {"memory_id": memory_id}
        payload = _call(provider, tool, args)
        _assert_answered(f"{tool}", payload)
        return payload

    def _build(provider, label, tool):
        # Fresh rows per probe: the matrix tests visibility while Check D
        # tests lifecycle, so no probe may destroy another probe's row.
        if label == "own":
            return _remember_id(provider, f"contract matrix own for {tool}")
        if label == "global-other":
            return _other_session_id(provider, f"contract matrix global for {tool}", "global")
        if label == "private-other":
            return _other_session_id(provider, f"contract matrix private for {tool}", "session")
        if label == "episodic":
            source = _remember_id(provider, f"contract matrix episodic for {tool}")
            return _consolidate_to_episodic(provider, source)
        # shared-surface: private-bank tools must all miss these rows.
        row = _call(
            provider,
            "mnemosyne_shared_remember",
            {"content": f"contract matrix shared for {tool}"},
        )
        return row["memory_id"]

    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        provider = _init_contract_provider(tmp)
        for label in ("own", "global-other", "private-other", "episodic", "shared-surface"):
            for tool in _MATRIX_TOOLS:
                memory_id = _build(provider, label, tool)
                payload = _probe(provider, tool, memory_id)
                found = _is_found(tool, payload)
                expect = _EXPECTED[(label, tool)]
                if found != expect:
                    reason = KNOWN_DIVERGENCES.get((label, tool.split("_", 1)[1]), "")
                    failures.append(
                        f"{tool} on {label}: expected "
                        f"{'found' if expect else 'not_found'} got {payload} {reason}"
                    )

    assert not failures, "ID visibility matrix diverged:\n  - " + "\n  - ".join(failures)


# ---------------------------------------------------------------------------
# Check D — write → read coherence
# ---------------------------------------------------------------------------


def test_write_read_coherence():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        provider = _init_contract_provider(tmp)
        token = "coherence kraken-01 xyzzy"
        memory_id = _remember_id(provider, f"contract {token} original")

        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "ok", f"get missed a fresh remember: {got}"
        assert token in got["memory"].get("content", ""), got["memory"]

        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        _assert_answered("mnemosyne_recall(coherence)", recalled)
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id in ids, f"recall did not return the remembered row (ids={ids}): {recalled}"

        updated = _call(
            provider,
            "mnemosyne_update",
            {"memory_id": memory_id, "content": f"contract {token} edited"},
        )
        _assert_answered("mnemosyne_update(coherence)", updated)
        assert updated.get("status") == "updated", updated
        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "ok" and "edited" in got["memory"].get("content", ""), got
        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id in ids, f"recall lost the row after update (ids={ids}): {recalled}"

        invalidated = _call(provider, "mnemosyne_invalidate", {"memory_id": memory_id})
        _assert_answered("mnemosyne_invalidate(coherence)", invalidated)
        assert invalidated.get("status") == "invalidated", invalidated
        # Invalidate is expiry, not deletion: the row still exists for get
        # but must drop out of recall results.
        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "ok", f"get lost an invalidated (not forgotten) row: {got}"
        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id not in ids, f"recall still returns an invalidated row: {recalled}"

        forgotten = _call(provider, "mnemosyne_forget", {"memory_id": memory_id})
        _assert_answered("mnemosyne_forget(coherence)", forgotten)
        assert forgotten.get("status") == "deleted", forgotten
        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "not_found", f"get still resolves a forgotten row: {got}"
        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id not in ids, f"recall still returns a forgotten row: {recalled}"


def test_engine_global_update_and_bind_order():
    """Exercise BEAM itself: P20 cannot hide an engine update failure."""
    BeamMemory = _require_engine()
    if BeamMemory is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp):
        from mnemosyne.core import beam as beam_mod

        with patch.object(beam_mod, "_cross_session_enabled", return_value=False):
            writer = BeamMemory(session_id="writer", db_path=os.path.join(tmp, "probe.db"))
            target = writer.remember("oldtoken global target", scope="global")
            private = writer.remember("private target", scope="session")
            # A reversed ID/session bind would update this decoy instead.
            writer.conn.execute(
                "UPDATE working_memory SET id = ?, session_id = ? WHERE id = ?",
                ("reader", target, private),
            )
            writer.conn.commit()
            reader = BeamMemory(session_id="reader", db_path=writer.db_path)
            assert reader.get(target) is not None
            for fields in (
                {"content": "newtoken global target"},
                {"importance": 0.91},
                {"content": "finaltoken global target", "importance": 0.83},
            ):
                assert reader.update_working(target, **fields), (
                    "get found global ID, update missed it"
                )
                got = reader.get(target)
                for key, value in fields.items():
                    assert got[key] == value
            decoy = reader.conn.execute(
                "SELECT content FROM working_memory WHERE id = 'reader'"
            ).fetchone()
            assert decoy[0] == "private target", "misordered binds changed the decoy"
            assert not reader.update_working("reader", content="denied")
            assert not reader.update_working("missing", content="missing")
            assert not reader.update_working(target)
            assert reader.conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0] == 2
            recalled = reader.recall("finaltoken", top_k=10)
            assert any(row["id"] == target and "finaltoken" in row["content"] for row in recalled)


def test_engine_mcp_update_reports_beam_success():
    """The actual MCP handler must report BEAM edits even without a legacy row."""
    BeamMemory = _require_engine()
    if BeamMemory is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp):
        from mnemosyne import mcp_tools
        from mnemosyne.core import beam as beam_mod
        from mnemosyne.core.memory import Mnemosyne

        with patch.object(beam_mod, "_cross_session_enabled", return_value=False):
            caller = Mnemosyne(session_id="reader", db_path=os.path.join(tmp, "mcp.db"))
            writer = BeamMemory(session_id="writer", db_path=caller.db_path)
            target = writer.remember("mcpglobal old content", scope="global")
            private = writer.remember("private mcp content", scope="session")
            events = []
            with (
                patch.object(mcp_tools, "_create_instance", return_value=caller),
                patch.object(caller, "_emit_wrapper", side_effect=lambda *a, **k: events.append(a)),
            ):
                result = mcp_tools._handle_update(
                    {"memory_id": target, "content": "mcpglobal edited"}
                )
                assert result["status"] == "updated", result
                assert caller.beam.get(target)["content"] == "mcpglobal edited"
                assert caller.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
                assert len(events) == 1
                denied = mcp_tools._handle_update({"memory_id": private, "content": "denied"})
                assert denied["status"] == "not_found", denied
                assert len(events) == 1, "failed update emitted a success event"
                # Legacy content alone must not bypass BEAM visibility.
                caller.conn.execute(
                    "INSERT INTO memories (id, content, session_id) VALUES (?, ?, ?)",
                    (private, "legacy private", "reader"),
                )
                caller.conn.commit()
                assert not caller.update(private, content="denied again")
                assert (
                    caller.conn.execute(
                        "SELECT content FROM memories WHERE id = ?", (private,)
                    ).fetchone()[0]
                    == "legacy private"
                )
                assert len(events) == 1
                caller.conn.execute(
                    "INSERT INTO memories (id, content, session_id) VALUES (?, ?, ?)",
                    (target, "legacy old", "writer"),
                )
                caller.conn.commit()
                assert caller.update(target, content="mcpglobal mirrored")
                assert caller.conn.execute(
                    "SELECT content, session_id FROM memories WHERE id = ?", (target,)
                ).fetchone()[:] == ("mcpglobal mirrored", "writer")
                no_fields = mcp_tools._handle_update({"memory_id": target})
                assert no_fields.get("error") == "content or importance is required"
                # A mirror failure rolls back the BEAM edit as well.
                caller.conn.execute(
                    "CREATE TRIGGER reject_mirror BEFORE UPDATE ON memories "
                    "BEGIN SELECT RAISE(ABORT, 'mirror blocked'); END"
                )
                caller.conn.commit()
                try:
                    caller.update(target, content="must roll back")
                except sqlite3.IntegrityError:
                    pass
                else:
                    raise AssertionError("mirror failure was swallowed")
                assert caller.beam.get(target)["content"] == "mcpglobal mirrored"
                assert len(events) == 2, "rolled-back update emitted a success event"


def test_cross_session_id_tools_share_runtime_scope():
    """Both scope modes apply to BEAM ID tools and provider P20/P21."""
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        from mnemosyne.core import beam as beam_mod

        provider = _init_contract_provider(tmp)
        for enabled in (False, True):
            with patch.object(beam_mod, "_cross_session_enabled", return_value=enabled):
                for tool in _MATRIX_TOOLS:
                    target = _other_session_id(provider, f"toggle {enabled} {tool}", "session")
                    args = {"memory_id": target}
                    if tool == "mnemosyne_update":
                        args["content"] = "toggle edited"
                    if tool == "mnemosyne_validate":
                        args["action"] = "attest"
                    result = _call(provider, tool, args)
                    assert _is_found(tool, result) is enabled, (enabled, tool, result)
                # P20's episodic fallback must use the same toggle.
                target = _other_session_id(provider, f"episodic toggle {enabled}", "session")
                episodic = _consolidate_to_episodic(provider, target)
                provider._beam.conn.execute(
                    "UPDATE episodic_memory SET session_id = 'foreign', scope = 'session' WHERE id = ?",
                    (episodic,),
                )
                provider._beam.conn.commit()
                result = _call(
                    provider,
                    "mnemosyne_update",
                    {
                        "memory_id": episodic,
                        "content": "episodic toggle edited",
                    },
                )
                assert _is_found("mnemosyne_update", result) is enabled, result


def test_engine_scope_uses_one_runtime_snapshot():
    BeamMemory = _require_engine()
    if BeamMemory is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp):
        from mnemosyne.core import beam as beam_mod

        beam = BeamMemory(session_id="reader", db_path=os.path.join(tmp, "snapshot.db"))
        for operation in ("get", "update_working", "invalidate", "forget_working"):
            target = beam.remember(f"snapshot {operation}")
            with patch.object(
                beam_mod, "_cross_session_enabled", side_effect=[False, True]
            ) as toggle:
                kwargs = {"content": "snapshot edit"} if operation == "update_working" else {}
                assert getattr(beam, operation)(target, **kwargs)
                assert toggle.call_count == 1, f"{operation} sampled runtime more than once"


def test_provider_validate_actions_use_runtime_scope():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp), _stub_hermes_constants():
        from mnemosyne.core import beam as beam_mod

        provider = _init_contract_provider(tmp)
        for enabled in (False, True):
            for action in ("attest", "update", "invalidate", "delete"):
                target = _other_session_id(provider, f"validate {enabled} {action}", "session")
                args = {"memory_id": target, "action": action, "new_content": "validation edit"}
                with patch.object(
                    beam_mod, "_cross_session_enabled", side_effect=[enabled, not enabled]
                ) as toggle:
                    result = _call(provider, "mnemosyne_validate", args)
                    assert _is_found("mnemosyne_validate", result) is enabled, result
                    assert toggle.call_count == 1
                row = provider._beam.conn.execute(
                    "SELECT content, valid_until, validation_count FROM working_memory WHERE id = ?",
                    (target,),
                ).fetchone()
                if not enabled:
                    assert row[0] == f"validate {enabled} {action}"
                    assert row[1] is None and not row[2]
                elif action == "delete":
                    assert row is None
                elif action == "update":
                    assert row[0] == "validation edit"


def test_engine_mcp_batch_update_preserves_outer_transaction():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp):
        from mnemosyne import mcp_tools
        from mnemosyne.core.memory import Mnemosyne

        caller = Mnemosyne(session_id="reader", db_path=os.path.join(tmp, "batch.db"))
        target = caller.beam.remember("batch before")
        events = []
        with (
            patch.object(mcp_tools, "_create_instance", return_value=caller),
            patch.object(caller, "_emit_wrapper", side_effect=lambda *a, **k: events.append(a)),
        ):
            result = mcp_tools._handle_batch(
                {
                    "operations": [
                        {"action": "update", "memory_id": target, "content": "batch rollback"},
                        {"action": "forget", "memory_id": "missing"},
                    ]
                }
            )
            assert result["status"] == "error", result
            assert caller.beam.get(target)["content"] == "batch before"
            assert events == [], "rolled-back batch emitted wrapper events"


def test_engine_update_refreshes_vectors_and_cached_recall():
    BeamMemory = _require_engine()
    if BeamMemory is None:
        return
    with (
        tempfile.TemporaryDirectory() as tmp,
        _contract_env(tmp),
        patch.dict(os.environ, {"MNEMOSYNE_ENHANCED_RECALL": "1"}),
    ):
        from mnemosyne.core import beam as beam_mod

        writer = BeamMemory(session_id="writer", db_path=os.path.join(tmp, "cache.db"))
        target = writer.remember("cachetoken before", scope="global")
        reader = BeamMemory(session_id="reader", db_path=writer.db_path)
        options = {
            "use_weibull": False,
            "use_mmr": False,
            "use_intent": False,
            "use_synonyms": False,
        }
        before = reader.recall_enhanced("cachetoken", top_k=10, **options)
        assert any(r["id"] == target and r["content"] == "cachetoken before" for r in before)
        # Prime and inspect the actual enhanced-recall cache before editing.
        cache = reader._query_cache
        cached_key = next(iter(cache._opaque))
        assert cache.get_opaque(cached_key) is not None
        with (
            patch.object(beam_mod._embeddings, "available", return_value=True),
            patch.object(beam_mod._embeddings, "embed", return_value=[[0.5] * 384]) as embed,
        ):
            assert reader.update_working(target, content="cachetoken edited")
            assert reader.update_working(target, importance=0.85)
            assert embed.call_count == 1
        assert cache.get_opaque(cached_key) is None, "content edit retained stale cached results"
        stored = reader.conn.execute(
            "SELECT embedding_json FROM memory_embeddings WHERE memory_id = ?", (target,)
        ).fetchone()
        assert stored is not None, "global content edit did not refresh the embedding"
        assert len(json.loads(stored[0])) == 384
        after = reader.recall_enhanced("cachetoken", top_k=10, **options)
        assert any(r["id"] == target and r["content"] == "cachetoken edited" for r in after)


def test_remember_dedup_stays_session_local():
    BeamMemory = _require_engine()
    if BeamMemory is None:
        return
    with tempfile.TemporaryDirectory() as tmp, _contract_env(tmp):
        from mnemosyne.core import beam as beam_mod

        first = BeamMemory(session_id="first", db_path=os.path.join(tmp, "dedup.db"))
        second = BeamMemory(session_id="second", db_path=first.db_path)
        with patch.object(beam_mod, "_cross_session_enabled", return_value=True):
            original = first.remember("identical dedup content", scope="global", importance=0.6)
            assert first.remember("identical dedup content", importance=0.9) == original
            other = second.remember("identical dedup content", importance=0.7)
            assert other != original
            assert first.get(original)["importance"] == 0.9
            assert second.get(other)["importance"] == 0.7


if __name__ == "__main__":
    tests = [
        test_engine_api_surface_matches_snapshot,
        test_every_tool_answers,
        test_id_visibility_matrix,
        test_write_read_coherence,
        test_engine_global_update_and_bind_order,
        test_engine_mcp_update_reports_beam_success,
        test_cross_session_id_tools_share_runtime_scope,
        test_engine_scope_uses_one_runtime_snapshot,
        test_provider_validate_actions_use_runtime_scope,
        test_engine_mcp_batch_update_preserves_outer_transaction,
        test_engine_update_refreshes_vectors_and_cached_recall,
        test_remember_dedup_stays_session_local,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
