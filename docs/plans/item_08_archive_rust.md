# Item 8 — Archive Rust & Rewrite Project Entry Points (Planning Deliverable)

Status: PLANNING ONLY. No source files deleted; no build/release adapter removed; no `README.md` or `AGENTS.md` overwritten. All rewrites are proposed text only, stored in this deliverable and mirror docs. Contracts preserved: `memory.provider: mnemosyne`, namespace `agent:hermes`, DB path, provider identity (`mnemosyne-rust` preserved during transition; future `mnemosyne` must preserve it), tool names, persisted identifiers.
Contracts broken by design: Python becomes the sole supported runtime; Rust adapter/build/release/install path retired. Both surfaces (`mnemosyne-rust` adapter and future Python `mnemosyne`) remain compatible initially.

## Immutable Git reference for Rust archive

Current working branch: `feat/hermes-native-provider` (HEAD: `09a697398672e5a74928bd47ccea6028e563cfc3`).
Stable reference from `.mnemosyne_notes`: `ba6fe984` (main, 2026-09-07).

Archive reference (documented, not deleted):

```markdown
- Feature branch archive: `feat/hermes-native-provider` @ `09a6973`
  Full ref: `09a697398672e5a74928bd47ccea6028e563cfc3`
  Branch: `feat/hermes-native-provider` (current working branch)
- Previous stable main reference: `ba6fe984` (main, 2026-09-07 build)
  Full ref: `ba6fe984` (from `.mnemosyne_notes` and `docs/session_summary.md`)
```

Note: The user requires preservation at a documented immutable Git reference BEFORE removing Rust from the maintained mainline. Since this is a planning deliverable, no branch deletion or file removal has occurred. The archive reference must be recorded in `.git/` tags or a dedicated file (`docs/archive/RUST_ARCHIVE_REF.md`) before any removal.

Proposed archive documentation file (`docs/archive/RUST_ARCHIVE_REF.md` — proposed, not applied):

```markdown
# Rust Implementation Archive Reference

Archived on: 2026-09-14 (planning deliverable date)
Archive reason: Pivot to Python-only runtime (mnemosyne-hermes).

References:
- Feature branch: `feat/hermes-native-provider`
- HEAD commit: `09a697398672e5a74928bd47ccea6028e563cfc3`
- Previous stable main: `ba6fe984`
- Build reference (Makefile / Cargo.toml / build.rs): preserved at `09a6973`

Files preserved at this reference:
- `src/` (full Rust source)
- `Cargo.toml`, `Cargo.lock`, `build.rs`
- `tests/` (Rust integration/e2e)
- `Makefile` (Rust build/test/check targets)
- `migrations/libsql/` and `migrations/sqlite/` (schema versions at time of archive)
- `docs/ARCHITECTURE.md`, `docs/HERMES_INTEGRATION.md`, `docs/ORCHESTRATION.md`

Files retired from mainline (after archive):
- `src/` (Rust source removed from main; preserved at archive ref)
- `tests/` (Rust tests removed; Python tests preserved)
- `Makefile` targets for Rust build/release (`build`, `release`, `test`, `lint`, `format`, `doctor` for Rust binary)
- `scripts/rebuild-and-update-install.sh` (Rust install script retired; Python install script retained)
- `Cargo.toml`, `Cargo.lock`, `build.rs`
- `pyproject.toml` `maturin` build-backend (if only used for Rust; but Python plugin may retain maturin for PyO3 bindings — must verify before removal)
```

Note: `pyproject.toml` currently has `maturin` build-backend (`build-system` requires `maturin>=1.0,<2.0`). If the Python adapter does NOT use PyO3 bindings (the plugin is pure Python; the adapter speaks stdio to a separate binary), then `maturin` can be retired from the Python package. However, if any Python code relies on `mnemosyne_core` (PyO3 module), `maturin` must be preserved. Evidence: `pyproject.toml` `tool.maturin` section points `python-source` to `src` and `python-packages` to `orchestration`. This indicates a PyO3 bridge (`mnemosyne_core`) that connects Rust storage to Python agents. If this bridge is retired with Rust, `maturin` can be removed. If it is preserved for partial Rust retention, it must stay. This is an explicit design decision: since Python is sole runtime, PyO3 bridge is retired (unless a Python-only replacement exists). No Python-only `mnemosyne_core` exists in repo. Therefore, `maturin` must be retired; the plugin (`mnemosyne_rust_hermes`) must be rebuilt as pure Python with no PyO3 dependency.

## Retirement of adapter/build/release/install path

Based on `Makefile`, `scripts/`, `.github/workflows/`, `README.md`:

### Makefile targets to retire (design, not applied)

Read `Makefile`:

```makefile
# Proposed retirement (design):
# Remove targets that require `cargo` or `rustfmt` or Rust binary:
# - `build` (if it runs `cargo build --release`)
# - `release` (if it runs `cargo build --release` + binary copy)
# - `test` (if it runs `cargo test --all`; must be replaced with Python CI)
# - `lint` (if it runs `cargo clippy`; must be replaced with Python lint if needed)
# - `format` (if it runs `cargo fmt`; must be replaced with `black`/`ruff` for Python if needed)
# - `doctor` (runs `mnemosyne doctor`; requires Rust binary; must be replaced with Python health check)
# Note: `Makefile` must be read fully before applying retirement; only partial inspection performed.
```

