"""Explicit-correction review ledger: content-free events and measured outcomes."""

import contextlib
import json
import os
import pathlib
import sqlite3
import sys
from argparse import ArgumentParser
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

from hermes_memory_provider import cli  # noqa: E402
from hermes_memory_provider.review_ledger import (  # noqa: E402
    ReviewLedgerError,
    content_sha256,
    ledger_path,
    pending_proposals,
    read_events,
    record_outcome,
    record_review,
    summarize,
)
from hermes_memory_provider.source_check import check_memory  # noqa: E402


def _judgment(verdict="contradicted", replacement="The exit is east."):
    return {
        "verdict": verdict,
        "reason": "The source explicitly states east.",
        "replacement": replacement if verdict == "contradicted" else None,
        "citations": (
            [] if verdict == "insufficient" else [{"message_id": "7", "quote": "The exit is east."}]
        ),
    }


def _complete(result):
    return lambda *args, **kwargs: json.dumps(result)


def _report(
    *, verdict="contradicted", replacement="The exit is east.", content="The exit is west."
):
    memory = {"id": "note", "memory_store": "working", "content": content, "metadata": {}}
    bundle = {
        "session_id": "own",
        "message_id_namespace": "internal",
        "messages": [{"id": "7", "role": "user", "content": "The exit is east."}],
    }
    judgment = _judgment(verdict, replacement)
    return check_memory(memory, bundle, complete=_complete(judgment))


def _fixture(tmp_path):
    from test_provider_evidence import _database, _remember

    database = tmp_path / "memory.db"
    connection = _database(database)
    _remember(connection, "note", content="The exit is west.")
    connection.close()
    evidence = tmp_path / "excerpt.json"
    evidence.write_text(
        json.dumps(
            {
                "session_id": "own",
                "message_id_namespace": "internal",
                "messages": [{"id": "7", "role": "user", "content": "The exit is east."}],
            }
        )
    )
    return database, evidence


def _set_content(database, content):
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE working_memory SET content = ? WHERE id = 'note'", (content,))
        connection.commit()


def _parser():
    parser = ArgumentParser()
    cli.register_cli(parser)
    return parser


def test_review_event_keeps_hashes_and_verdict_but_no_memory_or_transcript_text(tmp_path):
    database = tmp_path / "memory.db"
    report = _report()
    recorded = record_review(database, report, correction_message_id="7")

    assert recorded["proposal_id"] == recorded["review_id"]
    events = read_events(ledger_path(database))
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "review"
    assert event["trigger"] == "explicit_correction"
    assert event["verdict"] == "contradicted"
    assert event["original_sha256"] == report["original_sha256"]
    assert event["evidence_sha256"] == report["evidence_sha256"]
    assert event["replacement_sha256"] == content_sha256(report["replacement"])
    assert event["session_id"] == "hermes_default"
    assert event["all_sessions"] is False
    assert event["correction_message_id"] == "7"

    raw = ledger_path(database).read_text(encoding="utf-8")
    assert "The exit is west." not in raw
    assert "The exit is east." not in raw
    assert "source explicitly states" not in raw
    assert report["replacement"] not in raw


def test_deterministic_review_ids_make_duplicate_checks_idempotent(tmp_path):
    database = tmp_path / "memory.db"
    first = record_review(database, _report())
    second = record_review(database, _report())

    assert first["review_id"] == second["review_id"]
    assert first["proposal_id"] == second["proposal_id"]
    assert len(read_events(ledger_path(database))) == 2
    stats = summarize(read_events(ledger_path(database)), ledger_path(database))
    assert stats["reviews"] == 1
    assert stats["proposals"] == 1


def test_pending_proposals_list_only_undecided(tmp_path):
    database = tmp_path / "memory.db"
    first = record_review(database, _report(replacement="The exit is east."))["proposal_id"]
    second = record_review(database, _report(replacement="The exit is north."))["proposal_id"]
    record_outcome(database, first, "accepted")

    events = read_events(ledger_path(database))
    pending = pending_proposals(events)
    assert pending["count"] == 1
    assert [row["proposal_id"] for row in pending["pending"]] == [second]
    assert pending["pending"][0]["memory_id"] == "note"
    assert pending["truncated"] is False

    record_outcome(database, second, "rejected")
    assert pending_proposals(read_events(ledger_path(database)))["count"] == 0


