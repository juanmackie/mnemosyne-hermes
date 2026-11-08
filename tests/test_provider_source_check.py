"""Proposal-only source checking: synthetic evidence, no model or live data."""

import copy
import hashlib
import json
import pathlib
import sys
from argparse import ArgumentParser
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

from hermes_memory_provider import cli  # noqa: E402
from hermes_memory_provider.evidence import EvidenceError  # noqa: E402
from hermes_memory_provider.source_check import (  # noqa: E402
    check_memory,
    read_transcript,
    validate_transcript,
)


def _memory(content="The exit is west."):
    return {"id": "note", "memory_store": "working", "content": content, "metadata": {}}


def _bundle():
    return {
        "session_id": "own",
        "message_id_namespace": "internal",
        "messages": [{"id": "7", "role": "user", "content": "The exit is east."}],
    }


def _judgment(verdict="contradicted", **overrides):
    return {
        "verdict": verdict,
        "reason": "The source explicitly states east.",
        "replacement": "The exit is east." if verdict == "contradicted" else None,
        "citations": [{"message_id": "7", "quote": "The exit is east."}],
        **overrides,
    }


def _complete(result):
    def complete(prompt, **kwargs):
        assert "UNTRUSTED INPUT JSON:" in prompt
        assert kwargs == {"max_tokens": 3000, "temperature": 0.0, "timeout": 60.0}
        return json.dumps(result)

    return complete


def test_contradiction_is_a_cited_proposal_and_inputs_stay_unchanged():
    memory, bundle = _memory(), _bundle()
    before = copy.deepcopy((memory, bundle))
    report = check_memory(memory, bundle, complete=_complete(_judgment()))
    assert report["replacement"] == "The exit is east."
    assert report["citations"] == [{"message_id": "7", "quote": "The exit is east."}]
    assert report["original_sha256"] == hashlib.sha256(memory["content"].encode()).hexdigest()
    assert report["source_association"] == "operator_selected"
    assert report["source_authenticated"] is False
    assert report["applied"] is False
    assert (memory, bundle) == before


@pytest.mark.parametrize("verdict", ["supported", "insufficient"])
def test_correct_and_uncertain_memories_have_no_edit(verdict):
    result = _judgment(
        verdict, citations=[] if verdict == "insufficient" else _judgment()["citations"]
    )
    report = check_memory(_memory("The exit is east."), _bundle(), complete=_complete(result))
    assert report["replacement"] is None
    assert report["applied"] is False
    with pytest.raises(EvidenceError, match="Only a contradicted"):
        check_memory(_memory(), _bundle(), complete=_complete({**result, "replacement": "altered"}))


@pytest.mark.parametrize(
    "changes",
    [
        {"citations": []},
        {"citations": [{"message_id": "wrong", "quote": "The exit is east."}]},
        {"citations": [{"message_id": "7", "quote": "The exit is north."}]},
        {"citations": [{"message_id": "7", "quote": " "}]},
        {"citations": [{"message_id": [], "quote": "east"}]},
        {"citations": [{"message_id": "7", "quote": "east", "command": "delete"}]},
        {"verdict": "verified"},
        {"replacement": None},
        {"replacement": "The exit is west."},
        {"reason": ""},
        {"reason": "x" * 2001},
        {"apply": True},
    ],
)
def test_invalid_judgments_cannot_produce_proposals(changes):
    with pytest.raises(EvidenceError):
        check_memory(_memory(), _bundle(), complete=_complete(_judgment(**changes)))


@pytest.mark.parametrize(
    "response",
    [
        None,
        "",
        "not JSON",
        "```json\n{}\n```",
        "x" * 12001,
        '{"verdict":"supported","verdict":"contradicted"}',
    ],
)
def test_model_failure_and_malformed_output_fail_loudly(response):
    with pytest.raises(EvidenceError):
        check_memory(_memory(), _bundle(), complete=lambda *args, **kwargs: response)


def test_model_exception_is_sanitized_and_never_falls_back():
    def broken(*args, **kwargs):
        raise RuntimeError("sensitive upstream details")

    with pytest.raises(EvidenceError, match="^Hermes source checker failed; no proposal produced$"):
        check_memory(_memory(), _bundle(), complete=broken)


@pytest.mark.parametrize(
    "reference",
    [
        {"session_id": "other", "message_id": "7", "message_id_namespace": "internal"},
        {"session_id": "own", "message_id": "missing", "message_id_namespace": "internal"},
        {"session_id": "own", "message_id": "7", "message_id_namespace": "platform"},
        {"session_id": "own", "message_id": "7"},
    ],
)
def test_reference_mismatch_or_legacy_namespace_prevents_model_call(reference):
    memory = _memory()
    memory["metadata"] = {"source_ref": reference}
    with pytest.raises(EvidenceError, match="does not match"):
        check_memory(
            memory, _bundle(), complete=lambda *args, **kwargs: pytest.fail("model called")
        )


