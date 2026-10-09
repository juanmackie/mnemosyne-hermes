# Memoryrepo-inspired implementation

**Date:** 2026-10-09. **Branch:** `codex/memoryrepo-improvements`.

The user authorized implementation after the [proposed plan](MEMORYREPO_PROPOSED_PLAN.md).
Three `gpt-6-luna` subagents with high reasoning implemented and reviewed the audit,
provider CLI/capture, and lite changes. The parent integrated the read-only evidence
helper, documentation, gates, and final checks. The upstream findings are pinned in
the original plan; no memoryrepo source was copied. The continuation used three
additional Luna high subagents for maintenance status, the Windows test runner
and review of both the original changes and the continuation.

## Delivered

| Slice | Result |
| --- | --- |
| Evidence characterization | Real pinned-engine contract verifies metadata through update/validation, episodic lineage in export, and source-reference origin. Summary lineage remains separate from metadata; transcript lookup is unavailable. |
| Audit repair | Classified engine databases only; short-lived calling-thread connections; 100 ms SQLite busy wait; serialized close/retarget; sanitized health counters; failed reads raise. Successful mutations record target ownership, so deleted memories retain eligible partial history. |
| Inspection | Existing CLI gains `inspect --id` and `history --id`, with bounded output, own/global session selection, explicit local-operator `--all-sessions`, and history JSON/text. No engine initialization or DDL for these reads. |
| Source references | Unique exact host message matches with stable IDs can carry source references through turn/identity capture. Duplicate IDs and ambiguous matches omit references. Caller metadata remains asserted across direct, batch, shared, native, and pending writes. |
| Markdown projection | Optional `export --format markdown` reads a SQLite snapshot, bounds content/metadata/lineage/index, escapes Markdown, filters eligible lineage, marks missing evidence, and publishes only a new file. DB and SQLite sidecar targets are refused. |
| Lite improvements | MCP count failures propagate as storage errors; bounded prefetch fallback totals are labelled. Cards optionally show a bounded descriptive context and UTC creation date. Tool names, aliases, schema and content-body cap are preserved. |
| Maintenance status | Existing diagnose reports process-local tracked-worker state, trigger, start/finish times, monotonic duration, sanitized failure class, reuse/skip counts and shutdown timeout. Status reads avoid worker/foreground lock ordering. |
| Portable verification | The full test runner uses the configured interpreter's native path separator and preserves an inherited engine path, including Windows Git Bash and paths containing spaces. |

The four default provider tools, registration, engine pin, lite schema and version
remain unchanged. The provider changes are recorded as P26–P29 in
[PATCHES.md](../integrations/hermes-provider/PATCHES.md), with byte hashes in the
vendored manifest. New regression gates are registered in `scripts/checks.sh`.

## Limits

Audit history is best effort and partial, without atomic memory/audit commits or
undo. Stored references identify asserted sources; they do not verify a claim or
authenticate imported metadata. Consolidation retains original IDs, but does not
automatically preserve every original's source metadata. These commands select
records for a local operator; a session filter is not an authorization boundary.

The bounded status slice from phase 5 is implemented following the user's request
to continue. Status is process-local and covers automatic/session-end work; broader
maintenance/claim changes still require a reproduced engine gap. The existing
tracked consolidation worker remains in place. This change adds no additional
worker, cache, Git-backed memory store, schema migration, engine pin widening or
LLM dependency. Stored source-origin labels are explicitly unauthenticated in
inspection output and rendered as stored claims in Markdown.

## Verification

Verification uses temporary synthetic memory stores. A temporary copy of the
pinned installed engine
receives the repository's existing audited engine patches for strict tests because
the live installed engine lacks the newer consolidation lock API. The live package
and user memory databases are untouched.

- Full Bash runner on Windows: `./test-all.sh --skip-llm --require-engine` passed
  all provider contract gates and **249 unit tests**, selecting the isolated
  patched engine through the inherited `PYTHONPATH`. The earlier native strict
  run passed all 238 tests before the continuation added eleven regressions.
- Registered gates: `bash scripts/checks.sh` **ALL PASS**, including snapshot hashes,
  version agreement, installation, provider lifecycle, inspection/audit/evidence,
  lite and engine contracts.
  After the abort-finalization edge fix, the new registered gate plus the existing
  consolidation lane passed all 28 focused tests; the full suite above also includes them.
- Public-prefetch evaluation: **9/9 passed**, no execution errors, LLM calls disabled.
- Pre-commit for changed files: **all four hooks passed** (Ruff, formatting, mypy,
  shellcheck). Scoped Ruff checks/formatting, compilation and `git diff --check` passed.
- Repository-wide `pre-commit run --all-files` fails on pre-existing lint/format
  issues in `.claude/hillclimb/recall-quality` and `task/score.py`; these unrelated
  evaluation files were left unchanged.

The initial `test-all.sh` run under Git Bash combined Unix and Windows `PYTHONPATH`
separators and fell back to the live engine, exposing seven missing audited-lock
API failures. The continuation fixes the runner; the final full run above verifies
the isolated engine is selected. No live-engine patch was changed.

No live Hermes gateway acceptance was executed on this Windows host. The Linux/macOS
onboarding lane and optional LLM consolidation remain outside this local run.
