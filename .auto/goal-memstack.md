# Goal — Supermemory-derived memory-behaviour port to Mnemosyne

Status: **drafted, not confirmed** (propose_goal_draft RPC fails in this PI WEB
session with `Cannot read properties of undefined (reading 'cancelled')`).
This file is the durable copy of the drafted contract.

Primary metric: `membench_score` (higher is better)
Secondary guards: frozen `realquery_heldout_mrr/hit1/hit5`, `recall_latency_warm_p95_ms`

## Objective

Port the memory-behaviour ideas behind Supermemory's LongMemEval / LoCoMo /
ConvoMem results into Mnemosyne as **deterministic Rust**, keeping only the
ports that measurably improve a new memory-behaviour scoreboard.

Supermemory's engine is closed source; the stealable surface is design, API
shape and prompts (cloned read-only at
`/tmp/pi-github-repos/41daecd68811a5fe9a25ad84729b5720b14b48aea2c4d730dd0c833d5eb1a45e`,
see `apps/docs/concepts/*.mdx`, `apps/mcp/src/server`).

## Primary metric

`membench_score` = mean category score over a NEW deterministic, keyless suite
in `.auto/membench/`, covering failure classes the frozen 177-record suite
cannot see (it sits at 0.98–1.0 MRR and session 2 forbids ranking changes):

- `temporal_latest_wins` — contradictory facts, current value must rank first
- `always_on` — profile facts (name, timezone, tone) under semantically
  unrelated queries, where no query can match them
- `reference_noise` — reference material must not crowd out personal facts
- `expired_facts` — "exam tomorrow" style facts must drop out
- `multihop_derived` — answers no single memory states

Tune on the **dev** split; report on the **held-out** split. Deterministic
corpus + pinned `bge-small-en-v1.5` => deltas are exact, not sampled noise.

## Success criteria

1. `.auto/membench/` (corpus + dev/held-out queries + labels) and
   `.auto/measure_mem.sh` emitting `METRIC` lines; baseline logged in a NEW
   `.auto/log-memstack.jsonl` (prior logs stay append-only history).
2. All 7 idea families carry a logged keep/discard verdict with numbers.
3. Final held-out `membench_score` > baseline by more than the noise floor.
4. Guards intact: frozen `realquery_heldout_mrr/hit1/hit5` ≥ baseline;
   `recall_latency_warm_p95_ms` within the ~25 ms band.
5. `./.auto/checks.sh` green on every keep; final `cargo test --all` and
   `./test-all.sh --skip-llm` green; work on a feature branch, `main` untouched;
   docs updated for behaviour changes.

## Boundaries

In scope: `src/storage/libsql.rs`, `src/utils/{retrieval,ppr,hotness}.rs`,
`src/cli/recall.rs`, `src/mcp/tools.rs`, `src/evolution/*`,
`src/context_assembler.rs`, `src/config.rs`, `migrations/`, `.auto/` harness.

Out of scope: LLM calls inside the measured path (flag-gated, never part of
keep/discard); frozen benchmark data (`corpus.jsonl`, `eval_*.jsonl`, labels,
pinned encoder, fixture seeds); tuning against `generalization_probe.py`
(audit-only); removing public MCP tool names or Hermes underscore aliases;
rewriting `log.jsonl` / `log-latency.jsonl`.

## Constraints

Deterministic measured path only (no network, no keys, byte-reproducible).
Additive-only MCP surface. New crates only when clearly better than
hand-rolling, justified in the commit message. Fail-closed retrieval semantics
preserved. `keep` only when the primary improves AND guards hold.

## Verification contract

Per kept change: `./.auto/checks.sh` (rustfmt, `storage::` tests, MCP server
tests, `--features full,distributed`) + membench dev & held-out METRIC lines +
frozen guards not below baseline + warm p95 in band + one generalization-probe
audit. At completion: `cargo test --all`, `./test-all.sh --skip-llm`,
`mnemosyne doctor` clean; `.auto/reports/` per-idea before/after table with
keep/discard reasons; docs updated; every kept change re-justified against its
own logged numbers.

## If blocked

Stop and ask the user when: a guard regresses and cannot be restored; a schema
migration is not backward-compatible with existing user DBs; the scoreboard
cannot separate two candidate designs; an idea proves untestable without an
LLM or API key.

## Task tree (execution order = expected-win ranking)

| id | task | verdict |
|---|---|---|
| task-0 | Branch + scoreboard: `.auto/membench` corpus, dev/held-out queries, `measure_mem.sh`, baseline, `log-memstack.jsonl` | **done** (a30e30f; held-out baseline 0.5842, guards reproduce session-2: 0.981481/0.962963/1.0, warm p95 19.1ms) |
| task-1 | Typed `UPDATES`/`EXTENDS`/`DERIVES` edges + indexed `is_latest` preferred by retrieval (replaces the 0.35 supersession nudge) | pending |
| task-2 | Always-on profile: deterministic auto-maintained static+dynamic fact sheet per namespace, returned with recall | **done** (a69d97d; always-on 0.133->0.458 = the 3-slot ceiling, held-out 0.5842->0.6383, guards bit-identical, warm p95 20.0ms; static/dynamic *split* deferred — recency slots added no value yet) |
| task-3 | Ingest split: reference ("superrag") content searchable but never a memory, profile fact or graph edge | pending |
| task-4 | Enforced temporal validity / auto-forgetting at recall + consolidation (`expires_at` set at ingest, not honoured) | pending |
| task-5 | Deterministic AND/OR metadata filter DSL + explicit `searchMode`, pushed into SQL | pending |
| task-6 | DERIVES inference pass in consolidation, with provenance + confidence, dedup-guarded | pending |
| task-7 | Dreaming as coherent-unit batching, default-off flag | pending |
| task-8 | Consolidation, full verification, docs, before/after report | pending |

Per-task verification contracts are recorded in the drafting transcript; each
keeps `checks.sh` green plus its own category improvement, and task-1 also
requires migrating a copy of a pre-existing DB without data loss.