def test_stored_reference_match_remains_unauthenticated():
    memory = _memory()
    memory["metadata"] = {
        "source_ref": {
            "session_id": "own",
            "message_id": "7",
            "message_id_namespace": "internal",
            "origin": "host",
        }
    }
    report = check_memory(memory, _bundle(), complete=_complete(_judgment()))
    assert report["source_association"] == "stored_reference_match"
    assert report["source_authenticated"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"content_truncated": True},
        {"metadata_error": "invalid_json_object"},
        {"metadata_truncated": True},
        {"content": ""},
        {"content": "x" * 4001},
    ],
)
def test_incomplete_memory_prevents_model_call(changes):
    with pytest.raises(EvidenceError):
        check_memory(
            {**_memory(), **changes},
            _bundle(),
            complete=lambda *args, **kwargs: pytest.fail("model called"),
        )


@pytest.mark.parametrize(
    "modify",
    [
        lambda bundle: bundle.update(session_id=""),
        lambda bundle: bundle.update(message_id_namespace="unknown"),
        lambda bundle: bundle.update(messages=[]),
        lambda bundle: bundle["messages"].append(bundle["messages"][0]),
        lambda bundle: bundle["messages"][0].update(role="system"),
        lambda bundle: bundle["messages"][0].update(content="x" * 4001),
        lambda bundle: bundle.update(
            messages=[{"id": str(i), "role": "user", "content": "x" * 4000} for i in range(7)]
        ),
        lambda bundle: bundle.update(
            messages=[{"id": str(i), "role": "user", "content": "x"} for i in range(21)]
        ),
        lambda bundle: bundle.update(extra="ignored evidence"),
    ],
)
def test_invalid_evidence_is_rejected_before_any_model_call(modify):
    bundle = _bundle()
    modify(bundle)
    with pytest.raises(EvidenceError):
        check_memory(
            _memory(), bundle, complete=lambda *args, **kwargs: pytest.fail("model called")
        )


def test_transcript_file_bounds_and_duplicate_keys(tmp_path):
    path = tmp_path / "excerpt.json"
    for content in (b"x" * 100001, b"\xff", b'{"messages":[],"messages":[]}', b"[]"):
        path.write_bytes(content)
        with pytest.raises(EvidenceError):
            read_transcript(path)
        assert path.read_bytes() == content
    path.write_text(json.dumps(_bundle()))
    assert read_transcript(path) == _bundle()
    validate_transcript(_bundle())


def test_cli_uses_one_visible_target_and_does_not_initialize_engine(tmp_path, capsys):
    # Use the existing fixture schema; both files remain byte-identical.
    from test_provider_evidence import _database, _remember

    database = tmp_path / "memory.db"
    connection = _database(database)
    _remember(connection, "note", content="The exit is west.")
    _remember(connection, "foreign", session="other")
    connection.close()
    evidence = tmp_path / "excerpt.json"
    evidence.write_text(json.dumps(_bundle()))
    before = database.read_bytes(), evidence.read_bytes()
    parser = ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(
        ["review", "--id", "note", "--evidence", str(evidence), "--session-id", "own"]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch.dict(sys.modules, {"mnemosyne.core.beam": None}),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ) as model,
    ):
        assert cli.mnemosyne_command(args) == 0
        assert json.loads(capsys.readouterr().out)["applied"] is False
        assert model.call_count == 1
        args.memory_id = "foreign"
        with pytest.raises(SystemExit, match="1"):
            cli.mnemosyne_command(args)
        assert model.call_count == 1
        args.evidence = None
        with pytest.raises(SystemExit, match="1"):
            cli.mnemosyne_command(args)
        assert model.call_count == 1
    assert (database.read_bytes(), evidence.read_bytes()) == before


def test_synthetic_evaluator_counts_correct_changes_and_false_proposals():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "source_check_eval", ROOT / "bench/source_check_eval.py"
    )
    assert spec is not None and spec.loader is not None
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    cases = json.loads(evaluator.CASES.read_text())["cases"]
    assert len(cases) == 8
    responses = []
    for case in cases:
        messages = case["transcript"]["messages"]
        verdict = case["expected_verdict"]
        responses.append(
            {
                "verdict": verdict,
                "reason": "Scripted judgment for scorer regression only.",
                "replacement": " ".join(case["replacement_all"])
                if verdict == "contradicted"
                else None,
                "citations": [{"message_id": messages[-1]["id"], "quote": messages[-1]["content"]}]
                if verdict != "insufficient"
                else [],
            }
        )
    response_iter = iter(responses)
    report = evaluator.evaluate(
        cases, complete=lambda *args, **kwargs: json.dumps(next(response_iter))
    )
    assert report["passed"] == 8
    assert report["corrections_passed"] == 2
    assert report["correct_memory_false_proposals"] == 0
    assert report["insufficient_false_proposals"] == 0

    # Valid exact quotes alone do not prove entailment. The independent authored
    # labels catch a judge that proposes a wrong edit despite citing real text.
    report = evaluator.evaluate(
        [cases[2]],
        complete=_complete(
            {
                "verdict": "contradicted",
                "reason": "Deliberately wrong judgment.",
                "replacement": "The cave exit is west.",
                "citations": [{"message_id": "1", "quote": "The cave exit is east."}],
            }
        ),
    )
    assert report["correct_memory_false_proposals"] == 1
    assert report["passed"] == 0
