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
import json
import os
import pathlib
import sys
import tempfile

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
        from mnemosyne.core.beam import BeamMemory  # noqa: F401

        import mnemosyne.core.beam as beam_mod  # noqa: F401

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
            )
        print(message)
        return None


def _init_contract_provider(tmp, session_id="contract_primary", **kwargs):
    """Build a real engine-backed provider with the full tool surface open.

    Mirrors the ``_init`` pattern from tests/test_provider_db_path.py (throwaway
    MNEMOSYNE_DATA_DIR, stubbed audit log) but does NOT patch ``_get_beam_class``:
    the real BeamMemory is constructed. ``_read_config_key`` is overridden so
    ``tools`` resolves to ``["*"]``; every other key falls through to the real
    implementation. ``MNEMOSYNE_EMBEDDINGS_OFF=1`` keeps recall on the
    keyword path so the lane is fast and network-independent.
    """
    BeamMemory = _require_engine()
    assert BeamMemory is not None, "engine guard must have failed first"
    provider = provider_mod.MnemosyneMemoryProvider()
    original_data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
    original_embeddings_off = os.environ.get("MNEMOSYNE_EMBEDDINGS_OFF")
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "_engine_data")
    os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
    provider.__dict__["_init_audit_log"] = lambda: None
    real_read_config = provider._read_config_key

    def _open_all_tools(key):
        if key == "tools":
            return ["*"]
        return real_read_config(key)

    provider.__dict__["_read_config_key"] = _open_all_tools
    try:
        init_kwargs = {"agent_context": "primary", **kwargs}
        provider.initialize(session_id=session_id, hermes_home=str(tmp), **init_kwargs)
    finally:
        provider.__dict__.pop("_init_audit_log", None)
        provider.__dict__.pop("_read_config_key", None)
        if original_data_dir is None:
            os.environ.pop("MNEMOSYNE_DATA_DIR", None)
        else:
            os.environ["MNEMOSYNE_DATA_DIR"] = original_data_dir
        if original_embeddings_off is None:
            os.environ.pop("MNEMOSYNE_EMBEDDINGS_OFF", None)
        else:
            os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = original_embeddings_off
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
        raise AssertionError(f"{tool_name} returned non-JSON: {raw[:300]!r}")
    assert isinstance(payload, dict), f"{tool_name} returned non-object JSON: {raw[:300]!r}"
    return payload


def _assert_answered(tool_name, payload, raw_hint=""):
    """Every tool must answer without the hollow-provider or crash shapes."""
    status = payload.get("status")
    assert status != "memory_unavailable", (
        f"{tool_name} returned memory_unavailable (hollow provider): "
        f"{payload} {raw_hint}"
    )
    err = payload.get("error", "")
    assert "Mnemosyne unavailable" not in str(err), (
        f"{tool_name} returned an unavailable error: {payload}"
    )
    assert "Mnemosyne tool" not in str(err) or "failed" not in str(err), (
        f"{tool_name} raised an unhandled exception through the dispatcher: {payload}"
    )


def _remember_id(provider, content, scope="session", **extra):
    payload = _call(
        provider, "mnemosyne_remember", {"content": content, "scope": scope, **extra}
    )
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
    with tempfile.TemporaryDirectory() as tmp:
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

    fresh = lambda tag: _remember_id(provider, f"contract check-B {tag}")
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
    with tempfile.TemporaryDirectory() as tmp:
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
            if graph_unavailable_ok and name in (
                "mnemosyne_graph_query",
                "mnemosyne_graph_link",
            ):
                if payload.get("error") == "Episodic graph not available":
                    continue
            assert "error" not in payload, f"{name} returned an error: {payload}"
            assert payload.get("status") != "error", (
                f"{name} returned status=error: {payload}"
            )


# ---------------------------------------------------------------------------
# Check C — ID visibility matrix
# ---------------------------------------------------------------------------

# Known divergences: asserted strictly so they fail loudly when fixed (the fix
# then removes the entry instead of silently changing behaviour).
#
# 1. forget/episodic: the engine's forget_working is working-only, while get
#    falls back to episodic_memory. Found by the P20 investigation; whether to
#    patch deletion is a product decision, so the lane records it.
KNOWN_DIVERGENCES = {
    ("episodic", "forget"): (
        "engine forget_working deletes from working_memory only; "
        "mnemosyne_get resolves episodic rows"
    ),
}


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
        try:
            other.close()
        except Exception:
            pass


