"""Engine-free coverage for P55 lesson provenance.

A stored lesson can say which model, Hermes version and engine version it was
learned under, so a recall after a model switch shows that context instead of
implying the lesson was learned under the current model. These tests pin the
three properties the change has to keep, without the engine or Hermes:

* a value the host did not report is stored as ``null``, never guessed;
* a row written before provenance existed renders exactly as it did before;
* the extra prompt text is at most one short line per injected row.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest
from hermes_memory_provider import MnemosyneMemoryProvider as Provider
from hermes_memory_provider import (
    _provenance_line,
    _stamped_metadata,
)
from hermes_memory_provider.hermes_llm_adapter import active_model_id

PROVENANCE = {
    "model": "z-ai/glm-5.3-flash",
    "hermes_version": "0.19.0",
    "engine_version": "3.15.1",
    "recorded_at": "2026-11-19T10:00:00Z",
}


class RememberBeam:
    """Records what the provider asks it to store."""

    def __init__(self) -> None:
        self.remembered: list[dict] = []
        self.rows: list[dict] = []
        self.fetched: dict[str, dict] = {}
        self.conn = sqlite3.connect(":memory:")
        self.db_path = ":memory:"
        self.session_id = "session-a"
        self.author_id = None
        self.channel_id = None
        self.canonical_owner_id = "owner-a"

    def remember(self, **kwargs: object) -> str:
        self.remembered.append(dict(kwargs))
        return f"m{len(self.remembered)}"

    def recall(self, query: str | None = None, **_kwargs: object) -> list[dict]:
        # `_handle_recall` passes the query positionally; `_prefetch_bank` as a
        # keyword. A fake that accepts only one shape measures nothing.
        return list(self.rows)

    def get(self, memory_id: str) -> dict | None:
        return self.fetched.get(memory_id)


def _provider(beam: RememberBeam | None = None) -> Provider:
    instance = Provider()
    instance._beam = beam
    instance._init_audit_log = lambda: None
    instance._agent_context = "interactive"
    instance._skip_contexts = set()
    instance._prefetch_profile = "general"
    instance._prefetch_cache_enabled = lambda: False
    instance._shared_surface_read = False
    instance._with_persona_block = lambda text: text
    return instance


def _row(content: str = "Lesson: verify before claiming completion", **extra: object) -> dict:
    row = {
        "id": "m1",
        "content": content,
        "source": "preference",
        "score": 0.9,
        "keyword_score": 0.8,
        "importance": 0.9,
        "timestamp": "2026-11-19T10:00:00",
    }
    row.update(extra)
    return row


# --- the model id is read, never guessed -----------------------------------


def test_model_id_is_none_when_the_host_does_not_report_one(monkeypatch) -> None:
    monkeypatch.delenv("MNEMOSYNE_MODEL_ID", raising=False)
    assert active_model_id() is None


def test_model_id_reads_the_hermes_runtime_helper(monkeypatch) -> None:
    monkeypatch.delenv("MNEMOSYNE_MODEL_ID", raising=False)
    module = type("module", (), {})()
    module._read_main_model = lambda: " anthropic/claude-opus-5.5 "
    monkeypatch.setitem(__import__("sys").modules, "agent.auxiliary_client", module)
    assert active_model_id() == "anthropic/claude-opus-5.5"


def test_a_declared_model_id_wins_and_unusable_values_are_dropped(monkeypatch) -> None:
    monkeypatch.setenv("MNEMOSYNE_MODEL_ID", "declared/model")
    assert active_model_id() == "declared/model"
    monkeypatch.setenv("MNEMOSYNE_MODEL_ID", "x" * 101)
    assert active_model_id() is None
    monkeypatch.setenv("MNEMOSYNE_MODEL_ID", "bad\nmodel")
    assert active_model_id() is None


# --- stamping --------------------------------------------------------------


def test_stamped_metadata_records_null_for_an_unknown_model() -> None:
    stamped = _stamped_metadata(
        {"canonical_name": "verify-before-claiming"},
        model=None,
        hermes_version=None,
        engine_version="3.15.1",
        recorded_at="2026-11-19T10:00:00Z",
    )
    assert stamped["canonical_name"] == "verify-before-claiming"
    assert stamped["provenance"] == {
        "model": None,
        "hermes_version": None,
        "engine_version": "3.15.1",
        "recorded_at": "2026-11-19T10:00:00Z",
    }


def test_malformed_metadata_is_passed_through_untouched() -> None:
    assert (
        _stamped_metadata(
            "not-an-object",
            model="m",
            hermes_version=None,
            engine_version="3.15.1",
            recorded_at="2026-11-19T10:00:00Z",
        )
        == "not-an-object"
    )


def test_stamping_does_not_mutate_the_callers_metadata() -> None:
    original = {"canonical_name": "x"}
    _stamped_metadata(
        original,
        model="m",
        hermes_version=None,
        engine_version="3.15.1",
        recorded_at="2026-11-19T10:00:00Z",
    )
    assert original == {"canonical_name": "x"}


# --- the display line ------------------------------------------------------


def test_legacy_rows_have_no_provenance_line() -> None:
    assert _provenance_line(None) is None
    assert _provenance_line({"canonical_name": "x"}) is None
    assert _provenance_line("not a dict") is None


def test_provenance_line_names_model_engine_and_date() -> None:
    assert _provenance_line({"provenance": PROVENANCE}) == (
        "learned under model z-ai/glm-5.3-flash, engine 3.15.1, hermes 0.19.0, 2026-11-19"
    )


def test_provenance_line_omits_what_is_unknown() -> None:
    line = _provenance_line({"provenance": {**PROVENANCE, "model": None, "hermes_version": None}})
    assert line == "learned under engine 3.15.1, 2026-11-19"
    assert _provenance_line({"provenance": {"model": None, "engine_version": "unknown"}}) is None


# --- the three surfaces ----------------------------------------------------


def _veracity_stub() -> Any:
    """`_handle_remember` imports the engine's clamp at call time."""
    module: Any = types.ModuleType("mnemosyne.core.veracity_consolidation")
    module.clamp_veracity = lambda value, **_kwargs: value
    return patch.dict(sys.modules, {"mnemosyne.core.veracity_consolidation": module})