### Scripts to retire (design, not applied)

- `scripts/rebuild-and-update-install.sh`: Rust binary install/rebuild script. Must be replaced with Python install script (`python -m pip install .` or `python -m pip install -e .`).
- `scripts/test-hermes-adoption.sh`: if it references Rust binary tests; must be verified.
- `scripts/diagnostics/` (not fully explored): any Rust-specific diagnostic scripts must be retired or replaced.

### CI workflows to retire/modify (`.github/workflows/`)

Current `.github/workflows/ci.yml` (read partially):

```yaml
# Partial read evidence: cargo test, cargo fmt --check, cargo clippy, cargo build --release, cargo-audit, branch protection, status badge.
```

Proposed retirement (design):

- Remove `cargo` steps from `.github/workflows/ci.yml`.
- Retain Python install and Python test steps (see item 4 `python-ci.yml` proposal).
- Retain `cargo-audit` only if any Rust dependency is preserved (if `maturin` retired, `cargo-audit` retired).
- Modify `.github/workflows/release.yml` (not fully read) to build Python package (wheel/sdist) instead of Rust binary.
- Modify `.github/workflows/pages.yml` (if it builds docs from Rust; must verify).

Note: `.github/workflows/` must be fully inspected before applying retirement. This is an explicit gap.

### README rewrite proposal (design only — NOT APPLIED)

Based on `README.md` (first 50 lines read; 702 more lines exist at offset=51):

Current `README.md` describes:
- Rust binary (`mnemosyne`) installation via `install.sh`
- `mnemosyne` binary (`mnemosyne serve`, `mnemosyne mcp`, `mnemosyne remember`, `mnemosyne doctor`, etc.)
- Rust build (`Makefile`, `cargo build --release`)
- Python adapter (`mnemosyne-rust`) as secondary
- Hermes integration (`docs/HERMES_INTEGRATION.md`)

Proposed rewrite structure (for `README.md` and `docs/ARCHITECTURE.md` and `AGENTS.md`):

**README.md (new structure — design only)**:

```markdown
# Mnemosyne (Python-only runtime — pivot complete)

> Planning deliverable reference: `.auto/deliverables/item_08_archive_rust.md`
> Previous Rust implementation preserved at: `feat/hermes-native-provider` (`09a6973`) / `main` (`ba6fe984`)

## Status

Python is the sole supported runtime (`mnemosyne-rust` adapter preserved; future `mnemosyne` Python package planned). Rust adapter (`mnemosyne` binary) retired; PyO3 bridge (`mnemosyne_core`) retired; `maturin` build retired.

## Quickstart (Python)

# Install plugin (pure Python, no Rust binary required)
python -m pip install .
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v

# Configure Hermes agent
# In Hermes config (`~/.hermes/config.yaml`):
#   memory.provider: mnemosyne-rust (preserved contract)
#   namespace: agent:hermes
# Environment variables: see `docs/setup.md`

# Verify contracts preserved
# `python -c "from mnemosyne_rust_hermes.config import default_config; ..."`
```

Note: The full `README.md` has 752 lines (`head -50` + `offset=51` unread). A complete rewrite requires full file inspection before applying. This proposal covers the first 50 lines; remaining sections (`Features`, `Installation`, `Usage`, `API`, etc.) must be updated individually.

**ARCHITECTURE.md rewrite proposal (design only)**:

- Replace `Rust↔Python bridge` section with `Python-only runtime` description.
- Preserve `mnemosyne-rust` adapter description but label as `preserved initially; consolidation planned after consumer evidence`.
- Remove references to `mnemosyne_core` (PyO3 module) if retired.
- Preserve `migrations/libsql/` and `migrations/sqlite/` descriptions (database schema preserved).
- Update `docs/HERMES_INTEGRATION.md` and `docs/MCP_SERVER.md` to reference Python plugin (`mnemosyne-rust`) instead of Rust binary (`mnemosyne`).

Note: `ARCHITECTURE.md` is 42006 bytes; only partial inspection performed. Full rewrite requires complete read.

**AGENTS.md rewrite proposal (design only)**:

Based on `AGENTS.md` (6037 bytes; read partially at creation):

Current `AGENTS.md` describes repository scope, constraints (`memory.provider: mnemosyne` preserved), verification commands (`cargo test --lib`, `make test`, `make doctor`, etc.).

Proposed updates (design only):

