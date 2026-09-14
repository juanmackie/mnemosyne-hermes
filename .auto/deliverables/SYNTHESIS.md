# Pivot Synthesis — All 8 Ordered Items (Planning Deliverable)

Status: COMPLETE (planning only; no deployed system changed; no database modified; no commits to main beyond documentation/reports; no destructive operations performed).

Reference goal: `.pi/goals/active_goal_2026091418240956_mu0z87do-nne63k.md`
Reference plan: `.pi/plan/` (updated during execution).
Archive reference for Rust: `feat/hermes-native-provider` (`09a697398672e5a74928bd47ccea6028e563cfc3`) / previous stable `main` (`ba6fe984`).

## Evidence policy

Every item reports:
- What evidence exists (file paths, lines, exit codes, actual content snippets).
- What is missing (explicitly labeled `unknown` or `unconfirmed`).
- What is proposed (design only; no source modifications applied unless explicitly noted).
- What contracts are preserved (`memory.provider`, namespace, DB path, provider identity, tool names, persisted identifiers).
- What gaps remain (historical events, missing upstream references, missing DB files on deployed host).

No secret content (keys, tokens, `.age` passphrases) appears in any deliverable file.
No past events are manufactured; historical gaps remain explicitly unknown.

## Batch evidence (subagent exploration results preserved)

- Subagent `d3cd44c1` (scout, item 1 — backup/auth): confirmed `nous_auth.json` absent; no SQLite DB in repo; Sept 13 backup unconfirmed; correct auth-file is `~/.config/mnemosyne/secrets.age` + `mnemosyne secrets`.
- Subagent `1bc8ba40` (scout, item 2 — Python baseline): plugin files listed; `pyproject.toml` v0.1.0; no `mnemosyne-memory 3.15.1` reference; only patch `.auto/evidence/link_type_fix.patch`; container digest / Hermes binary version unknown.
- Subagent `5cb58402` (scout, item 3 — embedding/BGE): `src/config.rs` references (`bge-small-en-v1.5`, `all-MiniLM-L6-v2`, `embedding-gemma-300m`, `nomic`); MiniLM override at `local.rs` 139-140; BGE preset at 384 dims (`line 215`, `237`); `.auto/unified_audit.json` has no embedding conflict entries.

Direct evidence (parent exploration):
- `README.md` (first 50 lines): Rust binary install (`install.sh`), `mnemosyne` binary commands, Python adapter secondary.
- `ARCHITECTURE.md` (partial): Rust↔Python `PyO3` bridge (`mnemosyne_core`); `maturin` build-backend; Python agent implementations (`agent_factory.py`, `orchestrator.py`, etc.).
- `.auto/checks.sh`: Rust tests (`cargo test --release --lib storage::`, `mcp::`); `rustfmt`; `cargo clippy`; `cargo build --release`.
- `.auto/evaluate.py`: evaluation harness (`corpus.jsonl`, `eval_dev.jsonl`, `eval_heldout_a.jsonl`, `eval_heldout_b.jsonl`); metrics `hit1`, `hit5`, `mrr`, `latency_p95`, `latency_p99`.
- `.auto/evaluate_mcp.py`: MCP stdio evaluation; includes `mcp_latency_overhead`.
- `.mnemosyne_notes`: previous build `ba6fe984` (main, 2026-09-07).
- `docs/MEMORY_INTEGRITY.md`: audit contracts (`update`, `supersede`, `BackupCreated`); `OrphanRepair`; maintenance reports; migration `027` creates fact relation.
- `provider.py`: adapter contracts (provider id `mnemosyne-rust`, namespace `agent:hermes`, checkpoint API v2, `BackgroundWorker`, context skip list, lifecycle contracts).
- `.auto/unified_audit.json`: `db_audit_trail_rows`: 2; `memory_evidence_rows`: 0; `claims_linked_to_memory_evidence`: false.
- `.auto/evidence/claim_001.json`: `sha256` empty (`e3b0c44298fc...`); `db_criteria` shows `S4_memory_evidence`: 11 but `S5_audit_trail`: 3; timestamp `1699999999` stale; contradiction with `.auto/unified_audit.json` (`memory_evidence_rows`: 0).
- `.github/workflows/` (`ci.yml`): Rust CI; no Python CI.
- `docs/plans/` (existing): `PHASE_A_DSPY_COMPLETION.md` and others (historical context only; not updated).

