# Autoresearch session 2 — Hermes recall latency

Branch `autoresearch/hermes-retrieval-latency-2026-09-07`, log `.auto/log-latency.jsonl`
(session 1's quality log `.auto/log.jsonl` is untouched).

Objective: cut the latency a Hermes agent sees on `mnemosyne_recall` through a warm
MCP stdio server, without moving retrieval ranking. Encoder pinned to
`bge-small-en-v1.5`; no new dependencies; PPR / graph expansion / vector search /
diagnostics / fail-closed semantics and the public MCP surface all stay.

Primary metric: `recall_latency_warm_p95_ms` — p95 of `mnemosyne_recall` against one
already-warm `mnemosyne serve` process over the 177-memory eval corpus
(`.auto/evaluate_mcp_warm.py`). Guards: held-out MRR/Hit@1/Hit@5 (CLI **and** MCP),
warm MRR/Hit@1, cold MCP and cold CLI p95.

## Result

| metric | baseline | after | delta |
|---|---|---|---|
| warm recall p95 (primary) | 417.9 ms | **20.2 ms** | **-95.2%** |
| warm recall p50 | 262.1 ms | 13.9 ms | -94.7% |
| cold MCP p95 (`mnemosyne serve`, first call) | 2807.9 ms | ~1257 ms | -55% |
| cold CLI p95 (fresh process per call) | 1846.2 ms | ~1232 ms | -33% |
| held-out MRR (MCP) | 1.0 | 1.0 | = |
| warm MRR / Hit@1 | 0.989583 / 0.979167 | 0.989583 / 0.979167 | = |
| 854-test lib suite wall time | 301 s | 7 s | -97.7% |

Ranking is bit-identical on every guard; all the movement is in fixed per-call cost.

## What the time actually was

1. **`Database::connect()` per storage call.** `LibsqlStorage::get_conn()` reopened
   the database file on every call; libsql's `Connection` is an `Arc` clone, so one
   handle is now held on the struct. Warm p95 417.9 → 314.4 ms.
2. **fsync'd autocommit writes.** The store ran with the rollback journal
   (`journal_mode=delete`) and `synchronous=FULL`: 28.6 ms per committed single-row
   insert on this disk. One recall commits several times (retrieval trace, link
   traversal signal, access bookkeeping), so ~300 ms of the 418 ms was fsync, not
   search. `journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL` on the
   shared handle: 314.4 → 20.4 ms. Measured per commit:
   `delete+FULL 28.6 ms → WAL+FULL 11.7 ms → WAL+NORMAL 0.34 ms`.
3. **An O(history) scan on every query** (kept as hardening, primary-neutral):
   `record_retrieval_trace` ran `SELECT COUNT(*), SUM(fallback…) FROM
   retrieval_traces` over every trace ever written — 5 ms at 10k rows, 27 ms at 50k —
   plus a golden-item evaluation replay of up to 100 traces. Now sampled every 64th
   trace.

## Deliberate tradeoff to review

`synchronous=NORMAL` in WAL mode survives application crashes; an OS or power failure
can lose the last few commits. That is a change to the durability guarantee, so it is
configurable: `MNEMOSYNE_SQLITE_SYNCHRONOUS=full` restores per-commit fsync and is
still ~2.4× faster per commit than the previous setting. `journal_mode=WAL` itself
adds no durability loss and requires a local filesystem (not NFS) for the `-shm` file.

## Things measured and rejected

- The `source_id = ? OR target_id = ?` join in the recursive graph CTE is **not** the
  wall on this SQLite build: `EXPLAIN QUERY PLAN` shows the OR-optimization using
  `idx_links_source_target` + `idx_links_target_source`, and the whole walk over
  2k memories / 12k links costs ~2 ms. Rewriting it as a materialized undirected
  `edges` CTE was **15× slower** (32 ms, two full index scans per query). Rejected.
- Memoizing `connection_has_column` PRAGMA probes: 0.03 ms each. Not worth code.
- Cold-start work (embedding model load) dominates cold p95; a long-lived MCP server
  pays it once, so it is out of scope for this objective.

## Harness notes

- `recall_latency_warm_p95_ms` added as primary; `.auto/evaluate_mcp_warm.py` scores
  the same held-out set against one warm server, so quality and latency come from the
  same process state.
- `compact` now defaults to true on `mnemosyne_recall`; evaluators must pass
  `"compact": false` or every score collapses to 0 (this invalidated the first
  baseline).
- The evaluators copy the corpus DB with `copy2`, which takes the main file only.
  Under WAL a leftover `-wal` would hide committed writes, so `setup_data.py`
  checkpoints (TRUNCATE) and refuses a cached corpus that has a stray `-wal`.
- `.auto/checks.sh` (inherited gate) covers 21 tests; `.auto/checks_full.sh` runs the
  whole 854-test lib suite and is what storage/connection changes get run against.
- Open measurement question: the keyless bench's per-channel profile attributes
  ~30 ms to `graph_traverse_bounded` at 2k memories, while the same SQL measured
  directly against SQLite is ~2 ms and the real warm path is 14 ms end to end. The
  channel profiler is not trusted for absolute attribution until that gap is
  explained; treat its numbers as relative only.
- Raw CLI held-out Hit@1 is noisy run to run (0.963–1.0) while the MCP variant of the
  same query set is stable at 1.0; the log keeps both raw values.