def _consolidate_to_episodic(provider, memory_id):
    """Move one working row to episodic memory through the real engine.

    Prefers the engine's own consolidation path (sleep with force); falls back
    to no-op detection via the episodic table so the test names the missing
    capability instead of silently passing.
    """
    payload = _call(provider, "mnemosyne_sleep", {"force": True})
    _assert_answered("mnemosyne_sleep(consolidate)", payload)
    conn = provider._beam.conn
    row = conn.execute(
        "SELECT id FROM episodic_memory WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is not None:
        return memory_id
    # Force consolidation did not move this row (e.g. LLM-gated summarization
    # is unavailable with embeddings off). Sleep with all_sessions as a second
    # real-engine attempt before giving up with a loud failure.
    payload = _call(provider, "mnemosyne_sleep", {"force": True, "all_sessions": True})
    _assert_answered("mnemosyne_sleep(consolidate all)", payload)
    row = conn.execute(
        "SELECT id FROM episodic_memory WHERE id = ?", (memory_id,)
    ).fetchone()
    assert row is not None, (
        "could not consolidate a working row to episodic_memory via "
        "mnemosyne_sleep force (tried current and all sessions). "
        "The visibility matrix needs a real episodic row."
    )
    return memory_id


def _visibility_matrix_smoke(provider):
    """Smoke that each visibility class can be built; returns one id per class.

    Runs against throwaway DBs where needed so the global sleep used for the
    episodic probe cannot consolidate the working rows other probes need.
    Only used as a setup assertion; the per-tool matrix below builds fresh
    isolated rows per operation.
    """
    own = _remember_id(provider, "contract visibility own-session")
    glob = _other_session_id(provider, "contract visibility global-other", "global")
    priv = _other_session_id(provider, "contract visibility private-other", "session")
    shared_row = _call(
        provider, "mnemosyne_shared_remember", {"content": "contract visibility shared"}
    )
    _assert_answered("mnemosyne_shared_remember(visibility)", shared_row)
    return {"own": own, "global-other": glob, "private-other": priv}


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
    tools = [
        "mnemosyne_get",
        "mnemosyne_update",
        "mnemosyne_invalidate",
        "mnemosyne_validate",
        "mnemosyne_forget",
    ]
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

    def _expect(label, tool):
        if label in ("own", "global-other"):
            return True
        if label in ("private-other", "shared-surface"):
            return False
        # episodic
        if (label, tool.split("_", 1)[1]) in KNOWN_DIVERGENCES:
            return False
        return True

    # Working/global/private/shared rows share one DB: no sleep runs there, so
    # no consolidation can move rows between classes mid-matrix.
    with tempfile.TemporaryDirectory() as tmp:
        provider = _init_contract_provider(tmp)
        smoke = _visibility_matrix_smoke(provider)
        assert all(smoke.values()), f"visibility setup produced no ids: {smoke}"

        for label in ("own", "global-other", "private-other", "shared-surface"):
            for tool in tools:
                if label == "own":
                    memory_id = _remember_id(
                        provider, f"contract matrix own for {tool}"
                    )
                elif label == "global-other":
                    memory_id = _other_session_id(
                        provider, f"contract matrix global for {tool}", "global"
                    )
                elif label == "private-other":
                    memory_id = _other_session_id(
                        provider, f"contract matrix private for {tool}", "session"
                    )
                else:  # shared-surface: private tools must all miss these rows.
                    row = _call(
                        provider,
                        "mnemosyne_shared_remember",
                        {"content": f"contract matrix shared for {tool}"},
                    )
                    memory_id = row["memory_id"]
                payload = _probe(provider, tool, memory_id)
                found = _is_found(tool, payload)
                expect = _expect(label, tool)
                if found != expect:
                    reason = KNOWN_DIVERGENCES.get((label, tool.split("_", 1)[1]), "")
                    failures.append(
                        f"{tool} on {label}: expected "
                        f"{'found' if expect else 'not_found'} got {payload} {reason}"
                    )

    # Episodic rows live in an isolated DB: mnemosyne_sleep force consolidates
    # every unconsolidated working row, so it must never run in the DB that
    # holds the working-visibility probes above.
    with tempfile.TemporaryDirectory() as tmp:
        provider = _init_contract_provider(tmp)
        for tool in tools:
            source = _remember_id(provider, f"contract matrix episodic for {tool}")
            memory_id = _consolidate_to_episodic(provider, source)
            payload = _probe(provider, tool, memory_id)
            found = _is_found(tool, payload)
            expect = _expect("episodic", tool)
            if found != expect:
                reason = KNOWN_DIVERGENCES.get(("episodic", tool.split("_", 1)[1]), "")
                failures.append(
                    f"{tool} on episodic: expected "
                    f"{'found' if expect else 'not_found'} got {payload} {reason}"
                )

    assert not failures, (
        "ID visibility matrix diverged:\n  - " + "\n  - ".join(failures)
    )


# ---------------------------------------------------------------------------
# Check D — write → read coherence
# ---------------------------------------------------------------------------


def test_write_read_coherence():
    if _require_engine() is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        provider = _init_contract_provider(tmp)
        token = "coherence kraken-01 xyzzy"
        memory_id = _remember_id(provider, f"contract {token} original")

        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "ok", f"get missed a fresh remember: {got}"
        assert token in got["memory"].get("content", ""), got["memory"]

        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        _assert_answered("mnemosyne_recall(coherence)", recalled)
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id in ids, (
            f"recall did not return the remembered row (ids={ids}): {recalled}"
        )

        updated = _call(
            provider,
            "mnemosyne_update",
            {"memory_id": memory_id, "content": f"contract {token} edited"},
        )
        _assert_answered("mnemosyne_update(coherence)", updated)
        assert updated.get("status") == "updated", updated
        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "ok" and "edited" in got["memory"].get(
            "content", ""
        ), got

        invalidated = _call(provider, "mnemosyne_invalidate", {"memory_id": memory_id})
        _assert_answered("mnemosyne_invalidate(coherence)", invalidated)
        assert invalidated.get("status") == "invalidated", invalidated

        forgotten = _call(provider, "mnemosyne_forget", {"memory_id": memory_id})
        _assert_answered("mnemosyne_forget(coherence)", forgotten)
        assert forgotten.get("status") == "deleted", forgotten
        got = _call(provider, "mnemosyne_get", {"memory_id": memory_id})
        assert got.get("status") == "not_found", (
            f"get still resolves a forgotten row: {got}"
        )
        recalled = _call(provider, "mnemosyne_recall", {"query": token, "limit": 10})
        ids = [r.get("id") for r in recalled.get("results", [])]
        assert memory_id not in ids, (
            f"recall still returns a forgotten row: {recalled}"
        )


if __name__ == "__main__":
    tests = [
        test_engine_api_surface_matches_snapshot,
        test_every_tool_answers,
        test_id_visibility_matrix,
        test_write_read_coherence,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