## Ordered item results

### Item 1 — Restore/verify backups (complete, design only)

Deliverable: `.auto/deliverables/item_01_backup_auth.md` + `docs/plans/item_01_backup_auth_fix.md`
Evidence: `nous_auth.json` absent; no DB file; Sept 13 backup unconfirmed; auth-file = `~/.config/mnemosyne/secrets.age`.
Action: Replaced stale auth-file reference with `secrets.age` contract; proposed SQLite backup script (`TRUNCATE` checkpoint, `.backup`, integrity check, isolated restore test, representative record count verification).
Gap: Backup file not found; DB file not present; drift watchdog script name/path unknown; must verify on deployed host.
Contract preserved: DB location (`MNEMOSYNE_DB_PATH`), namespace (`agent:hermes`), provider identity (`mnemosyne-rust` / `mnemosyne`), tool names (`mnemosyne_memory_search`, `mnemosyne_memory_remember`).

### Item 2 — Capture Python baseline (complete, design only)

Deliverable: `.auto/deliverables/item_02_python_baseline.md` + `docs/plans/item_02_python_baseline.md`
Evidence: Plugin files (`provider.py`, `mcp_client.py`, `worker.py`, `config.py`, `contexts.py`, `tests/test_provider.py`, `README.md`, `pyproject.toml` v0.1.0). No `mnemosyne-memory 3.15.1` reference; only patch `.auto/evidence/link_type_fix.patch`; container digest / binary sha256 unknown.
Action: Proposed `baseline/` directory structure (`upstream/`, `patches/`, `deploy/sanitized_example/`, `scripts/verify_baseline_install.sh`). Clean-install verification plan proposed; not executed.
Gap: Upstream source missing; binary version/digest unknown; deployed behavior reproduction blocked until binary available.
Contract preserved: Provider id (`mnemosyne-rust`), namespace (`agent:hermes`), DB path (`MNEMOSYNE_DB_PATH`), entry point (`mnemosyne-rust`), environment variable mapping (1:1 to `ProviderConfig` fields).
Sanitized deploy example: excludes `.env`, DB, `.age`, caches, model caches, private memory; includes `.env.example`, `provider.json.example`, `README_SANITIZED.md`, `patches/`, `scripts/`.

### Item 3 — Standardize embeddings (BGE) (complete, design only)

Deliverable: `.auto/deliverables/item_03_embedding_bge.md` + `docs/plans/item_03_embedding_bge.md`
Evidence: `src/config.rs` (`EmbeddingConfig` names: `bge-small-en-v1.5`, `all-MiniLM-L6-v2`, `all-MiniLM-L12-v2`, `embedding-gemma-300m`, `nomic-embed-text-v1.5`); default `embedding-gemma-300m` (`MNEMOSYNE_EMBEDDING_MODEL`); MiniLM override (`local.rs` 139-140); BGE preset (`line 215`, 384 dims); `.auto/unified_audit.json` no embedding conflicts; plugin (`provider.py`) no model override; `docs/EMBEDDING_CANDIDATE_EVALUATION.md` baseline fp32 nomic (confirms model disagreement).
Action: Proposed removing MiniLM mapping (`local.rs`); setting default `bge-small-en-v1.5` (`config.rs`); enforcing identity (`effective_identity()`); rejecting incompatible vectors (`model_conflict()` compares identity, not just dimensions); rebuild plan (pause writers, backup DB, rebuild with BGE, validate identity, resume); nomic isolation (separate branch/config, not in production preset).
Gap: `fastembed` version pinned 5.2.0 (must verify installation); BGE ONNX mapping availability unknown; deployed DB embeddings unknown (DB not in repo); audit table embedding-change events unknown.
Contract preserved: Namespace (`agent:hermes`), DB path, provider identity (`mnemosyne-rust` / `mnemosyne`), tool names, namespace, DB path. Model standardization changes only embedding identity; no namespace or provider id change.

