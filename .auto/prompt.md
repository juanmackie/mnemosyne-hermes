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
(Update as experiments accumulate.)