def test_remember_stamps_the_model_that_produced_the_write(monkeypatch) -> None:
    monkeypatch.setenv("MNEMOSYNE_MODEL_ID", "z-ai/glm-5.3-flash")
    beam = RememberBeam()
    provider = _provider(beam)

    with _veracity_stub():
        payload = json.loads(
            provider._handle_remember({"content": "lesson", "metadata": {"canonical_name": "c"}})
        )

    assert payload["status"] == "stored"
    metadata = beam.remembered[0]["metadata"]
    assert metadata["canonical_name"] == "c"
    assert metadata["provenance"]["model"] == "z-ai/glm-5.3-flash"
    assert metadata["provenance"]["recorded_at"].endswith("Z")


def test_remember_records_a_null_model_rather_than_a_guess(monkeypatch) -> None:
    monkeypatch.delenv("MNEMOSYNE_MODEL_ID", raising=False)
    beam = RememberBeam()
    provider = _provider(beam)

    with _veracity_stub():
        provider._handle_remember({"content": "lesson"})

    assert beam.remembered[0]["metadata"]["provenance"]["model"] is None


def test_recall_adds_one_line_and_leaves_legacy_rows_identical() -> None:
    legacy = _row("legacy row", id="m1")
    tagged = _row("tagged row", id="m2", metadata={"provenance": PROVENANCE})
    beam = RememberBeam()
    beam.rows = [legacy, tagged]
    provider = _provider(beam)

    response = json.loads(provider._handle_recall({"query": "lesson", "limit": 5}))
    by_id = {row["id"]: row for row in response["results"]}

    assert "learned_under" not in by_id["m1"]
    assert by_id["m1"] == {**legacy, "bank": "private"}
    assert by_id["m2"]["learned_under"].startswith("learned under model z-ai/glm-5.3-flash")


def test_recall_reads_provenance_from_the_store_when_the_row_omits_metadata() -> None:
    beam = RememberBeam()
    beam.rows = [_row("tagged row", id="m2")]
    beam.fetched["m2"] = {"id": "m2", "metadata": json.dumps({"provenance": PROVENANCE})}
    provider = _provider(beam)

    response = json.loads(provider._handle_recall({"query": "lesson", "limit": 5}))

    assert "engine 3.15.1" in response["results"][0]["learned_under"]


def test_get_adds_the_line_and_keeps_the_legacy_payload() -> None:
    beam = RememberBeam()
    beam.fetched["m1"] = {"id": "m1", "content": "legacy"}
    provider = _provider(beam)
    assert json.loads(provider._handle_get({"memory_id": "m1"})) == {
        "status": "ok",
        "memory": {"id": "m1", "content": "legacy"},
    }

    beam.fetched["m2"] = {"id": "m2", "metadata": {"provenance": PROVENANCE}}
    payload = json.loads(provider._handle_get({"memory_id": "m2"}))
    assert payload["learned_under"].startswith("learned under model z-ai/glm-5.3-flash")


def test_prefetch_grows_by_exactly_one_line_for_a_tagged_row() -> None:
    legacy = _row("legacy row", id="m1")
    tagged = _row("tagged row", id="m2", metadata={"provenance": PROVENANCE})
    line = "    learned under model z-ai/glm-5.3-flash, engine 3.15.1, hermes 0.19.0, 2026-11-19"

    legacy_only = RememberBeam()
    legacy_only.rows = [legacy]
    without = _provider(legacy_only).prefetch("lesson", session_id="s1")

    both = RememberBeam()
    both.rows = [legacy, tagged]
    with_provenance = _provider(both).prefetch("lesson", session_id="s1")

    row = "  [2026-11-19T10:00] (importance 0.90, source preference) tagged row"
    assert with_provenance == f"{without}\n{row}\n{line}"
    assert len(with_provenance.splitlines()) - len(without.splitlines()) == 2  # row + line


def test_prefetch_block_is_unchanged_for_legacy_rows_only() -> None:
    beam = RememberBeam()
    beam.rows = [_row("User prefers concise summaries", source="preference")]
    provider = _provider(beam)

    assert provider.prefetch("summaries", session_id="s1") == (
        "## Mnemosyne Context\n"
        "  [2026-11-19T10:00] (importance 0.90, source preference) "
        "User prefers concise summaries"
    )


def test_prompt_guidance_keeps_the_lesson_shape_and_the_labels() -> None:
    beam = RememberBeam()
    provider = _provider(beam)
    provider._configured_tool_names = lambda: {"mnemosyne_remember", "mnemosyne_recall"}

    prompt = provider.system_prompt_block()

    assert "root cause, what was tried, what was expected, what happened" in prompt
    assert "failed attempts" in prompt
    assert "structured label" in prompt
    assert "not a current instruction" in prompt


@pytest.mark.parametrize("surface", ["recall", "prefetch"])
def test_no_extra_key_is_added_when_provenance_metadata_is_malformed(surface: str) -> None:
    beam = RememberBeam()
    beam.rows = [_row("row", id="m1", metadata={"provenance": "not-a-dict"})]
    provider = _provider(beam)
    if surface == "recall":
        response = json.loads(provider._handle_recall({"query": "row", "limit": 5}))
        assert "learned_under" not in response["results"][0]
    else:
        assert "learned under" not in provider.prefetch("row", session_id="s1")
