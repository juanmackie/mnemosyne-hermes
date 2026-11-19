"""Safety of the optional non-destructive episodic degradation guard."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_engine_home(monkeypatch, tmp_path):
    """Keep the engine's config out of the real ``~/.hermes``.

    Some recall paths construct the engine's ``Config``, which auto-seeds
    ``$HERMES_HOME/mnemosyne/config.yaml``. That seed writes a comment
    containing an em dash through the platform encoding, so on Windows the
    file lands as cp1252 while every reader here opens it as UTF-8. Seeding
    into ``tmp_path`` keeps the run hermetic and leaves no file behind that a
    later test would fail to decode.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("MNEMOSYNE_DATA_DIR", raising=False)


@pytest.mark.parametrize("tier", [1, 2])
def test_degradation_guard_keeps_original_when_compression_loses_text(monkeypatch, tmp_path, tier):
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        pytest.skip("requires audited engine")
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "1")
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(session_id="guard", db_path=str(tmp_path / "guard.db"), seed_config=False)
    try:
        source = (
            "Archival detail about the storage capacity. " * 30 + "The exact limit is 987654321."
        )
        memory_id = memory.consolidate_to_episodic(
            summary=source, source_wm_ids=[], source="guard", scope="session"
        )
        memory.conn.execute(
            "UPDATE episodic_memory SET tier = ?, created_at = '2000-01-01T00:00:00' WHERE id = ?",
            (tier, memory_id),
        )
        memory.conn.commit()
        result = memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row["content"] == source
        assert row["tier"] == tier
        assert result["coverage_rejected"] == 1
    finally:
        memory.conn.close()


def test_degradation_guard_allows_lossless_transition(monkeypatch, tmp_path):
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        pytest.skip("requires audited engine")
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(session_id="guard", db_path=str(tmp_path / "store.db"), seed_config=False)
    try:
        short = "The storage limit is 40 terabytes."
        short_id = memory.consolidate_to_episodic(
            summary=short, source_wm_ids=[], source="guard", scope="session"
        )
        memory.conn.execute(
            "UPDATE episodic_memory SET created_at = '2000-01-01T00:00:00' WHERE id = ?",
            (short_id,),
        )
        memory.conn.commit()
        monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "1")
        memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (short_id,)
        ).fetchone()
        assert (row["content"], row["tier"]) == (short, 2)
    finally:
        memory.conn.close()


def test_degradation_guard_is_on_by_default(monkeypatch, tmp_path):
    """With the flag unset the key-token guard still cancels a lossy step."""
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        pytest.skip("requires audited engine")
    monkeypatch.delenv("MNEMOSYNE_DEGRADE_VERIFY", raising=False)
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(
        session_id="guard-default", db_path=str(tmp_path / "default.db"), seed_config=False
    )
    try:
        content = "filler words " * 120 + "The vault code is QX-7741."
        memory_id = memory.consolidate_to_episodic(
            summary=content, source_wm_ids=[], source="guard", scope="session"
        )
        memory.conn.execute(
            "UPDATE episodic_memory SET tier = 1, created_at = '2000-01-01T00:00:00' WHERE id = ?",
            (memory_id,),
        )
        memory.conn.commit()
        result = memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row["tier"] == 1
        assert row["content"] == content
        assert result["coverage_rejected"] == 1
    finally:
        memory.conn.close()


def test_degradation_guard_explicit_opt_out_restores_legacy_rewrite(monkeypatch, tmp_path):
    """MNEMOSYNE_DEGRADE_VERIFY=0 restores the unguarded in-place rewrite."""
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        pytest.skip("requires audited engine")
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "0")
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(
        session_id="guard-off", db_path=str(tmp_path / "off.db"), seed_config=False
    )
    try:
        content = "filler words " * 120 + "The vault code is QX-7741."
        memory_id = memory.consolidate_to_episodic(
            summary=content, source_wm_ids=[], source="guard", scope="session"
        )
        memory.conn.execute(
            "UPDATE episodic_memory SET tier = 1, created_at = '2000-01-01T00:00:00' WHERE id = ?",
            (memory_id,),
        )
        memory.conn.commit()
        result = memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row["tier"] == 2
        assert row["content"] != content
        assert "coverage_rejected" not in result
    finally:
        memory.conn.close()


def _engine_or_skip():
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory, _degrade_key_tokens
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        pytest.skip("requires audited engine")
    return local_llm, BeamMemory, _degrade_key_tokens


def _aged_episodic(memory, content, tier):
    memory_id = memory.consolidate_to_episodic(
        summary=content, source_wm_ids=[], source="guard", scope="session"
    )
    memory.conn.execute(
        "UPDATE episodic_memory SET tier = ?, created_at = '2000-01-01T00:00:00' WHERE id = ?",
        (tier, memory_id),
    )
    memory.conn.commit()
    return memory_id


def test_degradation_guard_allows_step_that_keeps_every_key_token(monkeypatch, tmp_path):
    """Prose lost in truncation is not a key-token loss, so the step proceeds."""
    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "1")
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(session_id="guard", db_path=str(tmp_path / "keep.db"), seed_config=False)
    try:
        content = "The limit is 987654321 and the owner is Alice. " + "filler words " * 120
        memory_id = _aged_episodic(memory, content, 1)
        result = memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row["tier"] == 2
        assert row["content"] != content
        assert "987654321" in row["content"] and "Alice" in row["content"]
        assert result["coverage_rejected"] == 0
    finally:
        memory.conn.close()