### Item 4 — Python integration tested product (complete, design only)

Deliverable: `.auto/deliverables/item_04_python_tested.md` + `docs/plans/item_04_python_tested.md`
Evidence: Plugin contracts (`provider.py`: provider id, namespace, DB path, lifecycle, checkpoint v2, `BackgroundWorker`, context skip list, `StdioJsonRpcClient` persistent stdio); `tests/test_provider.py` (basic contract tests); `.github/workflows/ci.yml` (no Python CI); `.auto/checks.sh` (Rust tests only); proposal `.github/workflows/python-ci.yml` (not applied); proposed test design (not executed).
Action: Proposed Python CI (`.github/workflows/python-ci.yml`); proposed lifecycle test contracts (`initialize` no capture; `prefetch` unfenced; `sync_turn` serialized; `shutdown` idempotent; `on_pre_compress` fail-closed; `on_memory_write` no duplication); proposed concurrency/retry design; proposed gateway/dashboard concurrency (separate from Python adapter; relies on Rust binary); proposed background capture / skipped contexts / shutdown/restart tests.
Gap: Whether `tests/test_provider.py` covers full lifecycle: unknown; gateway/dashboard concurrency: unknown; retry behavior: unknown; shutdown/restart DB reconnection: unknown; compression with queued captures: unknown (see item 5); Python CI not executed.
Contract preserved: All adapter contracts preserved; both surfaces (`mnemosyne-rust` adapter and future `mnemosyne` Python server) remain compatible initially; no DB migration; no provider identity change.

### Item 5 — Compression durability & audit tracking repair (complete, design only)

Deliverable: `.auto/deliverables/item_05_compression_audit.md` + `docs/plans/item_05_compression_audit.md`
Evidence: `provider.py` (`on_pre_compress()` fail-closed, `_atomic_write()` fsynced, `CHECKPOINT_API_VERSION = 2`, idempotent for same digest, raises `CheckpointError`); `BackgroundWorker` (no public drain method); `.auto/unified_audit.json` (`db_audit_trail_rows`: 2, `memory_evidence_rows`: 0, `claims_linked_to_memory_evidence`: false, `audit_log_growing`: true but minimal); `.auto/evidence/claim_001.json` (stale timestamp `1699999999`, empty `sha256`, contradiction: `S4_memory_evidence`: 11 vs unified audit `0`); `docs/MEMORY_INTEGRITY.md` (audit contracts: `update`, `supersede`, `BackupCreated`; `OrphanRepair`; maintenance reports); `migrations/libsql/027_...sql` (fact relation, maintenance constraint).
Action: Proposed drain/checkpoint integration (add `has_queued()` / `drain()` to `BackgroundWorker` or expose state before `on_pre_compress()`); proposed checkpoint audit event insertion (`event_type`: `checkpoint`, digest, path, provider, timestamp); proposed mutation audit wiring (`on_memory_write()` insert audit row, not just internal record); proposed maintenance audit wiring (`OrphanRepair` exact projection counts); proposed rollback/retry test design (corrupt directory, verify `CheckpointError`, restore permissions, verify idempotent return, verify file intact).
Gap: `audit_events` table existence in deployed DB: unknown; `BackgroundWorker` queued captures during compression: unknown (no drain mechanism); audit wiring for mutations/maintenance: unknown (evidence suggests broken/unimplemented); rollback/retry tested against real disk failures: unknown; historical audit content: unknown (claim_001 stale; unified audit minimal); historical checkpoint files: unknown.
No audit records manufactured; no historical events invented; no achievement counts fabricated.
Contract preserved: Checkpoint API version 2 preserved; `provider.py` contracts unchanged (only proposed additions); DB schema preserved (migrations unchanged); namespace (`agent:hermes`), DB path, provider identity preserved.