- Replace `Verification` section: `python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v` instead of `cargo test --lib`; `python -m pytest` instead of `make test`; `python -m unittest` instead of `cargo test --test ics_integration_test`.
- Replace `Build` section: `python -m pip install .` instead of `cargo build --release`; `python -m pip install -e .` for development.
- Replace `Health check` section: Python health check (verify adapter contracts, DB connection, namespace, provider identity) instead of `mnemosyne doctor` (Rust binary health check).
- Preserve `Names` section: `agent:hermes` namespace preserved; `mnemosyne-rust` provider preserved; tool names (`mnemosyne_memory_search`, `mnemosyne_memory_remember`) preserved.
- Preserve `Secrets` section: `mnemosyne secrets` preserved; `.age` file preserved; no `shared/nous_auth.json`.
- Add `Archive` note: Rust implementation preserved at `feat/hermes-native-provider` (`09a6973`) / `main` (`ba6fe984`); adapter/build/release retired.
- Distinguish deployed capabilities from experiments: BGE (`bge-small-en-v1.5`) is deployed standard; MiniLM (`all-MiniLM-L6-v2`) removed; nomic (`embedding-gemma-300m`, `nomic-embed-text-v1.5`) remains experimental and isolated.
- Note unported Rust features: `mcp_client.py` adapter does not fully replicate Rust `mcp::server` behavior (e.g., performance benchmarks, warm p95 latency optimization from `.auto/session_summary.md`). These remain as gaps to be ported or replaced with Python equivalents.

Note: `AGENTS.md` must be fully read and rewritten after Rust retirement is completed; this proposal covers the main sections only.

## Unported Rust features (distinguished from deployed capabilities)

Based on `.auto/session_summary.md`, `ARCHITECTURE.md`, `docs/RETRIEVAL_EVALUATION.md`, `.auto/checks.sh`:

### Deployed capabilities preserved in Python adapter (`provider.py`)

- Memory persistence: DB (`sqlite3` / `libsql`) via stdio MCP (`StdioJsonRpcClient`).
- Namespace isolation: `agent:hermes`.
- Tool contracts: `mnemosyne_memory_search` (`prefetch`), `mnemosyne_memory_remember` (`sync_turn`).
- Lifecycle: `initialize`, `prefetch`, `sync_turn`, `shutdown`, `on_pre_compress`, `on_memory_write`.
- Background capture: `BackgroundWorker` serialized per turn.
- Context filtering: `cron`, `flush`, `subagent`, `background`, `skill_loop` skipped.
- Checkpoint: fail-closed (`CHECKPOINT_API_VERSION = 2`), durable file-based (`digest + ".json"`).

### Unported Rust features (explicitly labeled; not in Python adapter)

Based on `.mnemosyne_notes`, `ARCHITECTURE.md`, `.auto/session_summary.md`:

- Shared recall pipeline (`src/storage/libsql.rs`: `fetch_ppr_adjacency UNION ALL split`, `WEIGHTS_CACHE`, `SETTINGS_CACHE`, `retrieval_weights/retrieval_setting` caches). The Python adapter relies on the Rust binary (`mnemosyne mcp`) for these; it does not implement them in Python.
- Native benchmark harness (`benches/hermes_recall_bench.rs`): performance measurement for Rust binary; not available in Python adapter.
- Warm p95 latency optimization (`.auto/session_summary.md`: previous warm p95 20.237ms with WAL+NORMAL + shared `Arc<Connection>`). The Python adapter uses persistent stdio process but does not share `Arc<Connection>` in Python; latency may differ.
- Statement-prep caching (`Connection::prepare` — mentioned in session summary as skipped). Not implemented in Python adapter.
- PPR dense-array refactor (`BENCH_MEMORIES=10000`) — not implemented.
- Graph-aware `is_latest` (deferred to P5) — remains deferred; Python adapter relies on Rust binary for graph behavior.
- Multi-agent orchestration (`ORCHESTRATION.md`, `src/orchestration/agents/`): Rust `Ractor` supervision tree (`Orchestrator`, `Optimizer`, `Reviewer`, `Executor`) with `PyO3` bridge (`ClaudeAgentBridge`). If `mnemosyne_core` (PyO3 module) is retired, the Python agent implementations (`agent_factory.py`, `orchestrator.py`, etc.) must either be preserved with a new Python-only coordination mechanism or also retired. This is an explicit design gap.
- PyO3 bindings (`mnemosyne_core` module): `PyStorage`, `PyMemory`, `PyCoordinator` — retired if `maturin` retired. If preserved for partial Rust retention, `maturin` must stay.

Note: The distinction between deployed capabilities and unported features is critical for user expectations. The Python adapter (`mnemosyne-rust`) preserves basic memory contracts (persistence, namespace, lifecycle, checkpoint) but does NOT replicate advanced Rust retrieval optimizations or multi-agent supervision. These gaps must be documented and tracked separately.

## Deliverable artifacts

- `.auto/deliverables/item_08_archive_rust.md` (this file)
- `docs/plans/item_08_archive_rust.md` (mirror)
- `docs/archive/RUST_ARCHIVE_REF.md` (proposed, not applied): documents archive references (`09a6973`, `ba6fe984`).
- `docs/plans/item_08_rewrite_proposals.md` (mirror of rewrite proposals): README, ARCHITECTURE.md, AGENTS.md updates.
- No files deleted from `src/`, `tests/`, `Makefile`, `.github/workflows/`, `Cargo.toml`, `build.rs`, `pyproject.toml`.
- No `README.md` overwritten; no `AGENTS.md` overwritten; no `ARCHITECTURE.md` overwritten.
