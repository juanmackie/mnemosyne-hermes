# Memory maintenance and approval contracts

This document turns the P1 recommendations in
[`MEMORY_STACK_RESEARCH.md`](MEMORY_STACK_RESEARCH.md) into implementation
contracts. It is limited to the existing Mnemosyne storage system and does not
introduce a second graph, database, taxonomy, or source of truth.

## Current seams

| Concern | Existing seam | Gap this phase closes |
|---|---|---|
| Memory truth | `memories`, supersession/as-of fields, `audit_log` | Maintenance needs bounded durable reports instead of only ad-hoc jobs. |
| Evidence | `memory_provenance`, policy evidence, entity indexes | Reports and proposals must carry source IDs, quotes, and scope. |
| Evolution | `src/evolution/*`, `EvolutionJob`, `JobConfig`, `JobReport` | Run state and limits need a durable contract. |
| Existing audit | `memory_modification_log` and `audit_log` | Maintenance/proposal transitions need structured records. |
| Review UI | `ChangeProposal`, `ProposalQueue`, ICS proposals panel | The queue is process-local and is not connected to canonical updates. |
| Ownership | namespaces and existing agent roles | Proposal routing must be explicit and must not grant silent write authority. |

## Maintenance protocol

A maintenance run is an asynchronous, bounded operation over the existing store.
It may report stale or broken state; it does not silently rewrite factual
memory.

Each run has a durable idempotency key and kind (`stale_links`,
`missing_citations`, or `health_summary`), namespace scope, timestamps, status,
item/retry/deadline limits, counters, and a compact JSON report. The current
run ID is also its lease token: an expired running record is reclaimed with a
new owner ID, and the old worker is checked/fenced at attempt and terminal-write
boundaries. Thus a slow or resumed worker cannot publish a duplicate report.
Caller limits are clamped to safe bounds. A failed operation is isolated where
possible, retries are finite and recorded, and interactive recall/remember
operations do not wait for maintenance.

Report kinds are:

- **Stale links:** links crossing a traversal-age threshold, or links with
  missing/archived endpoints.
- **Missing citations:** active factual memories without valid provenance, or
  provenance pointing at a missing source.
- **Health summary:** bounded text-learning integrity findings and missing
  embeddings.

Reports are advisory. Mutations belong to an explicit proposal or evolution
operation and are represented in the audit trail.

## Proposal and owner workflow

A proposal is a durable review artifact, not a queued instruction. It contains
an ID, namespace, target memory, base revision, exact before/after content,
line-scoped diff, source IDs, source revision snapshots, evidence quotes,
proposer, owner, status, review metadata, and apply/error metadata.

Allowed transitions are:

```text
pending -> accepted -> applied
pending -> dismissed
accepted -> failed
```

Only the explicit routed owner may accept or dismiss a pending proposal; a
wildcard owner is rejected. Applying an accepted proposal checks both the
target namespace and the captured base content/revision in the same
transaction. Source revisions and evidence quotes are rechecked too; a stale
proposal fails without changing canonical memory.
Verified proposal evidence replaces the target provenance, while summaries,
entities, keywords, and embeddings are refreshed or invalidated so derived
data cannot describe old content.
Repeated decisions and applies are rejected rather than replayed. All
creation, decisions, stale conflicts, and successful applies are recorded in
`audit_log`.

The generic proposal API handles factual memories. Interaction policies remain
in their typed policy tables and separate recall channel. Extracted response
feedback is written only as a pending, owner-routed policy proposal; it is not
materialized in `interaction_policies` until the owner explicitly accepts and
applies it. Stale source revisions or evidence fail the proposal without
creating a policy.

## Operator usage

Generate a bounded report and give it a stable key when a caller may retry:

```text
mnemosyne evolve maintenance missing-citations \
  --namespace project:myapp --item-limit 100 \
  --idempotency-key nightly-myapp-citations-2026-08-28
mnemosyne evolve maintenance-history --limit 20
```

Create and review a factual-memory proposal through the durable workflow:

```text
mnemosyne proposal create --target MEMORY_UUID \
  --proposed-content "approved replacement" --owner alice \
  --source-memory SOURCE_UUID --evidence "verbatim source quote"
mnemosyne proposal list --status pending
mnemosyne proposal show --id PROPOSAL_UUID
mnemosyne proposal accept --id PROPOSAL_UUID --reviewer alice
mnemosyne proposal apply --id PROPOSAL_UUID --reviewer alice
# or: mnemosyne proposal dismiss --id PROPOSAL_UUID --reviewer alice --note "..."

# Extracted interaction policies use a separate approval surface:
mnemosyne proposal policy list --status pending
mnemosyne proposal policy show --id POLICY_PROPOSAL_UUID
mnemosyne proposal policy accept --id POLICY_PROPOSAL_UUID --reviewer alice
mnemosyne proposal policy apply --id POLICY_PROPOSAL_UUID --reviewer alice
# or: mnemosyne proposal policy dismiss --id POLICY_PROPOSAL_UUID --reviewer alice
```

`accept` records the decision; `apply` performs the base-revision-checked
canonical update. If the target or an evidence source changed meanwhile, apply
fails without applying an orphaned or stale update. Reusing an idempotency key
with a different kind, namespace, or bound is rejected.

## Verification obligations

The implementation must provide fresh-database and upgrade-path migration
coverage for both SQLite schema families; tests for bounds, finite retries,
idempotent reruns, provenance failures, stale conflicts, acceptance,
dismissal, apply, and failed-apply preservation; operator-facing CLI or MCP
surfaces; and documentation of limits, statuses, ownership, and audit behavior.

All code is a clean-room implementation based on public behavior described in
the research report. Existing uncommitted work is not reset or committed.