### Item 6 — Trustworthy retrieval baseline (complete, design only)

Deliverable: `.auto/deliverables/item_06_retrieval_baseline.md` + `docs/plans/item_06_retrieval_baseline.md`
Evidence: `.auto/evaluate.py` (metrics: `hit1`, `hit5`, `mrr`, `latency_p95`, `p99`; isolation: `query_db` symlink/copy); `.auto/evaluate_mcp.py` (`mcp_latency_overhead`); `.auto/eval_*.jsonl` (dataset files); `.auto/membench/` (benchmark harness); `.auto/unified_audit.json` (no evaluation results stored); `.auto/session_summary.md` (previous warm p95 20.237ms; `datatype mismatch` in `hybrid_search` pre-existing; `.auto/evaluate.py` not executed in this session; no previous `.auto/eval_result_YYYY-MM-DD.json` exists); `docs/RETRIEVAL_EVALUATION.md` (evaluation methodology); `docs/EMBEDDING_CANDIDATE_EVALUATION.md` (baseline nomic; confirms model disagreement — see item 3).
Action: Proposed dataset identity (`corpus_sha256`, `eval_dev_sha256`, etc.); proposed code version identity (`git_sha1`, `python_version`, `plugin_version`, `embedding_model_identity`, `mcp_version`); proposed model identity verification (before evaluation: `MNEMOSYNE_EMBEDDING_MODEL` or query Rust binary via MCP); proposed scoring settings recording (`ranking_weights`, `rrf_k`, `hotness_weight`, `ppr_weight`); proposed variant comparison sequence (baseline → RRF → hotness → PPR → combined; only after individual variants show improvement); proposed evaluation output format (`.auto/eval_result_YYYY-MM-DD.json`); proposed persistence of results (each run writes JSON with identity, settings, metrics, latency).
Gap: Previous evaluation results with full dataset identity and model identity: unknown; `docs/RETRIEVAL_EVALUATION.md` full content: not fully read; default retrieval settings file (`default_retrieval_settings.json`): missing (must create or derive from `.auto/evaluate.py`); `fastembed` version on deployed host: unknown; evaluation against corrected Python stack: not executed (requires item 3 standardization first); benchmark (`.auto/measure.sh`) dataset identity recording: unknown; warm latency (`.auto/measure_mem.sh`) model identity recording: unknown.
No evaluation executed; no metrics fabricated; no dataset identity invented.
Contract preserved: Namespace (`agent:hermes`), DB path, provider identity (`mnemosyne-rust` / `mnemosyne`), evaluation isolation (`query_db` symlink/copy), dataset identity preserved; no DB migration to Rust; no nomic switch applied; no unrelated infrastructure overhaul.

### Item 7 — Deployment reproducible and recoverable (complete, design only)

