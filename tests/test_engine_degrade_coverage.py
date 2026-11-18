"""Safety of the optional non-destructive episodic degradation guard."""

import os

import pytest


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


def test_degradation_guard_allows_lossless_transition_and_default_is_unchanged(
    monkeypatch, tmp_path
):
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
        monkeypatch.delenv("MNEMOSYNE_DEGRADE_VERIFY")
        long = "Long archive content. " * 50
        long_id = memory.consolidate_to_episodic(
            summary=long, source_wm_ids=[], source="guard", scope="session"
        )
        memory.conn.execute(
            "UPDATE episodic_memory SET created_at = '2000-01-01T00:00:00' WHERE id = ?", (long_id,)
        )
        memory.conn.commit()
        memory.degrade_episodic()
        row = memory.conn.execute(
            "SELECT content, tier FROM episodic_memory WHERE id = ?", (long_id,)
        ).fetchone()
        assert row["tier"] == 2
        assert row["content"] != long
    finally:
        memory.conn.close()