def test_degradation_guard_rejects_tier3_step_that_drops_a_key_token(monkeypatch, tmp_path):
    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "1")
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    memory = BeamMemory(session_id="guard", db_path=str(tmp_path / "drop.db"), seed_config=False)
    try:
        content = "filler words " * 40 + "The vault code is QX-7741."
        memory_id = _aged_episodic(memory, content, 2)
        result = memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row["tier"] == 2
        assert row["content"] == content
        assert result["coverage_rejected"] == 1
    finally:
        memory.conn.close()


def test_key_tokens_are_digit_or_capital_tokens_without_trailing_punctuation():
    _, _, key_tokens = _engine_or_skip()
    assert key_tokens("Value 987. and lower words, Alice's v1.2.3") == {
        "value",
        "987",
        "alice",
        "v1.2.3",
    }


def test_additive_degradation_keeps_original_and_writes_lower_tier_copy(monkeypatch, tmp_path):
    import json

    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_ADDITIVE", "1")
    memory = BeamMemory(session_id="additive", db_path=str(tmp_path / "add.db"), seed_config=False)
    try:
        content = "The limit is 987654321 and the owner is Alice. " + "filler words " * 120
        original = _aged_episodic(memory, content, 1)
        stored = memory.conn.execute(
            "SELECT content FROM episodic_memory WHERE id = ?", (original,)
        ).fetchone()[0]
        result = memory.degrade_episodic()
        assert result["mode"] == "additive"
        assert result["tier1_to_tier2"] == 1
        row = memory.conn.execute(
            "SELECT content, tier, degraded_at FROM episodic_memory WHERE id = ?", (original,)
        ).fetchone()
        assert row["content"] == stored
        assert row["tier"] == 1
        assert row["degraded_at"]
        copy = memory.conn.execute(
            "SELECT id, content, tier, degraded_at, metadata_json FROM episodic_memory WHERE tier = 2"
        ).fetchone()
        assert copy["content"] != content
        assert "987654321" in copy["content"] and "Alice" in copy["content"]
        assert copy["degraded_at"] is None
        assert json.loads(copy["metadata_json"])["degraded_from"] == original
    finally:
        memory.conn.close()


def test_additive_short_unchanged_row_only_marks_parent(monkeypatch, tmp_path):
    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_ADDITIVE", "1")
    memory = BeamMemory(
        session_id="additive", db_path=str(tmp_path / "short.db"), seed_config=False
    )
    try:
        short = "The storage limit is 40 terabytes."
        original = _aged_episodic(memory, short, 1)
        memory.degrade_episodic()
        rows = memory.conn.execute(
            "SELECT id, content, tier, degraded_at FROM episodic_memory"
        ).fetchall()
        assert len(rows) == 1
        assert (rows[0]["id"], rows[0]["content"], rows[0]["tier"]) == (original, short, 1)
        assert rows[0]["degraded_at"]
    finally:
        memory.conn.close()


def test_additive_chain_reaches_tier3_and_is_idempotent(monkeypatch, tmp_path):
    import json

    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_ADDITIVE", "1")
    memory = BeamMemory(
        session_id="additive", db_path=str(tmp_path / "chain.db"), seed_config=False
    )
    try:
        content = "The limit is 987654321 and the owner is Alice. " + "filler words " * 120
        original = _aged_episodic(memory, content, 1)
        stored = memory.conn.execute(
            "SELECT content FROM episodic_memory WHERE id = ?", (original,)
        ).fetchone()[0]
        memory.degrade_episodic()
        tier2 = memory.conn.execute("SELECT id FROM episodic_memory WHERE tier = 2").fetchone()[
            "id"
        ]
        memory.degrade_episodic()
        tier3 = memory.conn.execute(
            "SELECT id, metadata_json FROM episodic_memory WHERE tier = 3"
        ).fetchone()
        assert json.loads(tier3["metadata_json"])["degraded_from"] == tier2
        assert memory.conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0] == 3
        memory.degrade_episodic()
        assert memory.conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0] == 3
        assert (
            memory.conn.execute(
                "SELECT content FROM episodic_memory WHERE id = ?", (original,)
            ).fetchone()[0]
            == stored
        )
    finally:
        memory.conn.close()


def test_additive_guard_rejects_step_without_writing_anything(monkeypatch, tmp_path):
    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_ADDITIVE", "1")
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_VERIFY", "1")
    memory = BeamMemory(
        session_id="additive", db_path=str(tmp_path / "guard.db"), seed_config=False
    )
    try:
        content = "filler words " * 40 + "The vault code is QX-7741."
        original = _aged_episodic(memory, content, 2)
        result = memory.degrade_episodic()
        assert result["coverage_rejected"] == 1
        rows = memory.conn.execute("SELECT id, tier, degraded_at FROM episodic_memory").fetchall()
        assert [(r["id"], r["tier"], r["degraded_at"]) for r in rows] == [(original, 2, None)]
    finally:
        memory.conn.close()


def test_additive_lineage_collapses_to_the_original_at_recall(monkeypatch, tmp_path):
    local_llm, BeamMemory, _ = _engine_or_skip()
    monkeypatch.setattr(local_llm, "llm_available", lambda: False)
    monkeypatch.setenv("MNEMOSYNE_DEGRADE_ADDITIVE", "1")
    memory = BeamMemory(
        session_id="additive", db_path=str(tmp_path / "recall.db"), seed_config=False
    )
    try:
        content = "The limit is 987654321 and the owner is Alice. " + "filler words " * 120
        original = _aged_episodic(memory, content, 1)
        memory.degrade_episodic()
        memory.degrade_episodic()
        assert memory.conn.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0] == 3
        rows = memory.recall("owner Alice limit 987654321", top_k=10)
        assert [r["id"] for r in rows] == [original]
    finally:
        memory.conn.close()