def test_observed_supersession_marks_the_applied_prior_proposal(tmp_path):
    database = tmp_path / "memory.db"
    prior = record_review(
        database, _report(content="The exit is west.", replacement="The exit is east.")
    )
    prior_id = prior["proposal_id"]
    newer = record_review(
        database, _report(content="The exit is east.", replacement="The exit is north.")
    )
    assert newer["superseded"] == [prior_id]

    events = read_events(ledger_path(database))
    outcomes = [event for event in events if event["event"] == "outcome"]
    assert outcomes[-1]["proposal_id"] == prior_id
    assert outcomes[-1]["outcome"] == "corrected_again"
    assert outcomes[-1]["source"] == "observed_rerun"
    assert outcomes[-1]["observed"] == "matches_proposal"

    stats = summarize(events, ledger_path(database))
    assert stats["corrected_again"] == 1
    assert stats["pending"] == 1
    assert stats["outcome_sources"]["observed_rerun"] == 1

    rerun = record_review(
        database, _report(content="The exit is east.", replacement="The exit is north.")
    )
    assert rerun["superseded"] == []
    assert (
        summarize(read_events(ledger_path(database)), ledger_path(database))["corrected_again"] == 1
    )


def test_supersession_requires_the_exact_prior_replacement(tmp_path):
    database = tmp_path / "memory.db"
    record_review(database, _report(content="The exit is west.", replacement="The exit is east."))
    unrelated = record_review(
        database, _report(content="The exit is west.", replacement="The exit is north.")
    )
    assert unrelated["superseded"] == []
    stats = summarize(read_events(ledger_path(database)), ledger_path(database))
    assert stats["corrected_again"] == 0
    assert stats["pending"] == 2


def test_accepted_rejected_and_corrected_again_rates(tmp_path):
    database = tmp_path / "memory.db"
    proposals = [
        record_review(database, _report(replacement=f"The exit is {direction}."))["proposal_id"]
        for direction in ("east", "north", "south", "up")
    ]
    record_outcome(database, proposals[0], "accepted")
    record_outcome(database, proposals[1], "rejected")
    record_outcome(database, proposals[2], "corrected_again")

    stats = summarize(read_events(ledger_path(database)), ledger_path(database))
    assert stats["proposals"] == 4
    assert stats["pending"] == 1
    assert stats["decided"] == 3
    assert stats["accepted"] == 1
    assert stats["rejected"] == 1
    assert stats["corrected_again"] == 1
    assert stats["acceptance_rate"] == 0.3333
    assert stats["false_correction_rate"] == 0.6667


def test_latest_outcome_wins_after_a_later_correction(tmp_path):
    database = tmp_path / "memory.db"
    proposal = record_review(database, _report())["proposal_id"]
    record_outcome(database, proposal, "accepted")
    record_outcome(database, proposal, "corrected_again")

    stats = summarize(read_events(ledger_path(database)), ledger_path(database))
    assert stats["decided"] == 1
    assert stats["accepted"] == 0
    assert stats["corrected_again"] == 1
    assert stats["false_correction_rate"] == 1.0


def test_supported_and_insufficient_reviews_are_counted_without_proposals(tmp_path):
    database = tmp_path / "memory.db"
    assert record_review(database, _report(verdict="supported"))["proposal_id"] is None
    assert record_review(database, _report(verdict="insufficient"))["proposal_id"] is None

    stats = summarize(read_events(ledger_path(database)), ledger_path(database))
    assert stats["reviews"] == 2
    assert stats["proposals"] == 0
    assert stats["verdicts"] == {"supported": 1, "contradicted": 0, "insufficient": 1}
    assert stats["acceptance_rate"] is None
    assert stats["false_correction_rate"] is None
    with pytest.raises(ReviewLedgerError, match="Unknown proposal"):
        record_outcome(database, "0" * 16, "accepted")


@pytest.mark.parametrize(
    "content",
    [
        "not json\n",
        "{}\n",
        "\n",
        '{"format":"mnemosyne-review-ledger/v1","event":"review"}\n',
        '{"format":"mnemosyne-review-ledger/v1","event":"unknown"}\n',
    ],
)
def test_malformed_ledger_lines_fail_loudly(tmp_path, content):
    path = ledger_path(tmp_path / "memory.db")
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ReviewLedgerError):
        read_events(path)


