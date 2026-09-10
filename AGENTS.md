# mnemosyne-hermes agent contract

## Operating Standard

- Apply `C:\Users\juanm\Documents\GitHub\Vibe Coding Rules 10.md` (V10) as the repository operating standard; read it in full before substantive work.
- This file is the nearest-owning contract. It refines the parent policy with repository-specific facts and cannot weaken a mandatory parent rule; conflicts resolve to the parent.

## Scope and Ownership

- Root: `Cargo.toml`, `build.rs`, `Makefile` (build/test/check/lint/format/doctor targets), `test-all.sh`, `install.sh`, `pyproject.toml`/`requirements.txt` (optional Python feature), `migrations/` (libsql and sqlite schemas).
- `src/` owns the implementation: `mcp/` (MCP server surface), `cli/` (CLI commands incl. `serve`, `mcp`, `secrets`, `import`, `ics`/`edit`), `storage/` (LibSQL/SQLite persistence), `embeddings/`, `orchestration/` and `agents/` (Ractor multi-agent system), `ics/` (collaborative editor), `tui/`, `api/`, `rpc/`, `coordination/` (Iroh P2P), `python_bindings/` (optional, off by default), `bin/`, `services/`, `evolution/`, `evaluation/`.
- `tests/` owns integration/e2e/stress suites (`ics_integration_test`, `tests/e2e/`, `tests/manual/`); `benches/` and `benchmark/` own performance harnesses.
- `scripts/` owns build/install/test automation (`rebuild-and-update-install.sh`, `build-and-install.sh`, `test-hermes-adoption.sh`, `test-server.sh`, diagnostics/testing subdirs).
- `docs/` owns architecture, feature, and operations documentation; top-level `.md` files (README, ARCHITECTURE, AGENT_GUIDE, SECRETS_MANAGEMENT, MCP_SERVER, TROUBLESHOOTING, etc.) are the entry points — read them rather than duplicating their content here.
- `proto/` owns gRPC/protobuf contracts; changing them requires checking all consumers.

## Constraints

- Dual-surface product: the same core is exposed as a Hermes server (`mnemosyne serve`) and an MCP stdio server (`mnemosyne mcp`). Changes to memory/retrieval behavior must keep both surfaces and the Hermes MCP stdio contract intact.
- Local-first and keyless by design: memory must work without cloud API keys or OS keyrings; graceful degradation is required, not optional. Preserve the keyless verification path (e.g. `mnemosyne remember --no-enrich` with API keys unset).
- Secrets never live in the repo. Use the built-in secret manager (`mnemosyne secrets init/set/list`) or the environment; secrets are age-encrypted at `~/.config/mnemosyne/secrets.age`. Never commit `.env*`, connection configs, keys, or tokens; `mnemosyne secrets list` prints names only.
- The Python/PyO3 feature is optional and off by default; pure Rust builds must not require a Python toolchain (`cargo build --release` works standalone). Keep `pyproject.toml`/`requirements.txt` changes consistent with maturin.
- Storage is local LibSQL/SQLite with vector search, FTS5, and graph links; migrations in `migrations/` are part of any schema change. User memory data is private — never send it to external services unless the flow already does so and the change preserves consent/privacy behavior (privacy-preserving evaluation, hashed task IDs).
- Installed-binary flow: development installs go to `~/.local/bin` via `scripts/rebuild-and-update-install.sh`; the Makefile `doctor` target expects a built binary (cargo bin dir or `target/release`).
- Git: work on feature/fix branches; do not commit directly to `main`. Use descriptive commit messages describing the work, not the tool. Do not attribute commits to AI unless explicitly requested.
- Namespaces (e.g. `agent:hermes`) and import/export paths (`mnemosyne import --from ...`) are user-facing contracts; preserve them across refactors.
- Multi-agent orchestration (Ractor actors: Orchestrator, Optimizer, Reviewer, Executor) has quality gates, deadlock handling, and event persistence; changes there must keep the audit trail and work-queue semantics. The Reviewer's LLM enhancement is an optional enhancement, not a required dependency.
- P2P peer coordination (Iroh) and the evaluation/evolution subsystems have config surfaces (`evolution-config.example.toml`); keep example configs in sync with schema changes.

## Verification

- Fast unit tests: `cargo test --lib`
- Full suite: `make test` (runs `cargo test --all`), or `./test-all.sh` (`--skip-llm` skips LLM-dependent tests)
- ICS integration: `cargo test --test ics_integration_test`
- Compile check: `make check` · Lint: `make lint` · Format: `make format`
- Health check after install: `make doctor` (runs `mnemosyne doctor`; requires a built binary)
- Build: `cargo build --release` (pure Rust); dev install: `./scripts/rebuild-and-update-install.sh`; production: `./scripts/rebuild-and-update-install.sh --full-release`
- All commands above are evidenced in `Makefile`, `scripts/`, and `tests/`. There is no browser/UI harness for the TUI/ICS — exercise `mnemosyne edit` / `mnemosyne ics` interactively and report what was actually exercised.
- LLM-dependent tests need a configured `ANTHROPIC_API_KEY` via the secret manager or environment; without it, use `./test-all.sh --skip-llm` and disclose the gap.

## Documentation index

- Entry points: `README.md` (features, quickstart), `ARCHITECTURE.md`, `AGENT_GUIDE.md`, `SECRETS_MANAGEMENT.md`, `MCP_SERVER.md`, `QUICK_START.md`, `INSTALL.md`.
- Deep dives in `docs/`: `HERMES_INTEGRATION.md`, `HIERARCHICAL_MEMORY.md`, `REASONING_MEMORY.md`, `BOOTSTRAP.md`, plus `guides/ICS_INTEGRATION.md`; `docs/architecture/`, `docs/features/`, `docs/operations/`, `docs/security/` own their topics.
- Process docs: `CONTRIBUTING.md`, `TROUBLESHOOTING.md`, `MANUAL_TESTING.md`, `LLM_TESTING.md`, `HOOKS_TESTING.md`.
- `plans/` and `docs/plans/` hold working plans; treat them as historical context, not current contracts.

## Known gaps

- The repo carries many stale top-level status/plan documents (e.g. `PHASE_1_2_PLAN.md`, `TEST_RESULTS.md`, `REFACTORING_*.md`, `EVENT_BROADCASTING_STATUS.md`); treat them as snapshots, not living contracts, and do not update them unless a task explicitly targets them.
