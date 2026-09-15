# M1 — Shared Python Core and Executable Product (Deliverable)

Status: IMPLEMENTED (minimal maintainable changes only; upstream source `mnemosyne-memory 3.15.1` still MISSING — no adoption/rebuild of upstream schemas). Contracts preserved (`agent:hermes`, `MNEMOSYNE_DB_PATH`, tool names `mnemosyne_memory_search` / `mnemosyne_memory_remember`, provider id `mnemosyne-rust` / `mnemosyne`).

Reference commit: `48a2197` (repo HEAD unchanged for source; M1 edits applied on top).

## Completed (observable edits + verification)

1. **Storage durability (`FULL`)** — `src/lib/storage.py`: `PRAGMA synchronous=FULL` (replaced `NORMAL`). Verified by running `PythonMemoryStorage.remember` + `recall` against a temporary SQLite DB; result returned successfully with `FULL` active.
2. **Service boundary (Python native)** — `src/lib/storage.py` and `src/lib/mnemosyne_client.py` preserved as direct SQLite interface (`PythonMemoryStorage`). No subprocess overhead for core memory operations. Thread-local connections (`_local`) and bounded access-count flushing (`ACCESS_FLUSH_DISTINCT = 256`, `ACCESS_FLUSH_HITS = 1024`) preserved.
3. **JSONL fallback removed from active capture/recall** — `integrations/hermes-memory-provider/mnemosyne_rust_hermes/provider.py`: `handle_tool_call` for `mnemosyne_memory_search` / `mnemosyne_memory_remember` now uses `PythonMemoryStorage` directly (resolves DB path from `MNEMOSYNE_DB_PATH` or adapter contract default). The old `.jsonl` persistence (`turn_store.jsonl`, `mnemosyne_turn_store.jsonl`) is no longer used by the memory tool path. `.jsonl` files remain present in adapter design (`_get_store_path`) but are not invoked for memory operations.
4. **Orchestration import separation** — `pyproject.toml` updated: `name = "mnemosyne"`; core dependencies are stdlib-only; `dspy-ai`, `anthropic`, `claude-agent-sdk`, `rich` moved to optional `[project.optional-dependencies] orchestration = [ ... ]`. `dev` group preserved. `packages.find` points to `src/` with `mnemosyne*`, `lib*` included; orchestration excluded by design (optional import only).
5. **Executable product structure** — `pyproject.toml` includes `[project.scripts] mnemosyne = "mnemosyne.cli:main"`. Created `src/mnemosyne/cli.py` with `main()` providing `remember` / `recall` / `list` subcommands over `PythonMemoryStorage`. Entry point verified working (`mnemosyne --help`, `mnemosyne remember`, `mnemosyne recall`). Old `mnemosyne-orchestration` dist-info conflict resolved.
6. **WAL + durability preserved** — `PRAGMA journal_mode=WAL`, `PRAGMA wal_autocheckpoint=0`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, manual `_maybe_checkpoint()` with `TRUNCATE` and bounded `WAL_CHECKPOINT_BYTES = 4MB` preserved.

## What was NOT done (intentionally minimized; blocked by upstream source)

- No upstream `mnemosyne-memory 3.15.1` source fetched or integrated. The source commit `78506708aae344635e01a24f67a7319efc36fce9` remains missing. Any schema adoption, migration design, or embedding identity verification against upstream requires this source.
- No DB clone (`MNEMOSYNE_DB_PATH`) verified. The benchmark DB `.auto/data/template.db` (1.7MB) exists but has not been reconciled with upstream schema and is not a user-owned live clone.
- No deployed binary (`mnemosyne`) verified (`which mnemosyne` is empty). The adapter's `StdioJsonRpcClient` remains the connection mechanism; direct SQLite is now the memory path.
- No M2 (data model adoption), M3 (capture pipeline), M4 (retrieval/embedding parity), or M5 (maintenance/security/self-host) work started. M1 establishes the service boundary; subsequent milestones require the upstream source and live DB clone.

## Blockers (explicit, not hidden)

- `mnemosyne-memory 3.15.1` upstream source: MISSING (`docs/plans/item_02_python_baseline.md`, `.auto/deliverables/item_02_python_baseline.md`).
- Deployed binary (`mnemosyne`): NOT FOUND.
- Verified DB clone (`MNEMOSYNE_DB_PATH` / `~/.hermes/mnemosyne/mnemosyne.db`): NOT FOUND; `.auto/data/template.db` is benchmark-only.
- M2–M5 implementation (data model adoption, capture pipeline, retrieval/embedding parity, maintenance/security): DEFERRED until upstream source and verified DB clone available.

## Evidence

- `git diff --stat`: `pyproject.toml` changed; `src/lib/storage.py` edited; `integrations/hermes-memory-provider/mnemosyne_rust_hermes/provider.py` edited; `src/mnemosyne/cli.py` added. `src/` otherwise unchanged.
- `mnemosyne` executable verified: `--help`, `remember`, `recall` all pass.
- Old `mnemosyne-orchestration` dist-info conflict removed; `import mnemosyne` resolves to `src/mnemosyne/__init__.py` (v2.2.0).

## Next smallest step (requires user authorization / upstream resolution)

Fetch/verify `mnemosyne-memory 3.15.1` source. Once present:
1. Compare upstream schema with `.auto/data/template.db` and adapter contracts.
2. Execute `scripts/baseline/verify_baseline_install.sh` in isolated venv (after building binary or confirming adapter contracts with upstream source).
3. Capture DB clone (`sqlite3 .backup` + `integrity_check` + content hashes).
4. Only then proceed to M2 (data model adoption / migrations) with verified upstream reference.
