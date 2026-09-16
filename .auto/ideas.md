# Deferred optimization ideas

## Memory search / storage

- **Trigram posting-list index for substring search** — the residual cost of a
  search is the full scan for rare/phrase/no-match queries (~0.5-0.6ms p50
  over 3000 rows, i.e. the whole query once the ordered index removes the
  sort). A `trigram -> rowid` table is sound for substring search: a row can
  only match if it contains every trigram of the query, so a no-match query
  answers from the index alone. Needs: populate on `remember`, backfill on
  open, queries shorter than 3 chars fall back to the scan, and a
  candidate-count cap that falls back to the scan for common terms (a term in
  every row would otherwise intersect to the full table). FTS5 was rejected
  for token semantics; this is the sound substring-preserving version.
- **`select only needed columns** — `recall`/`list_memories` use `SELECT *`;
  `_row_to_dict` reads positions 0-9 and `content_lower` was deliberately
  appended last so migrated and fresh DBs agree. An explicit column list would
  make that ordering constraint unnecessary, but it must be updated in one
  place to avoid drifting from `_row_to_dict`.
- **NULL-guard tradeoff if a mixed-version writer ever matters** — recall
  currently relies on the migration backfill for rows written without
  `content_lower`. A per-row fallback predicate was measured at 1.17-1.25x
  slower on the scan-bound shapes; only revisit if two versions really do
  share one DB file.

## Hermes MCP provider (out of scope for the search benchmark)

- **Per-call `PythonMemoryStorage` construction in the provider hot path** —
  `integrations/hermes-memory-provider/mnemosyne_rust_hermes/provider.py`
  `handle_tool_call` builds a storage instance (and closes it) on every tool
  call: measured 6.4ms median per call versus ~0.04ms for the namespaced
  recall it performs, so the construction is ~99% of the latency. Caching one
  instance per resolved DB path (and closing it in `shutdown()`) removes that;
  `integrations/hermes/src/mnemosyne_hermes/provider.py` already does this via
  `_get_storage()`. Not pursued here because `provider.py` is outside this
  session's scope and the benchmark measures `PythonMemoryStorage.recall`
  directly.
- **stdio transport**: `StdioJsonRpcClient` uses a thread-based stdout reader;
  timing-sensitive on Windows (`test_script_server_end_to_end`). Not a
  retrieval regression — adapter contracts preserved.
- **benchmark integration**: `.auto/data/template.db` exists (1.7MB, provenance
  `bge-small-en-v1.5`). `.auto/evaluate_membench.py` fails with
  `RuntimeError: unexpected argument '--scope' found` — binary
  `target/release/mnemosyne.exe` does not support `--scope` required by
  `queries_heldout.jsonl`. No adapter code change fixes this; needs
  binary/runtime integration or benchmark script alignment.
- **adapter build/install verification**: `python -m pip install -e
  integrations/hermes-memory-provider/` passes; adapter entry point
  `mnemosyne-rust` registered.

## Benchmark harness

- **Direct execution of `.auto/measure.sh` fails on this box** — msys `bash`
  cannot resolve the `#!/bin/bash` mount when exec'd by the tool; invoke as
  `bash .auto/measure.sh`. The scripts themselves are builtins-only + python
  because the tool's spawn PATH lacks `rm`/`dirname`/`tail`.
- **p99 on this machine is stall-dominated** (identical code swings 1.2→3.2ms
  run to run), which is why the primary metric is p50. A quieter box would let
  p99 be the primary again.