def test_truncated_ledger_line_fails_loudly(tmp_path):
    database = tmp_path / "memory.db"
    record_review(database, _report())
    path = ledger_path(database)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    with pytest.raises(ReviewLedgerError, match="truncated"):
        read_events(path)


def test_outcome_for_an_unknown_proposal_fails_loudly(tmp_path):
    database = tmp_path / "memory.db"
    record_review(database, _report())
    path = ledger_path(database)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "format": "mnemosyne-review-ledger/v1",
                    "event": "outcome",
                    "proposal_id": "0" * 16,
                    "recorded_at": "2026-11-09T00:00:00Z",
                    "outcome": "accepted",
                    "source": "explicit",
                    "observed": None,
                }
            )
            + "\n"
        )
    with pytest.raises(ReviewLedgerError, match="unknown proposal"):
        summarize(read_events(path), path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs extra privileges on Windows")
def test_ledger_refuses_a_symlink(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    path = ledger_path(tmp_path / "memory.db")
    path.symlink_to(target)
    with pytest.raises(ReviewLedgerError, match="regular file"):
        record_review(tmp_path / "memory.db", _report())
    with pytest.raises(ReviewLedgerError, match="regular file"):
        read_events(path)


def test_cli_correction_records_a_proposal_then_a_measured_outcome(tmp_path, capsys):
    database, evidence = _fixture(tmp_path)
    parser = _parser()
    args = parser.parse_args(
        [
            "review",
            "--id",
            "note",
            "--evidence",
            str(evidence),
            "--correction",
            "--correction-message-id",
            "7",
            "--session-id",
            "own",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch.dict(sys.modules, {"mnemosyne.core.beam": None}),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ),
    ):
        assert cli.mnemosyne_command(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["trigger"] == "explicit_correction"
    assert payload["review_id"] == payload["proposal_id"]
    assert payload["ledger"]["recorded"] is True
    assert payload["ledger"]["superseded"] == []
    assert "The exit is east." not in ledger_path(database).read_text(encoding="utf-8")

    _set_content(database, "The exit is east.")
    outcome_args = parser.parse_args(
        [
            "review-outcome",
            "--proposal",
            payload["proposal_id"],
            "--outcome",
            "accepted",
        ]
    )
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(outcome_args) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["outcome"] == "accepted"
    assert outcome["observed"] == "matches_proposal"
    assert outcome["warnings"] == []

    stats_args = parser.parse_args(["review-stats"])
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(stats_args) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["reviews"] == 1
    assert stats["accepted"] == 1
    assert stats["accepted_verified"] == 1
    assert stats["accepted_unverified"] == 0
    assert stats["acceptance_rate"] == 1.0
    assert stats["verified_acceptance_rate"] == 1.0
    assert stats["false_correction_rate"] == 0.0
    assert stats["observations"]["matches_proposal"] == 1
    assert stats["outcome_sources"]["explicit"] == 1


def test_unverified_acceptance_is_flagged(tmp_path, capsys):
    database, _ = _fixture(tmp_path)
    proposal = record_review(database, _report(), session_id="own")
    args = _parser().parse_args(
        [
            "review-outcome",
            "--proposal",
            proposal["proposal_id"],
            "--outcome",
            "accepted",
        ]
    )
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["observed"] == "matches_original"
    assert payload["warnings"]

    stats_args = _parser().parse_args(["review-stats"])
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(stats_args) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["accepted"] == 1
    assert stats["accepted_verified"] == 0
    assert stats["accepted_unverified"] == 1
    assert stats["verified_acceptance_rate"] == 0.0
    assert stats["observations"]["matches_original"] == 1


def test_cli_review_pending_lists_undecided_proposals(tmp_path, capsys):
    database, _ = _fixture(tmp_path)
    first = record_review(database, _report(), session_id="own")
    second = record_review(database, _report(replacement="The exit is north."), session_id="own")
    record_outcome(database, first["proposal_id"], "accepted")
    args = _parser().parse_args(["review-pending"])
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["pending"][0]["proposal_id"] == second["proposal_id"]
    assert payload["pending"][0]["memory_id"] == "note"


def test_plain_source_check_writes_no_ledger(tmp_path, capsys):
    database, evidence = _fixture(tmp_path)
    args = _parser().parse_args(
        [
            "review",
            "--id",
            "note",
            "--evidence",
            str(evidence),
            "--session-id",
            "own",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ),
    ):
        assert cli.mnemosyne_command(args) == 0
    capsys.readouterr()
    assert not ledger_path(database).exists()


def test_ledger_failure_returns_the_proposal_before_failing(tmp_path, capsys):
    database, evidence = _fixture(tmp_path)
    ledger_path(database).mkdir()
    args = _parser().parse_args(
        [
            "review",
            "--id",
            "note",
            "--evidence",
            str(evidence),
            "--correction",
            "--session-id",
            "own",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ),
        pytest.raises(SystemExit, match="1"),
    ):
        cli.mnemosyne_command(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["review_id"] is None
    assert payload["ledger"]["recorded"] is False


@pytest.mark.parametrize(
    "messages",
    [
        [{"id": "7", "role": "assistant", "content": "The exit is east."}],
        [{"id": "7", "role": "tool", "content": "The exit is east."}],
    ],
)
def test_correction_requires_a_user_message_and_does_not_call_the_model(tmp_path, messages):
    database, evidence = _fixture(tmp_path)
    evidence.write_text(
        json.dumps(
            {
                "session_id": "own",
                "message_id_namespace": "internal",
                "messages": messages,
            }
        )
    )
    args = _parser().parse_args(
        [
            "review",
            "--id",
            "note",
            "--evidence",
            str(evidence),
            "--correction",
            "--session-id",
            "own",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ) as model,
        pytest.raises(SystemExit, match="1"),
    ):
        cli.mnemosyne_command(args)
    assert model.call_count == 0
    assert not ledger_path(database).exists()


def test_correction_message_id_must_name_a_user_message(tmp_path):
    database, evidence = _fixture(tmp_path)
    evidence.write_text(
        json.dumps(
            {
                "session_id": "own",
                "message_id_namespace": "internal",
                "messages": [
                    {"id": "7", "role": "user", "content": "The exit is east."},
                    {"id": "8", "role": "assistant", "content": "Noted."},
                ],
            }
        )
    )
    args = _parser().parse_args(
        [
            "review",
            "--id",
            "note",
            "--evidence",
            str(evidence),
            "--correction",
            "--correction-message-id",
            "8",
            "--session-id",
            "own",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        patch(
            "hermes_memory_provider.hermes_llm_adapter.HermesAuxLLMBackend.complete",
            side_effect=_complete(_judgment()),
        ) as model,
        pytest.raises(SystemExit, match="1"),
    ):
        cli.mnemosyne_command(args)
    assert model.call_count == 0
    assert not ledger_path(database).exists()


def test_correction_flags_require_the_semantic_mode(tmp_path):
    parser = _parser()
    with pytest.raises(SystemExit, match="1"):
        cli.mnemosyne_command(parser.parse_args(["review", "--id", "note", "--correction"]))
    with pytest.raises(SystemExit, match="1"):
        cli.mnemosyne_command(
            parser.parse_args(
                [
                    "review",
                    "--id",
                    "note",
                    "--evidence",
                    str(tmp_path / "x.json"),
                    "--correction-message-id",
                    "7",
                ]
            )
        )


def test_stats_without_a_ledger_reports_zeroes(tmp_path, capsys):
    database, _ = _fixture(tmp_path)
    args = _parser().parse_args(["review-stats"])
    with patch.object(cli, "resolve_effective_db_path", return_value=str(database)):
        assert cli.mnemosyne_command(args) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["ledger_exists"] is False
    assert stats["reviews"] == 0
    assert stats["proposals"] == 0
    assert stats["acceptance_rate"] is None
    assert stats["false_correction_rate"] is None


def test_unknown_outcome_proposal_fails_in_the_cli(tmp_path):
    database, _ = _fixture(tmp_path)
    args = _parser().parse_args(
        [
            "review-outcome",
            "--proposal",
            "0" * 16,
            "--outcome",
            "accepted",
        ]
    )
    with (
        patch.object(cli, "resolve_effective_db_path", return_value=str(database)),
        pytest.raises(SystemExit, match="1"),
    ):
        cli.mnemosyne_command(args)
