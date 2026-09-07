# Autoresearch: Hermes recall latency (Mnemosyne retrieval path)

Session 2. Session 1 (`.auto/prompt-quality-session.md`, log `.auto/log.jsonl`, 17 runs)
saturated retrieval **quality** on the frozen benchmark: held-out CLI+MCP MRR and
Hit@1/Hit@5 = 1.0. Its own final logs show the cost of that quality work:
`recall_latency_p95_ms` grew from 1 738 ms (run 1) to 3 237 ms (run 17), because
candidate pools got bigger and reranking got heavier. This session pays that bill
back: **same ranking, much faster.**

## Objective

Cut the per-call latency of `mnemosyne_recall` on the local-only Hermes stack
(bge-small-en-v1.5 embeddings, libsql, MCP stdio server) **without moving any
memory up or down the ranking**. Quality is the hard constraint, latency is the
target. The frozen benchmark corpus is 177 personal-agent records; the query
sets are 27 × 3 splits evaluated through both the CLI and the MCP surface.

## Metrics

- **Primary**: `recall_latency_mcp_p95_ms` (**lower is better**) — p95 of
  per-call `mnemosyne_recall` latency across held-out A+B through the MCP stdio
  server (6 concurrent workers). This is what a Hermes agent actually waits on.
- **Hard guard**: `realquery_heldout_mrr` must stay 1.000000 and
  `realquery_heldout_hit1` / `hit5` must stay 1.000000. A run that moves them is
  a ranking change → discard (or re-scope explicitly).
- **Secondary**: `recall_latency_cli_p95_ms` (includes process spawn; hooks pay
  it, Hermes does not), `recall_latency_p95_ms` (session-1 name = max over all
  surfaces, kept for cross-session comparability), `realquery_*` quality metrics.
- **Fast localizer** (`measure_fast.sh`, keyless path, no embeddings):
  `hybrid_ppr_p95_ms`, `keyword_only_p95_ms`, `graph_delta_ms`, `ppr_delta_ms`,
  `results_hash` (32-bit FNV of top-3 ids per query — must not move),
  `ingest_ms` (write-path proxy).

## How to Run

```bash
./.auto/run.sh keep|discard|crash|checks_failed "description" ['{"asi":"json"}']
                              # measure (authoritative) + append .auto/log-latency.jsonl
./.auto/run.sh               # dry run: measure without logging
./.auto/measure.sh           # raw METRIC lines only
./.auto/checks.sh            # rustfmt + storage tests + MCP tests + full,distributed check
./.auto/measure_fast.sh      # ~4 min, no model: in-process Rust bench, 2k synthetic memories
BENCH_PROFILE=1 ./.auto/measure_fast.sh   # per-channel timings (keyword/graph/ppr/trace)
```

`./.auto/run.sh` prints the delta vs the best kept run and the session noise
floor. If storage schema/corruption errors appear after a storage change:
`rm -rf .auto/data` to force a corpus rebuild.

Iteration cost: incremental release build + 81 CLI + 54 MCP calls, then checks.
Budget ~8 min/run; measure with `measure_fast.sh` first when you only need a
direction, and use `BENCH_PROFILE=1` to find the channel before touching code.

## Files in Scope

- `src/storage/libsql.rs` — the retrieval path. Hot spots already located:
  `get_conn` (1922), `hybrid_search` (8175), `keyword_search`,
  `graph_traverse_bounded` (3696), `fetch_ppr_adjacency` (3545),
  `active_ppr_nodes` (3657), `get_memories_batch` (10040),
  `record_retrieval_trace` (9681), `retrieval_setting`,
  `connection_has_column` (305).
- `src/utils/retrieval.rs`, `src/utils/ppr.rs` — reranking + graph math.
- `src/cli/recall.rs`, `src/mcp/tools.rs` — per-call pipelines above storage.
- `src/config.rs` — `SearchConfig` (add knobs if needed, keep defaults).
- `migrations/` — indexes live here.
- `benches/hermes_recall_bench.rs`, `.auto/measure*.sh` — harness, edit for signal.