Deliverable: `.auto/deliverables/item_07_deploy_repro.md` + `docs/plans/item_07_deploy_repro.md`
Evidence: `pyproject.toml` (`maturin>=1.0,<2.0`; `setuptools>=61`; `requires-python>=3.11`); `requirements.txt` (`claude-agent-sdk`, `asyncio`, `rich`); `Cargo.toml` / `Makefile` / `scripts/rebuild-and-update-install.sh` (Rust build/release/install); `.github/workflows/ci.yml` (Rust CI); `docs/HERMES_INTEGRATION.md` (Hermes integration); `.mnemosyne_notes` (`ba6fe984` build reference); `.auto/evidence/claim_001.json` (empty sha256 — no digest); plugin adapter (`provider.py`); `.auto/session_summary.md` (previous warm p95 20.237ms; `.auto/measure.sh` blocked by `datatype mismatch` pre-existing).
Action: Proposed package structure (`baseline/` directory: `upstream/`, `patches/`, `deploy/sanitized_example/`, `scripts/`). Proposed TrueNAS redeploy procedure (`redeploy_trueNAS.sh`: pre-update backup with `TRUNCATE` checkpoint, DB integrity check, service stop, new image deploy, smoke checks, rollback to previous artifact). Proposed smoke checks (binary responds, adapter registers, DB writable, namespace/DB/config contracts preserved, checkpoint directory writable, basic tool call responds, audit table exists/grows after mutation). Proposed rollback (`rollback.sh`: verify backup integrity, restore DB atomically, restart previous image/service, verify rollback integrity, verify representative records). Proposed separate tracking (`separate_tracking` JSON design) for PID pressure and unrelated failing cron integrations (investigation required; no action taken in this deliverable).
Gap: Container digest / binary sha256: unknown (`sha256` empty in claim_001); deployed binary version (`mnemosyne --version`): unknown; `MNEMOSYNE_BIN` binary on deployed host: unknown; TrueNAS deployment details: unknown; `docs/OPERATIONS.md`: not fully read; rollback to previous artifact: not tested; smoke checks on deployed host: not executed; PID/crons: tracked separately; `.github/workflows/release.yml`: not fully read (must inspect before retirement); `Makefile`: partial inspection only.
No package built; no image redeployed; no smoke checks executed on deployed host; no rollback executed.
Contract preserved: Package design preserves plugin (`mnemosyne-rust` adapter) and future `mnemosyne` Python server; DB path (`MNEMOSYNE_DB_PATH`), namespace (`agent:hermes`), provider identity (`mnemosyne-rust` / `mnemosyne`), pinned dependencies (`pyproject.toml` + `requirements.txt`), schema versions (`migrations/libsql/` + `sqlite/`); rollback restores previous DB state (with WAL included via `TRUNCATE` checkpoint) without data loss.

### Item 8 — Archive Rust & rewrite entry points (complete, design only)

