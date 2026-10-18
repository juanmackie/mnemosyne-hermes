#!/usr/bin/env python3
"""Score memory-writer artifacts against authored synthetic traces.

This is an evaluation tool, not a production memory writer. It does not call a
model, read a live memory database, or change provider behavior. A candidate
artifact is produced elsewhere from the same traces with retrieval, answer and
decoding settings held fixed. Optional provider replay writes only to fresh
temporary stores through the existing public provider handlers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CASES_FORMAT = "mnemosyne-memory-writer-cases/v1"
ARTIFACT_FORMAT = "mnemosyne-memory-writer-artifact/v1"
REPORT_FORMAT = "mnemosyne-memory-writer-report/v1"
KINDS = {"fact", "preference", "identity", "plan", "experience", "decision", "blocker"}
SUBJECTS = {"user", "assistant", "repo", "other"}
SCOPES = {"session", "global"}
OPS = {"add", "update", "supersede"}
MAX_JSON_BYTES = 5_000_000
MAX_MEMORIES = 100
MAX_OPERATIONS = 100
MAX_EVIDENCE = 20

CASE_KEYS = {
    "id", "split", "domain", "trace", "context", "seed_memories",
    "preserve_seeds", "required", "forbidden", "allowed_extra",
    "expect_no_memories", "recall_query", "max_chars",
}
MESSAGE_KEYS = {"id", "role", "content", "name"}
CLAIM_KEYS = {
    "id", "description", "all", "any", "subject_any", "subject_name_any",
    "kind_any", "evidence", "exempt_any", "op", "valid_until",
}
SEED_KEYS = {"id", "content", "subject", "scope"}
MEMORY_KEYS = {
    "content", "kind", "subject", "subject_name", "scope", "valid_until",
    "evidence",
}
OPERATION_KEYS = {"op", "target", "memory"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: Any) -> str:
    return " ".join(str(value).lower().split())


def _terms(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("term lists must contain non-empty strings")
    return [_text(item) for item in value]


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(f"JSON input exceeds {MAX_JSON_BYTES} bytes: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON from {path}: {error}") from error


def resolve_output_path(output: str | os.PathLike[str], inputs: Iterable[str | os.PathLike[str]]) -> Path:
    """Return a new output path, refusing overwrite, stores and input paths."""
    path = Path(output).expanduser().resolve(strict=False)
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"output directory does not exist: {parent}")
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite existing output: {path}")
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".jsonl"}:
        raise ValueError(f"refusing to write an evaluation report to a store path: {path}")
    resolved_inputs = {Path(item).expanduser().resolve(strict=False) for item in inputs}
    if path in resolved_inputs:
        raise ValueError(f"refusing to overwrite an input artifact: {path}")
    return path


def _add(errors: list[str], where: str, message: str) -> None:
    errors.append(f"{where}: {message}")


def _validate_message(message: Any, where: str, errors: list[str], *, trace: bool) -> None:
    if not isinstance(message, dict):
        _add(errors, where, "message must be an object")
        return
    unknown = set(message) - MESSAGE_KEYS
    if unknown:
        _add(errors, where, f"unknown message keys: {sorted(unknown)}")
    if not isinstance(message.get("id"), str) or not message["id"].strip():
        _add(errors, where, "message id must be a non-empty string")
    if not isinstance(message.get("role"), str) or not message["role"].strip():
        _add(errors, where, "message role must be a non-empty string")
    if not isinstance(message.get("content"), str):
        _add(errors, where, "message content must be a string")
    if "name" in message and not isinstance(message["name"], str):
        _add(errors, where, "message name must be a string")
    if trace:
        content = message.get("content")
        if isinstance(content, str):
            for marker in ("http://", "https://", "sk-", "AKIA", "ghp_", "-----BEGIN"):
                if marker in content:
                    _add(errors, where, f"synthetic trace contains forbidden material: {marker}")


def validate_fixture(fixture: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(fixture, dict):
        return ["fixture: must be an object"]
    unknown = set(fixture) - {
        "version", "format", "description", "default_max_chars", "replay_tools", "cases",
    }
    if unknown:
        _add(errors, "fixture", f"unknown keys: {sorted(unknown)}")
    if fixture.get("format") != CASES_FORMAT:
        _add(errors, "fixture", f"format must be {CASES_FORMAT}")
    if not isinstance(fixture.get("version"), int) or fixture["version"] < 1:
        _add(errors, "fixture", "version must be a positive integer")
    if not isinstance(fixture.get("description"), str) or not fixture["description"].strip():
        _add(errors, "fixture", "description must be a non-empty string")
    default_max = fixture.get("default_max_chars")
    if not isinstance(default_max, int) or not 1 <= default_max <= 100_000:
        _add(errors, "fixture", "default_max_chars must be a positive bounded integer")
        default_max = 2000
    tools = fixture.get("replay_tools")
    if not isinstance(tools, list) or not tools or any(not isinstance(item, str) or not item.strip() for item in tools):
        _add(errors, "fixture", "replay_tools must be a non-empty list of tool names")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        _add(errors, "fixture", "cases must be a non-empty list")
        return errors
    case_ids: set[str] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            _add(errors, where, "case must be an object")
            continue
        unknown = set(case) - CASE_KEYS
        if unknown:
            _add(errors, where, f"unknown case keys: {sorted(unknown)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            _add(errors, where, "id must be a non-empty string")
            continue
        where = f"case {case_id}"
        if case_id in case_ids:
            _add(errors, where, "duplicate case id")
        case_ids.add(case_id)
        if case.get("split") not in {"dev", "held_out"}:
            _add(errors, where, "split must be dev or held_out")
        if case.get("domain") not in {"personal", "coding"}:
            _add(errors, where, "domain must be personal or coding")
        if not isinstance(case.get("expect_no_memories"), bool):
            _add(errors, where, "expect_no_memories must be boolean")
        if not isinstance(case.get("recall_query"), str) or not case["recall_query"].strip():
            _add(errors, where, "recall_query must be a non-empty string")
        max_chars = case.get("max_chars", default_max)
        if not isinstance(max_chars, int) or not 1 <= max_chars <= 100_000:
            _add(errors, where, "max_chars must be a positive bounded integer")
            max_chars = default_max
        trace = case.get("trace")
        context = case.get("context")
        if not isinstance(trace, list) or not trace:
            _add(errors, where, "trace must be a non-empty list")
            trace = []
        if not isinstance(context, list):
            _add(errors, where, "context must be a list")
            context = []
        trace_ids = set()
        for message_index, message in enumerate(trace):
            _validate_message(message, f"{where} trace[{message_index}]", errors, trace=True)
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                if message["id"] in trace_ids:
                    _add(errors, where, f"duplicate trace id: {message['id']}")
                trace_ids.add(message["id"])
        context_ids = set()
        for message_index, message in enumerate(context):
            _validate_message(message, f"{where} context[{message_index}]", errors, trace=False)
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                if message["id"] in context_ids:
                    _add(errors, where, f"duplicate context id: {message['id']}")
                if message["id"] in trace_ids:
                    _add(errors, where, f"context id overlaps trace id: {message['id']}")
                context_ids.add(message["id"])
        seeds = case.get("seed_memories")
        if not isinstance(seeds, list):
            _add(errors, where, "seed_memories must be a list")
            seeds = []
        seed_ids = set()
        for seed_index, seed in enumerate(seeds):
            seed_where = f"{where} seed_memories[{seed_index}]"
            if not isinstance(seed, dict):
                _add(errors, seed_where, "seed must be an object")
                continue
            unknown = set(seed) - SEED_KEYS
            if unknown:
                _add(errors, seed_where, f"unknown seed keys: {sorted(unknown)}")
            if not isinstance(seed.get("id"), str) or not seed["id"].strip():
                _add(errors, seed_where, "id must be a non-empty string")
            elif seed["id"] in seed_ids:
                _add(errors, where, f"duplicate seed id: {seed['id']}")
            else:
                seed_ids.add(seed["id"])
            if not isinstance(seed.get("content"), str) or not seed["content"].strip():
                _add(errors, seed_where, "content must be a non-empty string")
            if seed.get("subject") not in SUBJECTS:
                _add(errors, seed_where, "subject is not recognized")
            if seed.get("scope") not in SCOPES:
                _add(errors, seed_where, "scope is not recognized")
        preserve = case.get("preserve_seeds")
        if not isinstance(preserve, list) or any(not isinstance(item, str) for item in preserve):
            _add(errors, where, "preserve_seeds must be a list of seed ids")
            preserve = []
        if set(preserve) - seed_ids:
            _add(errors, where, f"preserve_seeds references unknown seeds: {sorted(set(preserve) - seed_ids)}")
        for field in ("required", "forbidden", "allowed_extra"):
            claims = case.get(field)
            if not isinstance(claims, list):
                _add(errors, where, f"{field} must be a list")
                continue
            claim_ids = set()
            for claim_index, claim in enumerate(claims):
                claim_where = f"{where} {field}[{claim_index}]"
                if not isinstance(claim, dict):
                    _add(errors, claim_where, "claim must be an object")
                    continue
                unknown = set(claim) - CLAIM_KEYS
                if unknown:
                    _add(errors, claim_where, f"unknown claim keys: {sorted(unknown)}")
                claim_id = claim.get("id")
                if not isinstance(claim_id, str) or not claim_id.strip():
                    _add(errors, claim_where, "id must be a non-empty string")
                elif claim_id in claim_ids:
                    _add(errors, where, f"duplicate {field} claim id: {claim_id}")
                else:
                    claim_ids.add(claim_id)
                try:
                    all_terms = _terms(claim.get("all")) if "all" in claim else []
                    any_terms = _terms(claim.get("any")) if "any" in claim else []
                except ValueError as error:
                    _add(errors, claim_where, str(error))
                    all_terms, any_terms = [], []
                if not all_terms and not any_terms:
                    _add(errors, claim_where, "must have non-empty all or any terms")
                for name in ("subject_any", "subject_name_any", "kind_any", "exempt_any"):
                    if name in claim:
                        if not isinstance(claim[name], list) or any(
                            not isinstance(item, str) or not item.strip() for item in claim[name]
                        ):
                            _add(errors, claim_where, f"{name} must contain non-empty strings")
                if "subject_any" in claim and set(claim["subject_any"]) - SUBJECTS:
                    _add(errors, claim_where, f"unknown subject_any values: {sorted(set(claim['subject_any']) - SUBJECTS)}")
                if "kind_any" in claim and set(claim["kind_any"]) - KINDS:
                    _add(errors, claim_where, f"unknown kind_any values: {sorted(set(claim['kind_any']) - KINDS)}")
                if "valid_until" in claim:
                    try:
                        _validate_date(claim["valid_until"], claim_where)
                    except ValueError as error:
                        _add(errors, claim_where, str(error))
                evidence = claim.get("evidence")
                if evidence is not None:
                    if not isinstance(evidence, list) or not evidence or any(
                        not isinstance(item, str) or not item.strip() for item in evidence
                    ):
                        _add(errors, claim_where, "evidence must be a non-empty list of trace ids")
                    elif set(evidence) - trace_ids:
                        _add(errors, claim_where, f"evidence references unknown trace ids: {sorted(set(evidence) - trace_ids)}")
                elif field in {"required", "allowed_extra"}:
                    _add(errors, claim_where, "evidence is required")
                if "op" in claim:
                    if field != "required":
                        _add(errors, claim_where, "op is only valid on required claims")
                    _validate_expected_operation(claim["op"], seed_ids, claim_where, errors)
        if case.get("expect_no_memories") and case.get("required"):
            _add(errors, where, "expect_no_memories conflicts with required claims")
    return errors


def _validate_date(value: Any, where: str) -> None:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("valid_until must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"valid_until is not a valid date: {value}") from error


def _validate_expected_operation(operation: Any, seed_ids: set[str], where: str, errors: list[str]) -> None:
    if not isinstance(operation, dict):
        _add(errors, where, "op must be an object")
        return
    unknown = set(operation) - {"op", "target"}
    if unknown:
        _add(errors, where, f"unknown expected op keys: {sorted(unknown)}")
    if operation.get("op") not in OPS:
        _add(errors, where, "expected op must be add, update or supersede")
    if operation.get("op") == "add":
        if "target" in operation:
            _add(errors, where, "add expected op must not have a target")
    elif operation.get("op") in {"update", "supersede"}:
        if operation.get("target") not in seed_ids:
            _add(errors, where, "expected update/supersede target is not a seed id")


def load_fixture(path: str | os.PathLike[str]) -> dict[str, Any]:
    fixture = _read_json(Path(path))
    errors = validate_fixture(fixture)
    if errors:
        raise ValueError("invalid fixture:\n" + "\n".join(errors))
    return fixture


def validate_artifact(artifact: Any, fixture: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return ["artifact: must be an object"]
    unknown = set(artifact) - {"format", "writer", "cases"}
    if unknown:
        _add(errors, "artifact", f"unknown keys: {sorted(unknown)}")
    if artifact.get("format") != ARTIFACT_FORMAT:
        _add(errors, "artifact", f"format must be {ARTIFACT_FORMAT}")
    writer = artifact.get("writer")
    if not isinstance(writer, dict):
        _add(errors, "artifact", "writer must be an object")
    else:
        unknown = set(writer) - {"name", "model", "prompt_version", "decoding"}
        if unknown:
            _add(errors, "artifact.writer", f"unknown keys: {sorted(unknown)}")
        for key in ("name", "model"):
            if not isinstance(writer.get(key), str) or not writer[key].strip():
                _add(errors, "artifact.writer", f"{key} must be a non-empty string")
        if "prompt_version" in writer and not isinstance(writer["prompt_version"], str):
            _add(errors, "artifact.writer", "prompt_version must be a string")
        if "decoding" in writer and not isinstance(writer["decoding"], dict):
            _add(errors, "artifact.writer", "decoding must be an object")
    cases = artifact.get("cases")
    if not isinstance(cases, list):
        _add(errors, "artifact", "cases must be a non-empty list")
        return errors
    fixture_cases: dict[str, dict[str, Any]] = {}
    default_max = 2000
    if fixture is not None:
        fixture_cases = {case["id"]: case for case in fixture.get("cases", [])}
        default_max = fixture.get("default_max_chars", default_max)
    seen_ids: set[str] = set()
    for case_index, case in enumerate(cases):
        where = f"artifact.cases[{case_index}]"
        if not isinstance(case, dict):
            _add(errors, where, "case must be an object")
            continue
        unknown = set(case) - {"id", "memories", "operations"}
        if unknown:
            _add(errors, where, f"unknown case keys: {sorted(unknown)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            _add(errors, where, "id must be a non-empty string")
            continue
        where = f"artifact case {case_id}"
        if case_id in seen_ids:
            _add(errors, where, "duplicate case id")
        seen_ids.add(case_id)
        fixture_case = fixture_cases.get(case_id)
        max_chars = fixture_case.get("max_chars", default_max) if fixture_case else default_max
        memories = case.get("memories")
        operations = case.get("operations")
        if not isinstance(memories, list):
            _add(errors, where, "memories must be a list")
            memories = []
        if not isinstance(operations, list):
            _add(errors, where, "operations must be a list")
            operations = []
        if len(memories) > MAX_MEMORIES:
            _add(errors, where, f"too many memories (>{MAX_MEMORIES})")
        if len(operations) > MAX_OPERATIONS:
            _add(errors, where, f"too many operations (>{MAX_OPERATIONS})")
        for memory_index, memory in enumerate(memories):
            memory_where = f"{where} memories[{memory_index}]"
            if not isinstance(memory, dict):
                _add(errors, memory_where, "memory must be an object")
                continue
            unknown = set(memory) - MEMORY_KEYS
            if unknown:
                _add(errors, memory_where, f"unknown memory keys: {sorted(unknown)}")
            content = memory.get("content")
            if not isinstance(content, str) or not content.strip():
                _add(errors, memory_where, "content must be a non-empty string")
            elif len(content) > max_chars:
                _add(errors, memory_where, f"content exceeds max_chars ({len(content)} > {max_chars})")
            if memory.get("kind") not in KINDS:
                _add(errors, memory_where, "kind is not recognized")
            if memory.get("subject") not in SUBJECTS:
                _add(errors, memory_where, "subject is not recognized")
            if "subject_name" in memory and memory["subject_name"] is not None and not isinstance(memory["subject_name"], str):
                _add(errors, memory_where, "subject_name must be a string or null")
            if memory.get("scope") not in SCOPES:
                _add(errors, memory_where, "scope is not recognized")
            if "valid_until" in memory and memory["valid_until"] is not None:
                try:
                    _validate_date(memory["valid_until"], memory_where)
                except ValueError as error:
                    _add(errors, memory_where, str(error))
            evidence = memory.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(
                not isinstance(item, str) or not item.strip() for item in evidence
            ):
                _add(errors, memory_where, "evidence must be a non-empty list of trace ids")
            elif len(evidence) > MAX_EVIDENCE:
                _add(errors, memory_where, f"too many evidence ids (>{MAX_EVIDENCE})")
            elif len(evidence) != len(set(evidence)):
                _add(errors, memory_where, "evidence ids must be unique")
            if fixture_case is not None and isinstance(evidence, list):
                trace_ids = {message["id"] for message in fixture_case["trace"]}
                if set(evidence) - trace_ids:
                    _add(errors, memory_where, f"evidence references ids outside the trace: {sorted(set(evidence) - trace_ids)}")
        referenced: set[int] = set()
        for operation_index, operation in enumerate(operations):
            operation_where = f"{where} operations[{operation_index}]"
            if not isinstance(operation, dict):
                _add(errors, operation_where, "operation must be an object")
                continue
            unknown = set(operation) - OPERATION_KEYS
            if unknown:
                _add(errors, operation_where, f"unknown operation keys: {sorted(unknown)}")
            op = operation.get("op")
            if op not in OPS:
                _add(errors, operation_where, "op must be add, update or supersede")
            memory_index = operation.get("memory")
            if not isinstance(memory_index, int) or isinstance(memory_index, bool):
                _add(errors, operation_where, "memory must be an integer index")
            elif not 0 <= memory_index < len(memories):
                _add(errors, operation_where, "memory index is out of range")
            else:
                if memory_index in referenced:
                    _add(errors, operation_where, "memory index is referenced by more than one operation")
                referenced.add(memory_index)
            if op == "add":
                if "target" in operation and operation["target"] is not None:
                    _add(errors, operation_where, "add operation must not have a target")
            elif op in {"update", "supersede"}:
                target = operation.get("target")
                if not isinstance(target, str) or not target.strip():
                    _add(errors, operation_where, "update/supersede requires a target seed id")
                elif fixture_case is not None:
                    seed_ids = {seed["id"] for seed in fixture_case["seed_memories"]}
                    if target not in seed_ids:
                        _add(errors, operation_where, f"target is not a seed id: {target}")
        for memory_index in range(len(memories)):
            if memory_index not in referenced:
                _add(errors, where, f"memory {memory_index} is not referenced by an operation")
    return errors


def _text_matches(memory: dict[str, Any], claim: dict[str, Any]) -> bool:
    content = _text(memory.get("content", ""))
    all_terms = [_text(item) for item in claim.get("all", [])]
    any_terms = [_text(item) for item in claim.get("any", [])]
    if all_terms and not all(term in content for term in all_terms):
        return False
    if any_terms and not any(term in content for term in any_terms):
        return False
    return bool(all_terms or any_terms)


def _subject_matches(memory: dict[str, Any], claim: dict[str, Any]) -> bool:
    if "subject_any" in claim and memory.get("subject") not in claim["subject_any"]:
        return False
    if "subject_name_any" in claim:
        name = memory.get("subject_name")
        if not isinstance(name, str) or name.lower() not in {item.lower() for item in claim["subject_name_any"]}:
            return False
    if "kind_any" in claim and memory.get("kind") not in claim["kind_any"]:
        return False
    return True


def _claim_matches(memory: dict[str, Any], claim: dict[str, Any]) -> bool:
    if not _text_matches(memory, claim) or not _subject_matches(memory, claim):
        return False
    if "valid_until" in claim and memory.get("valid_until") != claim["valid_until"]:
        return False
    evidence = set(memory.get("evidence", []))
    return bool(evidence & set(claim.get("evidence", [])))


def _forbidden_matches(memory: dict[str, Any], claim: dict[str, Any]) -> bool:
    if not _text_matches(memory, claim):
        return False
    if not _subject_matches(memory, claim):
        return False
    exempt = [_text(item) for item in claim.get("exempt_any", [])]
    content = _text(memory.get("content", ""))
    if exempt and any(term in content for term in exempt):
        return False
    return True


def _expected_operation_present(
    claim: dict[str, Any], memory_matches: list[int], operations: list[dict[str, Any]]
) -> bool:
    expected = claim.get("op")
    if not expected:
        return True
    for operation in operations:
        if operation.get("op") != expected.get("op"):
            continue
        if expected["op"] == "add":
            if operation.get("target") is not None:
                continue
        elif operation.get("target") != expected.get("target"):
            continue
        if operation.get("memory") in memory_matches:
            return True
    return False


def _case_score(case: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case_id": case["id"],
        "domain": case["domain"],
        "split": case["split"],
        "passed": False,
        "failures": [],
        "missing_required": [],
        "required_found": [],
        "unsupported_memories": 0,
        "forbidden_violations": 0,
        "duplicate_claims": 0,
        "operation_failures": 0,
        "evidence_failures": 0,
        "preserved_seed_violations": 0,
        "content_chars": 0,
    }
    if candidate is None:
        result["failures"].append("missing case in candidate artifact")
        result["missing_required"] = [claim["id"] for claim in case["required"]]
        return result
    memories = candidate["memories"]
    operations = candidate["operations"]
    trace_ids = {message["id"] for message in case["trace"]}
    result["content_chars"] = sum(len(memory["content"]) for memory in memories)
    failures = result["failures"]
    if case.get("expect_no_memories") and (memories or operations):
        failures.append("no-op case produced candidate memories or operations")
    for index, memory in enumerate(memories):
        unknown = set(memory["evidence"]) - trace_ids
        if unknown:
            result["evidence_failures"] += 1
            failures.append(f"memory {index} cites evidence outside the trace")
    required_matches: dict[str, list[int]] = {}
    for claim in case["required"]:
        matches = [
            index for index, memory in enumerate(memories) if _claim_matches(memory, claim)
        ]
        required_matches[claim["id"]] = matches
        if not matches:
            result["missing_required"].append(claim["id"])
        else:
            result["required_found"].append(claim["id"])
        if len(matches) > 1:
            result["duplicate_claims"] += 1
            failures.append(f"required claim {claim['id']} matched by multiple memories")
        if matches and not _expected_operation_present(claim, matches, operations):
            result["operation_failures"] += 1
            failures.append(f"required claim {claim['id']} has no matching expected operation")
    supported: set[int] = set()
    for claim in case["required"] + case["allowed_extra"]:
        supported.update(
            index for index, memory in enumerate(memories) if _claim_matches(memory, claim)
        )
    for index in range(len(memories)):
        if index not in supported:
            result["unsupported_memories"] += 1
            failures.append(f"memory {index} is not supported by a required or allowed claim")
    for index, memory in enumerate(memories):
        for claim in case["forbidden"]:
            if _forbidden_matches(memory, claim):
                result["forbidden_violations"] += 1
                failures.append(f"memory {index} violates forbidden claim {claim['id']}")
    mutated_targets = {
        operation["target"]
        for operation in operations
        if operation["op"] in {"update", "supersede"} and "target" in operation
    }
    for seed_id in case["preserve_seeds"]:
        if seed_id in mutated_targets:
            result["preserved_seed_violations"] += 1
            failures.append(f"preserved seed {seed_id} was mutated")
    expected_targets = {
        claim["op"]["target"]
        for claim in case["required"]
        if claim.get("op", {}).get("op") in {"update", "supersede"}
    }
    for target in mutated_targets:
        if target not in expected_targets:
            result["operation_failures"] += 1
            failures.append(f"unexpected seed mutation: {target}")
    for claim_id in result["missing_required"]:
        failures.append(f"missing required claim: {claim_id}")
    result["passed"] = not failures
    return result


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_required = 0
    found_required = 0
    metrics = {
        "passed_cases": 0,
        "failed_cases": 0,
        "required_claims": 0,
        "required_claims_found": 0,
        "required_claim_recall": 1.0,
        "unsupported_memories": 0,
        "forbidden_violations": 0,
        "duplicate_claims": 0,
        "operation_failures": 0,
        "evidence_failures": 0,
        "preserved_seed_violations": 0,
        "content_chars": 0,
    }
    for row in rows:
        metrics["passed_cases"] += int(row["passed"])
        metrics["failed_cases"] += int(not row["passed"])
        metrics["unsupported_memories"] += row["unsupported_memories"]
        metrics["forbidden_violations"] += row["forbidden_violations"]
        metrics["duplicate_claims"] += row["duplicate_claims"]
        metrics["operation_failures"] += row["operation_failures"]
        metrics["evidence_failures"] += row["evidence_failures"]
        metrics["preserved_seed_violations"] += row["preserved_seed_violations"]
        metrics["content_chars"] += row["content_chars"]
        total_required += len(row["required_found"]) + len(row["missing_required"])
        found_required += len(row["required_found"])
    metrics["required_claims"] = total_required
    metrics["required_claims_found"] = found_required
    metrics["required_claim_recall"] = (
        found_required / total_required if total_required else 1.0
    )
    return metrics


def _group(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[field]].append(row)
    return {key: _aggregate(value) for key, value in grouped.items()}


def score_artifact(fixture: dict[str, Any], artifact: Any) -> dict[str, Any]:
    errors = validate_artifact(artifact, fixture)
    if errors:
        return {
            "format": REPORT_FORMAT,
            "artifact": {"valid": False, "errors": errors},
            "writer": artifact.get("writer") if isinstance(artifact, dict) else None,
            "cases": [],
            "by_domain": {},
            "by_split": {},
            "metrics": {
                "case_count": len(fixture["cases"]),
                "cases_provided": 0,
                "passed_cases": 0,
                "failed_cases": len(fixture["cases"]),
                "required_claims": 0,
                "required_claims_found": 0,
                "required_claim_recall": 0.0,
                "unsupported_memories": 0,
                "forbidden_violations": 0,
                "duplicate_claims": 0,
                "operation_failures": 0,
                "evidence_failures": 0,
                "preserved_seed_violations": 0,
                "content_chars": 0,
                "missing_case_ids": [case["id"] for case in fixture["cases"]],
                "unexpected_case_ids": [],
            },
        }
    candidate_cases = {case["id"]: case for case in artifact["cases"]}
    fixture_ids = {case["id"] for case in fixture["cases"]}
    rows = [
        _case_score(case, candidate_cases.get(case["id"])) for case in fixture["cases"]
    ]
    metrics = _aggregate(rows)
    metrics["case_count"] = len(fixture["cases"])
    metrics["cases_provided"] = len(candidate_cases)
    metrics["missing_case_ids"] = sorted(fixture_ids - set(candidate_cases))
    metrics["unexpected_case_ids"] = sorted(set(candidate_cases) - fixture_ids)
    return {
        "format": REPORT_FORMAT,
        "artifact": {"valid": True, "errors": []},
        "writer": artifact["writer"],
        "cases": rows,
        "by_domain": _group(rows, "domain"),
        "by_split": _group(rows, "split"),
        "metrics": metrics,
    }


def oracle_artifact(fixture: dict[str, Any]) -> dict[str, Any]:
    """Build a self-check artifact from the fixture's own labels.

    This is an instrument check, not a memory writer. It must never be reported
    as model-quality evidence.
    """
    cases = []
    for case in fixture["cases"]:
        memories: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        for claim in case["required"]:
            parts = list(claim.get("all", [])) + list(claim.get("any", []))[:1]
            subject = claim.get("subject_any", [None])[0]
            if subject is None:
                subject = "repo" if case["domain"] == "coding" else "user"
            kind = claim.get("kind_any", [None])[0] or "fact"
            subject_name = claim.get("subject_name_any", [None])[0]
            memories.append(
                {
                    "content": " ".join(parts),
                    "kind": kind,
                    "subject": subject,
                    "subject_name": subject_name,
                    "scope": "global" if op_target(claim) else "session",
                    "valid_until": claim.get("valid_until"),
                    "evidence": list(claim["evidence"]),
                }
            )
            operation = dict(claim.get("op", {"op": "add"}))
            operation["memory"] = len(memories) - 1
            operations.append(operation)
        cases.append(
            {"id": case["id"], "memories": memories, "operations": operations}
        )
    return {
        "format": ARTIFACT_FORMAT,
        "writer": {
            "name": "synthetic-oracle",
            "model": "synthetic-oracle",
            "prompt_version": "fixture-labels",
            "decoding": {"temperature": 0},
        },
        "cases": cases,
    }


def op_target(claim: dict[str, Any]) -> str | None:
    return claim.get("op", {}).get("target")


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_cases = {row["case_id"]: row for row in baseline["cases"]}
    candidate_cases = {row["case_id"]: row for row in candidate["cases"]}
    regressions = []
    for case_id, base in base_cases.items():
        current = candidate_cases.get(case_id)
        if current is None:
            regressions.append({"case_id": case_id, "kind": "case_missing"})
            continue
        if len(base["required_found"]) > len(current["required_found"]):
            regressions.append(
                {
                    "case_id": case_id,
                    "kind": "required_claim_loss",
                    "baseline_found": len(base["required_found"]),
                    "candidate_found": len(current["required_found"]),
                }
            )
        if base["passed"] and not current["passed"]:
            regressions.append({"case_id": case_id, "kind": "case_pass_lost"})
        if current["forbidden_violations"] > base["forbidden_violations"]:
            regressions.append({"case_id": case_id, "kind": "forbidden_violation_increase"})
    return {
        "regressions": regressions,
        "baseline_passed_cases": baseline["metrics"]["passed_cases"],
        "candidate_passed_cases": candidate["metrics"]["passed_cases"],
        "passed_case_delta": candidate["metrics"]["passed_cases"] - baseline["metrics"]["passed_cases"],
        "baseline_required_claim_recall": baseline["metrics"]["required_claim_recall"],
        "candidate_required_claim_recall": candidate["metrics"]["required_claim_recall"],
    }


def _call_provider(provider: Any, name: str, args: dict[str, Any]) -> Any:
    if not provider.has_tool(name):
        raise RuntimeError(f"required replay tool is not exposed: {name}")
    raw = provider.handle_tool_call(name, dict(args))
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{name} returned non-JSON: {str(raw)[:200]!r}") from error
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"{name} failed: {payload['error']}")
    return payload


def _distribution_version(name: str) -> str | None:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return None


def _engine_pin() -> str | None:
    try:
        import tomllib

        path = Path(__file__).resolve().parents[1] / "integrations" / "hermes-provider" / "pyproject.toml"
        dependencies = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["dependencies"]
        return next(
            (dependency for dependency in dependencies if dependency.startswith("mnemosyne-memory")),
            None,
        )
    except Exception:
        return None


def _configure_replay_tools(provider: Any, tools: list[str]) -> None:
    """Make the declared fixture tools the provider's configured surface.

    ``tools=`` passed to ``initialize`` is only an override hint in the
    vendored provider; ``has_tool`` resolves ``_read_config_key("tools")``,
    which normally comes from ``memory.mnemosyne.tools``. Replay has no Hermes
    CLI to write a config file, so it installs the same resolution path on the
    throwaway provider instance and still fails loudly for an unknown tool.
    """
    reader = getattr(provider, "_read_config_key", None)
    if not callable(reader):
        raise RuntimeError("provider does not expose the tool configuration path needed for replay")

    def _read(key: str) -> Any:
        if key == "tools":
            return list(tools)
        return reader(key)

    provider.__dict__["_read_config_key"] = _read


def _default_provider_factory() -> Any:
    try:
        import mnemosyne.core.beam  # noqa: F401
    except Exception as error:  # noqa: BLE001 - report the exact unavailable reason
        raise RuntimeError(
            "provider replay requires the audited mnemosyne-memory engine; "
            "install ./integrations/hermes-provider in an isolated environment"
        ) from error
    provider_root = Path(__file__).resolve().parents[1] / "integrations" / "hermes-provider"
    if str(provider_root) not in sys.path:
        sys.path.insert(0, str(provider_root))
    import hermes_memory_provider

    provider = hermes_memory_provider.MnemosyneMemoryProvider()
    provider.__dict__["_init_audit_log"] = lambda: None
    return provider


class _replay_environment:
    def __init__(self, root: Path):
        self.root = root
        self.saved: dict[str, str] = {}

    def __enter__(self) -> None:
        self.saved = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("MNEMOSYNE_") or key == "HERMES_HOME"
        }
        for key in list(self.saved):
            os.environ.pop(key, None)
        os.environ.update(
            {
                "HERMES_HOME": str(self.root),
                "MNEMOSYNE_DATA_DIR": str(self.root / "_engine_data"),
                "MNEMOSYNE_EMBEDDINGS_OFF": "1",
            }
        )

    def __exit__(self, *_exc: Any) -> None:
        for key in [key for key in os.environ if key.startswith("MNEMOSYNE_") or key == "HERMES_HOME"]:
            os.environ.pop(key, None)
        os.environ.update(self.saved)


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("results", "memories"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _memory_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    memory = payload.get("memory")
    if isinstance(memory, dict):
        return str(memory.get("content", ""))
    return str(payload.get("content", ""))


def run_replay(
    fixture: dict[str, Any],
    artifact: dict[str, Any],
    *,
    provider_factory: Any = None,
) -> dict[str, Any]:
    errors = validate_artifact(artifact, fixture)
    if errors:
        return {"ok": False, "engine": "not-run", "cases": 0, "results": [], "errors": errors}
    factory = provider_factory or _default_provider_factory
    engine = "injected" if provider_factory is not None else "real"
    tools = list(fixture["replay_tools"])
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mnemosyne-writer-replay-") as folder:
        root = Path(folder)
        with _replay_environment(root):
            for case in fixture["cases"]:
                candidate = next(
                    (row for row in artifact["cases"] if row["id"] == case["id"]), None
                )
                if candidate is None:
                    results.append({"case_id": case["id"], "ok": False, "errors": ["missing case"]})
                    continue
                provider = factory()
                case_errors: list[str] = []
                try:
                    provider.initialize(
                        session_id=f"writer_eval_{case['id']}",
                        hermes_home=str(root),
                        agent_context="primary",
                        db_path=str(root / f"{case['id']}.db"),
                        auto_sleep=False,
                        shared_surface_path=str(root / "shared" / "mnemosyne.db"),
                    )
                    if getattr(provider, "_init_error", None):
                        raise RuntimeError(f"provider initialization failed: {provider._init_error}")
                    _configure_replay_tools(provider, tools)
                    for tool in tools:
                        if not provider.has_tool(tool):
                            raise RuntimeError(f"declared replay tool is not exposed: {tool}")
                    seed_ids: dict[str, str] = {}
                    seed_content: dict[str, str] = {}
                    for seed in case["seed_memories"]:
                        payload = _call_provider(
                            provider,
                            "mnemosyne_remember",
                            {
                                "content": seed["content"],
                                "scope": seed["scope"],
                                "source": "eval_seed",
                                "importance": 0.8,
                            },
                        )
                        memory_id = payload.get("memory_id")
                        if not memory_id:
                            raise RuntimeError(f"seed {seed['id']} did not return a memory id")
                        seed_ids[seed["id"]] = memory_id
                        seed_content[seed["id"]] = seed["content"]
                    memory_ids: dict[int, str] = {}
                    for operation in candidate["operations"]:
                        memory = candidate["memories"][operation["memory"]]
                        if operation["op"] == "add":
                            payload = _call_provider(
                                provider,
                                "mnemosyne_remember",
                                {
                                    "content": memory["content"],
                                    "scope": memory["scope"],
                                    "source": "eval_writer",
                                    "importance": 0.5,
                                },
                            )
                            memory_id = payload.get("memory_id")
                            if not memory_id:
                                raise RuntimeError("remember did not return a memory id")
                            memory_ids[operation["memory"]] = memory_id
                        else:
                            target = seed_ids.get(operation.get("target", ""))
                            if not target:
                                raise RuntimeError(f"unknown seed target: {operation.get('target')}")
                            _call_provider(
                                provider,
                                "mnemosyne_update",
                                {"memory_id": target, "content": memory["content"]},
                            )
                            memory_ids[operation["memory"]] = target
                    for memory_index, memory_id in memory_ids.items():
                        payload = _call_provider(
                            provider, "mnemosyne_get", {"memory_id": memory_id}
                        )
                        content = _memory_content(payload)
                        expected = candidate["memories"][memory_index]["content"]
                        if content != expected:
                            case_errors.append(
                                f"memory {memory_index} content did not persist exactly"
                            )
                    for seed_id in case["preserve_seeds"]:
                        payload = _call_provider(
                            provider, "mnemosyne_get", {"memory_id": seed_ids[seed_id]}
                        )
                        if _memory_content(payload) != seed_content[seed_id]:
                            case_errors.append(f"preserved seed {seed_id} changed")
                    recall = _call_provider(
                        provider,
                        "mnemosyne_recall",
                        {"query": case["recall_query"], "limit": 10},
                    )
                    recall_rows = _extract_rows(recall)
                    results.append(
                        {
                            "case_id": case["id"],
                            "ok": not case_errors,
                            "errors": case_errors,
                            "persisted_memories": len(memory_ids),
                            "recalled_rows": len(recall_rows),
                        }
                    )
                except Exception as error:  # noqa: BLE001 - replay reports per-case failure
                    results.append(
                        {
                            "case_id": case["id"],
                            "ok": False,
                            "errors": [f"{type(error).__name__}: {error}"],
                            "persisted_memories": 0,
                            "recalled_rows": 0,
                        }
                    )
                finally:
                    shutdown = getattr(provider, "shutdown", None)
                    if callable(shutdown):
                        shutdown()
    return {
        "ok": all(row["ok"] for row in results),
        "engine": engine,
        "engine_version": _distribution_version("mnemosyne-memory"),
        "engine_pin": _engine_pin(),
        "provider": "hermes_memory_provider",
        "cases": len(results),
        "results": results,
        "errors": [row for row in results if not row["ok"]],
        "correction_replay": "update-in-place; no historical chain is asserted",
    }


def build_report(
    fixture: dict[str, Any],
    artifact: dict[str, Any],
    *,
    cases_path: Path | None = None,
    candidate_path: Path | None = None,
    baseline: dict[str, Any] | None = None,
    baseline_path: Path | None = None,
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scored = score_artifact(fixture, artifact)
    report = {
        "format": REPORT_FORMAT,
        "generated_at": _now(),
        "fixture": {
            "format": fixture["format"],
            "version": fixture["version"],
            "case_count": len(fixture["cases"]),
            "domains": dict(Counter(case["domain"] for case in fixture["cases"])),
            "splits": dict(Counter(case["split"] for case in fixture["cases"])),
            "sha256": _sha256(cases_path) if cases_path else None,
        },
        "artifact": scored["artifact"],
        "writer": scored["writer"],
        "candidate_sha256": _sha256(candidate_path) if candidate_path else None,
        "metrics": scored["metrics"],
        "by_domain": scored["by_domain"],
        "by_split": scored["by_split"],
        "cases": scored["cases"],
    }
    if baseline is not None:
        report["baseline"] = {
            "artifact_valid": baseline["artifact"]["valid"],
            "metrics": baseline["metrics"],
            "sha256": _sha256(baseline_path) if baseline_path else None,
        }
        report["comparison"] = _compare(baseline, scored)
    if replay is not None:
        report["replay"] = replay
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    data = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as output:
        output.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--require-no-regression", action="store_true")
    parser.add_argument("--replay-provider", action="store_true")
    args = parser.parse_args(argv)
    try:
        inputs = [args.cases, args.candidate]
        if args.baseline:
            inputs.append(args.baseline)
        output = resolve_output_path(args.output, inputs)
        fixture = load_fixture(args.cases)
        artifact = _read_json(args.candidate)
        candidate = score_artifact(fixture, artifact)
        baseline = None
        if args.baseline:
            baseline_data = _read_json(args.baseline)
            baseline = score_artifact(fixture, baseline_data)
            if not baseline["artifact"]["valid"]:
                raise ValueError("baseline artifact is invalid:\n" + "\n".join(baseline["artifact"]["errors"]))
        replay = None
        if args.replay_provider:
            if not candidate["artifact"]["valid"]:
                raise ValueError("cannot replay an invalid candidate artifact")
            replay = run_replay(fixture, artifact)
        report = build_report(
            fixture,
            artifact,
            cases_path=args.cases,
            candidate_path=args.candidate,
            baseline=baseline,
            baseline_path=args.baseline,
            replay=replay,
        )
        _write_report(output, report)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if not candidate["artifact"]["valid"]:
        print("INVALID: candidate artifact failed validation", file=sys.stderr)
        return 2
    if replay is not None and not replay["ok"]:
        print("REPLAY FAILED: provider replay reported case failures", file=sys.stderr)
        return 2 if replay["engine"] == "not-run" else 1
    if args.require_no_regression and baseline is not None:
        regressions = report["comparison"]["regressions"]
        if regressions:
            print(f"REGRESSION: {len(regressions)} regression(s)", file=sys.stderr)
            return 1
    if args.require_pass:
        metrics = report["metrics"]
        passed = (
            candidate["artifact"]["valid"]
            and metrics["passed_cases"] == metrics["case_count"]
            and not metrics["missing_case_ids"]
            and not metrics["unexpected_case_ids"]
        )
        if not passed:
            print(
                f"FAILED: {metrics['passed_cases']}/{metrics['case_count']} cases passed",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