## Off Limits

- **Ranking.** No score, weight, candidate-pool, fusion, coverage, supersession
  or stopword changes that move `realquery_heldout_mrr/hit1/hit5` or
  `results_hash`. Perf work must be behavior-preserving by construction.
- Benchmark data: `corpus.jsonl`, `eval_*.jsonl`, labels, the pinned
  `bge-small-en-v1.5` encoder, fixture seeds in the Rust bench.
- Do not disable features (PPR, graph expansion, vector search, diagnostics,
  fail-closed) to buy speed; do not shrink `max_results` or add `LIMIT`s.
- Public MCP tool names/schemas and the Hermes underscore aliases.
- Session-1 artifacts: `log.jsonl` stays append-only history; do not rewrite it.

## Constraints

- `./.auto/checks.sh` must pass (rustfmt, `storage::` tests, MCP server tests,
  `--features full,distributed` compile). Cannot keep a failing run.
- No new dependencies (`Cargo.lock` stays as-is apart from nothing; the only
  allowed `Cargo.toml` edit is `[[bench]]` registration for this session's bench).
- Fail-closed retrieval semantics intact.

## What's Been Tried

### Session 1 (quality, `.auto/log.jsonl`) — do not undo

FTS candidate limit 20→50 and vector fetch `limit*2`→`limit*4`; coverage-aware
fusion with light stemming + compound tokenization + conversational stopwords +
`host`/`serve` normalization; 0.35 supersession penalty. Held-out MRR
0.9506 → 1.0. Known dead ends there: FTS 100, vector 50, keyword/vector
rebalance 0.35/0.35, structured type priors, current-state recency boost. Their
own logs flag p95 latency rising to ~3.2 s as the accepted cost — that is this
session's starting point.

### Session 2 recon (measured, 2026-09-07)

`measure_fast.sh` profile, 300-memory keyless store, best of 5:

| call | cost |
|---|---|
| `keyword_search` (FTS, 50 candidates) | 52 ms |
| `graph_traverse_bounded` (2 hops → 290 rows) | **292 ms** |
| `fetch_ppr_adjacency` (2 hops) | 7.6 ms |
| `count_memories` | 1.2 ms |
| `retrieval_weights` | 1.0 ms |
| `record_retrieval_trace` | 25 ms |
| `store_memory` | ~50 ms |

Structural causes, verified:

1. **`get_conn()` = `self.db.connect()` — a brand-new libsql connection for every
   storage call** (`libsql.rs:1922`). One `hybrid_search` opens ~8-10 connections
   (keyword, entities, graph, batch fetch, PPR adjacency, active-node check, trace
   write, settings). Proof it is connection/lock cost, not SQL work: single-threaded
   ingest of an **in-memory** database takes ~50 ms/row, and running 8 ingest tasks
   concurrently fails with `database is locked`. libsql `Connection` is `Clone` →
   hold one and hand out clones (or a tiny pool) instead of connecting per call.
2. `connection_has_column` runs `PRAGMA table_info` per call; 26 call sites, at
   least five on the per-query graph path (3805, 7025, 7331, 7387, 10086). Schema
   is fixed for the process lifetime → memoize.
3. `graph_traverse_bounded`'s recursive CTE joins
   `ml.source_id = gw.memory_id OR ml.target_id = gw.memory_id`; an `OR` join
   cannot use an index, so each hop scans `memory_links`. Same shape in
   `fetch_ppr_adjacency` (`source_id IN (…) OR target_id IN (…)`). Check
   `migrations/` for `(source_id)`/`(target_id)` indexes; split into two indexed
   halves + `UNION ALL` if absent.
4. `record_retrieval_trace` re-reads a settings row (and may read two) before every
   trace insert; `retrieval_weights()` is a settings read per query.
5. Hot SQL is built with `format!` and re-parsed; where the text is constant,
   prepare once.

### Dead ends

- (fill in as runs land)
