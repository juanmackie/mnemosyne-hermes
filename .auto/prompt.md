# Autoresearch: latency — all 4 pipelines

## Objective
Reduce latency across all four memory pipelines: search/recall, remember/ingest, MCP server roundtrip, and embedding generation. Primary metric is p99 latency (ms), lower is better. Workload: ~100 memories in the test DB.

## Metrics
- **Primary**: `p99_latency_ms` (ms, lower is better) — worst-case latency across all pipelines
- **Secondary**: `search_latency_ms`, `remember_latency_ms`, `mcp_roundtrip_ms`, `embed_latency_ms`

## How to Run
`./.auto/measure.sh` — outputs `METRIC name=value` lines.

## Files in Scope
- `src/lib/storage.py` — PythonMemoryStorage (recall, remember, list, consolidate)
- `src/mnemosyne/cli.py` — CLI entry point (recall, remember commands)
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/provider.py` — MCP provider
- `src/orchestration/` — orchestration agents (executor, orchestrator)

## Off Limits
- No new dependencies
- No Rust source changes
- No DB migration scripts

## Constraints
- Tests must pass (`python -m unittest discover -s tests -t . -v`)
- No new external packages
- Pure Python / stdlib only

## What's Been Tried
- **synchronous=FULL → NORMAL** (c53ed41): Removed fsync on every commit. remember p99 dropped 2.1× (1.847→0.884ms), overall p99 2× (0.977→0.476ms). Search unchanged.
- **mmap_size=256MB** (in-place): Marginal improvement (~5% faster reads).
- **WAL checkpoint every 10th write** (c1c590a): Reduced Windows stat overhead. remember p99 12% better (1.169→0.904ms).

## Ideas Backlog
- FTS5 virtual table for recall (replace LIKE '%query%' with MATCH)
- Drop idx_memories_recall (covering index slowed writes)
- Batch remember operations
- Pre-allocate connection pool
- Use sha1 instead of sha256 for memory IDs
- Optimize _row_to_dict with dataclasses.asdict