Deliverable: `.auto/deliverables/item_08_archive_rust.md` + `docs/plans/item_08_archive_rust.md`
Evidence: Current HEAD (`feat/hermes-native-provider`: `09a697398672e5a74928bd47ccea6028e563cfc3`); previous stable main (`ba6fe984`: `.mnemosyne_notes`); `README.md` (752 lines total; partial read: Rust binary install, `mnemosyne` commands, adapter secondary); `ARCHITECTURE.md` (42006 bytes; partial: `PyO3` bridge, `maturin`, Python agent implementations); `AGENTS.md` (6037 bytes; partial: contracts, verification commands); `.github/workflows/` (`ci.yml`: Rust CI); `Makefile` (`build`, `test`, `lint`, `format`, `doctor` — Rust); `scripts/rebuild-and-update-install.sh` (Rust binary install/rebuild); `pyproject.toml` (`maturin` build-backend, `python-source` = `src`, `python-packages` = `orchestration`); `docs/HERMES_INTEGRATION.md` (Hermes integration reference); `docs/MCP_SERVER.md`; `docs/OPERATIONS.md` (not fully read); `docs/ARCHITECTURE.md` (not fully read); `.auto/session_summary.md` (warm p95 20.237ms; `.auto/measure.sh` blocked by `datatype mismatch`; `fetch_ppr_adjacency` split at `line 3855`; `WEIGHTS_CACHE` + `SETTINGS_CACHE` added); `docs/EMBEDDING_CANDIDATE_EVALUATION.md` (embedding disagreement); `.mnemosyne_notes` (`mnemosyne` binary build `ba6fe984`); `docs/plans/` (historical: `PHASE_A_DSPY_COMPLETION.md`, etc.).
Action: Proposed archive reference (`feat/hermes-native-provider` (`09a6973`) + `main` (`ba6fe984`)); proposed `docs/archive/RUST_ARCHIVE_REF.md` (archive reference documentation); proposed retirement (Makefile targets: `build`, `test`, `lint`, `format`, `doctor` if Rust-only; scripts: `rebuild-and-update-install.sh` retired; `.github/workflows/`: `ci.yml` Rust steps removed; Python CI added from item 4; `release.yml` must be inspected before retirement; `maturin` retired if `mnemosyne_core` PyO3 bridge retired); proposed rewrites (`README.md` new structure: Python-only quickstart, archive note, contract preservation, unported features; `ARCHITECTURE.md`: Python-only runtime description, preserved adapter contracts, PyO3 retirement note, unported features; `AGENTS.md`: updated verification commands (`python -m unittest`, `python -m pytest`, `python -m pip install`); archive note; deployed vs unported distinction; contract preservation; separate tracking reference). Proposed unported Rust features list (shared recall pipeline `fetch_ppr_adjacency`, `WEIGHTS_CACHE`, `SETTINGS_CACHE`; benchmark harness; warm p95 latency optimization; statement-prep caching; PPR dense-array refactor; graph-aware `is_latest`; multi-agent `Ractor` supervision with `PyO3` bridge; `mnemosyne_core` module). Note: `README.md` (752 lines) and `ARCHITECTURE.md` (42006 bytes) and `AGENTS.md` (6037 bytes) are only partially read; full rewrites require complete inspection before applying.
Gap: `README.md` remaining 702 lines: not fully read; `ARCHITECTURE.md` remaining content: not fully read; `docs/OPERATIONS.md`: not fully read; `docs/HERMES_INTEGRATION.md`: not fully read (only referenced); `.github/workflows/release.yml`: not fully read; `.github/workflows/pages.yml`: not fully read; `Makefile` full content: not fully read; `scripts/test-hermes-adoption.sh`: not fully read; `docs/MCP_SERVER.md`: not fully read; `docs/ARCHITECTURE.md`: only partial inspection; `docs/ARCHITECTURE.md`: `Python-only` description must cover all architecture sections (multi-agent orchestration, storage, retrieval, evaluation, deployment); `docs/HERMES_INTEGRATION.md`: must update for Python-only plugin; `docs/MCP_SERVER.md`: must verify Python adapter contract matches MCP stdio contract.
No files deleted (`src/` count 420; `tests/` count 199); `Cargo.toml` exists; `.git` HEAD unchanged (`09a6973`); no source modifications applied; no `README.md` overwritten; no `AGENTS.md` overwritten; no `ARCHITECTURE.md` overwritten; no `Makefile` targets removed; no `.github/workflows/` files deleted; no `maturin` removal executed; no `pyproject.toml` modified.
Distinguished deployed capabilities (`memory.provider` contract, lifecycle, checkpoint v2, namespace, DB path, tool names) from experiments (`embedding-gemma-300m` / nomic — experimental; `all-MiniLM-L6-v2` — removed; `bge-small-en-v1.5` — standard after item 3) and unported Rust features (`fetch_ppr_adjacency`, `WEIGHTS_CACHE`, `SETTINGS_CACHE`, benchmark harness, warm p95 optimization, `PyO3` bridge, `mnemosyne_core`, `Ractor` supervision).

## Verification checklist (post-delivery checks performed in this session)

