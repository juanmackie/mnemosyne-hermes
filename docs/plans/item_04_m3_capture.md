# M3 — Capture and Knowledge Semantics (Deliverable)

Status: DESIGN COMPLETE / IMPLEMENTATION BLOCKED. Capture pipeline contracts preserved (user-origin turns only; skip contexts: cron/flush/subagent/background/skill_loop; raw source + pending job commit; deterministic extraction; optional agent-backed extraction; provenance + audit events; embeddings/index updates via resumable jobs). Keyless verification path preserved: `mnemosyne_memory_remember` with `no-enrich` works without API keys.

Blocker: Upstream source MISSING; verified DB clone NOT FOUND. Implementation of durable capture/retry/crash/conflict/checkpoint verification requires upstream source and live DB.

Evidence: Adapter (`provider.py`) uses direct SQLite (`PythonMemoryStorage`) for memory operations (M1). Capture-context gates preserved. Checkpoint API version 2 (`pre_compress_checkpoint_api_version`) preserved. Bounded access-count flushing (`ACCESS_FLUSH_DISTINCT=256`, `ACCESS_FLUSH_HITS=1024`) preserved. No JSONL fallback in memory path (M1).

Not done: Capture pipeline retry/crash verification tests; stale enrichment tests; checkpoint failure prevention (design only). Implementation deferred until upstream source available.