- [PASS] All 8 item deliverables exist (`.auto/deliverables/item_01_...` through `item_08_...`).
- [PASS] Mirror docs exist (`docs/plans/item_01_...` through `item_08_...`).
- [PASS] No source code deleted (`find src -type f` = 420; `find tests -type f` = 199).
- [PASS] `Cargo.toml` exists (Rust source not removed).
- [PASS] `.git` HEAD unchanged (`09a6973`).
- [PASS] No destructive DB operations (no `sqlite3` writes; no `.db` modifications; backup not overwritten; no restore executed).
- [PASS] No deployed service stopped/restarted (no `systemctl`, `docker`, `service` commands executed).
- [PASS] No secrets leaked (no `.age` content, no `ANTHROPIC_API_KEY`, no tokens, no `shared/nous_auth.json` invented).
- [PASS] No past events manufactured (audit gaps explicitly labeled `unknown`; claim_001 contradiction preserved; historical gaps not fabricated).
- [PASS] Contracts preserved (provider identity `mnemosyne-rust`, namespace `agent:hermes`, DB path `MNEMOSYNE_DB_PATH`, tool names, namespace, persisted identifiers).
- [PASS] Default settings preserved (Python sole runtime; both surfaces initially compatible; local memory keyless; enrichment optional; no DB migration to Rust; no nomic switch; no unrelated infrastructure overhaul).
- [PASS] Planning deliverable (no repository changes applied beyond documentation/reports; no deployment changes made).
- [PASS] Subagents used for batch exploration (3 subagents launched; 3 completed with evidence reports; results integrated into deliverables).
- [PASS] Workflow/tool integration (plan updated with 8 steps; goal created; subagent delegation used for independent exploration; direct work used for synthesis and file creation).

## Gaps and next actions (not completed in this planning deliverable — must be completed by owner before any deployment change)

1. **Backup verification execution** (item 1): Must locate deployed DB; execute `TRUNCATE` checkpoint; create fresh backup; restore into isolation; verify `integrity_check`; confirm September 13 backup or regenerate. Must verify `docs/OPERATIONS.md` backup procedure.
2. **Python baseline import verification** (item 2): Must fetch `mnemosyne-memory 3.15.1` upstream source; verify installed binary version (`mnemosyne --version` or `MNEMOSYNE_BIN --version`); verify container digest; execute clean-install verification script (`python -m unittest discover ...`); confirm adapter contracts in production.
3. **Embedding standardization execution** (item 3): Must verify `fastembed` version; confirm BGE ONNX mapping; query deployed DB embeddings; apply standardization; rebuild; validate identity; resume writers. Must isolate nomic experiments (separate branch/config).
4. **Python CI execution** (item 4): Must apply `.github/workflows/python-ci.yml`; run tests; verify lifecycle contracts in production; cover gateway/dashboard concurrency; cover background capture; cover retry; cover shutdown/restart.
5. **Audit tracking repair execution** (item 5): Must inspect `audit_events` table schema (verify existence); apply drain/checkpoint integration (if needed); wire mutations/maintenance; test rollback/retry; confirm no empty audit after operations.
6. **Retrieval evaluation execution** (item 6): Must apply BGE standardization first; create/default `default_retrieval_settings.json`; run `.auto/evaluate.py` with dataset identity; compare RRF/hotness/PPR individually; persist `.auto/eval_result_YYYY-MM-DD.json`.
7. **Deployment redeploy execution** (item 7): Must capture binary digest; document redeploy procedure; execute smoke checks on deployed host; test rollback; separate PID/crons tracking (if observed, create `.auto/evidence/` files).
8. **Rust retirement execution** (item 8): Must fully read `README.md` (remaining 702 lines), `ARCHITECTURE.md` (remaining content), `docs/OPERATIONS.md`, `docs/HERMES_INTEGRATION.md`, `.github/workflows/release.yml`, `.github/workflows/pages.yml`; apply archive reference (`docs/archive/RUST_ARCHIVE_REF.md`); remove Rust source/files after archive; rewrite docs; retire adapter/build/release; verify Python CI passes.

This planning deliverable completes the design, documentation, and evidence collection for all 8 ordered items. No deployed changes have been made. All contracts are preserved. All historical gaps remain explicitly unknown.
