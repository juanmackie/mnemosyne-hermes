# Level-up TODO — full project review

- **Date:** 2026-09-27
- **Baseline reviewed:** `main @ a951105` (clean tree; initial findings)
- **Scope:** whole repository, CI history, and the local Hermes install that runs it
- **Status:** findings implemented incrementally; final Linux smoke, final review, and authorized main push are pending. P0-5 remains with the live Hermes agent; P5-2 is explicitly skipped; release publication has not been triggered.

## Where the project stands

What actually ships today:

1. **The Hermes provider.** `integrations/hermes-provider/` is a vendored copy of the provider from `mnemosyne-memory` 3.15.1 (by AxDSan), plus local patches. `./install.sh` installs it with the pinned engine, and `hermes mnemosyne doctor` checks it.
2. **`mnemosyne-lite`.** `src/mnemosyne_lite/` is the standalone SQLite keyword-search store with a CLI and an MCP stdio server. It is not a Hermes provider.
3. **Rust/orchestration are retired.** No `src/orchestration/` implementation ships; archive tags and `docs/archive/` preserve historical material.

Active docs describe the provider and lite surface; Rust-era material remains explicitly historical under `docs/archive/` and in the changelog.

Initial baseline evidence (captured at `a951105`, not current verification):

- Python CI has failed on all 10 runs since it was added on 2026-09-18, and every Pages deploy has failed too.
- `pytest tests -m 'not integration'`: 79 passed, 36 skipped, 6 deselected. The result is the same locally and in CI.
- Provider gates pass: `test_vendored_provider.py` 3/3 and `test_provider_loader.py` 3/3. The old Rust-adapter unittest suite reports 21 OK.
- 223 of the 250 tracked markdown files mention Rust-era concepts. 32 files have 97 broken relative links between them.
- The engine (`mnemosyne-memory` 3.15.1) and `hermes-agent` 0.19.0 are the newest releases on PyPI, so the pins are current.

Current verification limits:

- V10 was read before the resumed implementation work; the original baseline note that it was blocked is superseded.
- The local Windows host skips the real-symlink collision lane (WSL is absent). The earlier CI log (`36284065100`) is historical; the extended Linux/macOS smoke remains a final gate.
- No LLM-dependent lane was run; current tests are local/keyless and `test-all.sh --skip-llm` is the supported verification path.

---

## P0: main is red and new installs fail (do first)

- [x] **P0-1 Fix the notes check that fails CI.** (done 2026-09-27: line 5 is now a `#` comment, the three dead entries carry resolution lines, file sorted; `bash scripts/checks.sh` prints `ALL PASS`)
  - **Problem:** `.mnemosyne_notes:5` is a prose line ("Notes lifecycle (2026-09-19): …") with no tab-separated fields, and the file is not sorted. So `scripts/check_notes.sh` fails, which fails `scripts/checks.sh` and the CI "Repo gates" step.
  - **Also stale:** the three entries point at files that no longer exist (`Makefile+L29`, `README.md+L828`, `src/utils/retrieval.rs`).
  - **Fix:** make line 5 a `#` comment. Retire the three entries by adding resolution lines, as the file's own lifecycle rule requires. Sort the file (`LC_ALL=C sort`).
  - **Check:** `bash scripts/checks.sh` prints `ALL PASS`.

- [x] **P0-2 `doctor` fails on every fresh install.**
  - **Problem:** `_db_writable` (`integrations/hermes-provider/hermes_memory_provider/cli.py:177-185`) fails when the database's parent folder does not exist. Right after `./install.sh` it never exists, because the installer deliberately creates nothing.
  - **CI evidence:** `[FAIL] DB resolved + writable — …/hermes-home/mnemosyne/data/mnemosyne.db (parent …/mnemosyne/data does not exist)`
  - **Impact:** README, `QUICK_START.md` and `docs/AGENT_SETUP.md` all say `doctor` "must exit 0". AGENT_SETUP also tells agents to stop when a check fails. So every new user and every agent-driven setup stops at this step.
  - **Fix:** walk up to the nearest folder that exists, check it is writable, and report "will be created".
  - **Bookkeeping:** this file is vendored, so record the change as an amendment to patch P10 in `integrations/hermes-provider/PATCHES.md`, with a `# LOCAL PATCH:` marker. Then update the hash in `VENDORED_FROM.json`.
  - **Test:** add a unit test for the case where the folder has not been created yet.
  - **Check:** `bash scripts/smoke-hermes-onboarding.sh` passes in CI.
  - **Done:** `_db_writable` walks up to the nearest existing ancestor; regression tests `test_db_writable_fresh_install_parents_missing`, `test_db_writable_existing_file_and_parent`, `test_db_writable_unresolved_path_is_failure`. Recorded as a P10 amendment in `PATCHES.md`; manifest hashes/size/lines and vendored drift tests are updated. The earlier `5de2465` CI run passed; the extended collision/latency smoke must pass on the final pushed revision before this is considered fully verified.

- [x] **P0-3 Pages deploy fails on every push.**
  - **Problem:** Pages is not enabled on the repo (`deploy-pages` gets a 404). The site it would publish, `docs/index.html`, is upstream rand/mnemosyne's Rust marketing page, with 15 links to rand/mnemosyne.
  - **Fix:** delete `.github/workflows/pages.yml` until there is a real site. The alternative is to build a new site and enable Pages.
  - **Done (2026-09-27):** `.github/workflows/pages.yml` deleted. P3/P2-10 remove the stale `docs/index.html` site assets that it would have published.

- [x] **P0-4 `release.yml` builds a Rust binary.**
  - **Problem:** it runs `cargo build --release --bin mnemosyne` for four targets, so any `v*` tag push fails.
  - **Fix:** replace it with a Python release (see P5-6) or delete it.
  - **Done:** `.github/workflows/release.yml` was deleted when it still contained only the retired Rust/Cargo matrix. A new tag-only Python wheel pipeline now builds and install-tests both distributions before creating a GitHub Release; it does not publish a release on ordinary main pushes.

- [ ] **P0-5 Your own Hermes is not using the provider.** (Local machine, not the repo.)
  - `HERMES_HOME=%LOCALAPPDATA%\hermes`. Its `config.yaml` has `memory.provider: ''`, and it has no `plugins/mnemosyne`.
  - `~/.hermes/plugins/mnemosyne` links to `venv/Lib/site-packages/hermes_memory_provider/`. That is the unpatched upstream copy, not the vendored one.
  - The Hermes venv still has old editable installs:
    - `mnemosyne 2.4.0`: this repo under its old name, which is the collision the README warns about
    - `mnemosyne-hermes`: points at the retired `integrations/hermes`
    - `mnemosyne-orchestration`
    - `mnemosyne-rust-hermes`
  - The local `dist/` still holds a `mnemosyne-2.4.0` wheel under the old colliding name.
  - **Disposition:** this remains assigned to the user's live Hermes agent. That agent may remove the conflicting editable installs and run `integrations/hermes-provider/LIVE_VERIFICATION.md` after the repository changes land; retain the existing `dist/` wheel (deleting it is neither necessary for provider selection nor authorized here). Also see P5-1 (Windows).
  - **Status: handed off, not executed here.** The live Hermes agent owns the machine cleanup after this work lands, so no local venv/`dist/` mutation was done. The repo side is ready: `install.sh --uninstall` removes a symlink or an intact format-v1 installer copy, keeps data by default, refuses unverified copies and unsafe purge roots, and fails visibly if package removal fails; `docs/AGENT_SETUP.md` remains the agent-facing runbook.

## P1: code bugs

- [x] **P1-1 Recall can return stale results across threads (lite store).**
  - **Cause:** the recall cache `_recall_cache` lives on the shared storage object (`src/lib/storage.py:173`). Its validity check `_search_version` (`storage.py:657-665`) uses `PRAGMA data_version` and `total_changes` from the calling thread's own connection, and those numbers cannot be compared across connections.
  - **Reproduced:** thread A recalls "alpha". Thread B stores "alpha two". A new thread C recalls "alpha" and gets only `['alpha one']`, while `count()` returns 2.
  - **Fix:** make the cache per-thread, the same way `search_cache` already is. Add a cross-thread case to `tests/test_recall_freshness.py`.
  - **Done (2026-09-27):** `_recall_cache` is now a property backed by `self._local.recall_cache`, and `_conn()` clears it on reconnect (a new connection resets both numbers the version is built from, so an entry from the previous connection could otherwise match). `_flush_accesses` patches only its own thread's rows; other threads discard via `PRAGMA data_version`, which this commit moves. Regression test `test_recall_memo_is_per_thread`. Verified both ways against HEAD's `storage.py`: pre-fix a second thread's recall returned 1 stale row while `count()` returned 2; post-fix it returns 2.

- [x] **P1-2 `mnemosyne-lite backup` crashes with default settings.**
  - `cmd_backup` uses `args.db_path` directly (`src/mnemosyne_lite/cli.py:147`). Without `--db-path` or `MNEMOSYNE_DB_PATH` that value is `None`, so the command dies with `TypeError: stat: path should be string… not NoneType`.
  - **Fix:** use `_resolve_existing_db(args)`.
  - **Done (2026-09-27):** `cmd_backup` resolves through `_resolve_existing_db` and reports the missing store in one line. Regression tests `test_backup_without_db_path_reports_a_missing_store` and `test_backup_writes_a_usable_copy` in `tests/test_lite_cli.py`.

- [x] **P1-3 `bootstrap` categories are always empty.**
  - It filters on `m.get("memory_type")` (`cli.py:103-107`), but the lite schema has no `memory_type` column. So facts, policies, guardrails and skills always come back as `[]`.
  - **Fix:** either remove the categories, or add the column with a schema version 2 migration.
  - **Done (2026-09-27): removed the categories.** The store has no `memory_type` column (that is the engine's schema), so all four lists filtered on a key that never exists and printed empty as if they had been searched. Adding the column would be a real feature (schema v2 migration + `remember()` parameter + CLI flag) with no caller; nothing else in the repo uses it. `bootstrap` now returns constraints, provenance and abstentions, which is what it can answer. Regression test `test_bootstrap_does_not_print_categories_it_cannot_answer`.

- [x] **P1-4 `restore` is unsafe** (`cli.py:189-235`).
  - It overwrites the live database with no confirmation and no safety backup.
  - It never checks that the source file is a Mnemosyne store.
  - Replaying a `.gz` dump with `executescript` onto an existing database collides with its tables.
  - **Fix:** add a confirmation prompt. Make a safety backup first. Check the source by opening it with `PythonMemoryStorage` before swapping it in.
  - **Done (2026-09-27):** the restore is staged, not in place. A prompt (or `--yes`) is required; the current store is copied to `<db>.pre-restore.<ts>` first; the source is replayed into `<db>.restore-tmp.<pid>` next to the destination; that staged file is opened with `PythonMemoryStorage` (a foreign or truncated source is refused there, before the destination is touched); only then is it `os.replace`d in. Because the dump is replayed into a fresh file, it can no longer collide with the live database's tables. A SQLite backup source is read with `mode=ro` so validation cannot rewrite the backup. Three regression tests, including a gzipped-dump round trip that also asserts the safety copy really holds the old store and that no staged file survives.

- [x] **P1-5 Recall hides database errors.**
  - Recall returns `[]` on any `sqlite3.Error` (`storage.py:791-793`); `list_memories` (`:862`) and `count` (`:1024`) do the same.
  - MCP clients cannot tell "no match" from "store broken", which contradicts the fail-closed design.
  - **Fix:** raise `StorageError`, or return `isError` over MCP.
  - **Done (2026-09-27):** `recall`, `list_memories` and `count` raise `StorageError` naming the path and the SQLite error. The same lie existed in `consolidate`: a failed read returned "0 duplicate groups" — a clean bill of health for a store it could not read — and a failed delete was swallowed into `removed = 0` after a rollback. Both now raise. The CLI's `main()` already turns `StorageError` into one line and exit 1, and the MCP tool path already reports `isError`, so both surfaces distinguish "no match" from "store broken".

- [x] **P1-6 Orchestration agents never save anything.** (resolved by P2-3: `src/orchestration/` is gone)
  - All 8 memory writes call `self.storage.store({...})`, which is the retired PyO3 API:
    - `executor.py:786`, `executor.py:898`
    - `optimizer.py:463`, `optimizer.py:744`
    - `orchestrator.py:319`, `orchestrator.py:464`
    - `reviewer.py:300`, `reviewer.py:514`
  - `PythonMemoryStorage` only has `remember()`, so every call raises `AttributeError`.

- [x] **P1-7 `optimizer.py:555` checks `'' in task_lower`.** (resolved by P2-3: `src/orchestration/` is gone)
  - That is always true, so every task gets `file_types=['']`.
  - It is left over from a bulk deletion of `.rs` strings; the same edit left the "e.g., , .py" artifact in the old prompt.

- [x] **P1-8 DSPy module loading always fails.** (resolved by P2-3: `src/orchestration/` is gone)
  - `dspy_service.py:118,143,160` import `mnemosyne.orchestration.dspy_modules…`.
  - That path does not exist, because `mnemosyne` is the engine's package.

- [x] **P1-9 The executor runs model-written commands in a shell.** (resolved by P2-3: `src/orchestration/` is gone — the unsandboxed executor no longer ships)
  - `run_command` (`executor.py:~735`) passes model-generated strings to `asyncio.create_subprocess_shell`.
  - The "trusted execution boundary" only sets the working directory. The command can still touch any absolute path.
  - **Fix:** add an allowlist or an approval hook, or document the executor as unsandboxed.

- [x] **P1-10 Wrong name and version labels.**
  - `_package_version()` (`cli.py:162-169`) falls back to the `mnemosyne` distribution. That is the engine, so the lite CLI can report the engine's version.
  - `src/mnemosyne_lite/mcp.py:44` hardcodes `serverInfo` as `name: "mnemosyne"` and `version: "2.4.0"`. It should say `mnemosyne-lite` and read `__version__`.
  - **Done (2026-09-27):** `_package_version()` now returns `mnemosyne_lite.__version__` — one source, no metadata lookup and no fallback to the engine's distribution name. `mcp.py` reports `serverInfo` as `mnemosyne-lite` with that version. `tests/test_lite_mcp.py` asserted the old name; that assertion was updated for the intended contract change (and now checks the version too). Regression tests `test_version_labels_come_from_this_package` and `test_mcp_announces_itself_as_mnemosyne_lite`.

- [x] **P1-11 `.auto/run.sh` can log a failed run as a success.** (fixed in P2-8, as `bench/run.sh`)
  - `RC=$?` runs after `cp`, so it captures `cp`'s exit code instead of `measure.sh`'s.
  - A failed measurement can therefore be logged as `keep`.

## P2: remove Rust-era and experiment leftovers

Git history and `docs/archive/RUST_ARCHIVE_REF.md` (pointing at `feat/hermes-native-provider` `09a6973`) keep the old work. Deleting it from `main` loses nothing.

- [x] **P2-1 Delete the retired Rust adapter, `integrations/hermes-memory-provider/`** (including its tracked `mnemosyne_rust_hermes.egg-info`).
  - It talks to a Rust binary that no longer exists.
  - Its README says this repo "does not ship a Python provider named mnemosyne", which contradicts the canonical provider.
  - Its 21 tests are the first thing `test-all.sh` runs, and AGENTS.md calls them the "fast unit tests". So CI's headline test run says nothing about the provider you actually ship.
  - **Follow-up:** point `test-all.sh` and AGENTS.md at `tests/test_vendored_provider.py`, `tests/test_provider_loader.py` and `tests/test_provider_db_path.py`.
  - **Optional:** fold the `integrations/hermes/` tombstone README into `docs/archive/`.
  - **Done (2026-09-27):** adapter and egg-info deleted; tombstone README now `docs/archive/RETIRED_SLIM_PROVIDER.md`. `test-all.sh` runs the three real provider gates (`tests/test_vendored_provider.py`, `test_provider_loader.py`, `test_provider_db_path.py`) instead of the dead adapter's unittest suite, and its dead `--skip-llm` / `--llm-only` modes are gone (no LLM-marked test remains).

- [x] **P2-2 Delete the Rust test suites.**
  - **Done (2026-09-27):** `tests/e2e/`, `tests/manual/`, `tests/scripts/`, `tests/e2e_validation.sh` deleted.
  - `tests/e2e/`: 94 files. `lib/common.sh:268-280` runs `cargo build --release`.
  - `tests/manual/`
  - `tests/scripts/`
  - `tests/e2e_validation.sh`

- [x] **P2-3 Decide the fate of `src/orchestration/`** (about 24k lines, including 44 files under `dspy_modules/`).
  - `dspy_modules/` has 10 `test_*.py` files inside `src/`, and pytest never runs them.
  - Nothing packages it: `pyproject.toml` only includes `mnemosyne_lite*` and `lib*`.
  - Saving is broken (P1-6), DSPy loading is broken (P1-8), and the shell executor is unsandboxed (P1-9).
  - Its tests account for all 36 skips. They skip on the PyO3 module `mnemosyne_core`, or on a missing `ANTHROPIC_API_KEY`. The API-key gate contradicts AGENTS.md's rule that LLM calls go through the Hermes proxy.
  - **Recommendation:** tag it (for example `archive/orchestration`) and remove it from `main`. That also removes:
    - `tests/orchestration/`
    - `tests/test_orchestration_integration.py`, `tests/test_privacy_python_integration.py`, `tests/test_executor_boundary.py`, `tests/test_hermes_llm.py`
    - the `orchestration` extras in `pyproject.toml` and `requirements.txt`
    - `src/mnemosyne_orchestration.egg-info`
    - `uv.lock`: its root package is `mnemosyne-orchestration` and it was last touched 2025-11-04. Regenerate it for `mnemosyne-lite`, or drop it.
  - **If you keep it,** it needs its own milestone: fix P1-6 to P1-9, package it, and rewrite the tests against `hermes_llm`.
  - **Done (2026-09-27): archived then deleted.** `git tag archive/orchestration` + `archive/rust-era` (both pushed) hold the last commit containing it, so it is one command away. Removed `src/orchestration/`, `tests/orchestration/`, `test_orchestration_integration.py`, `test_privacy_python_integration.py`, `test_executor_boundary.py`, `test_hermes_llm.py`, the `orchestration` extras, `src/mnemosyne_orchestration.egg-info/` and `uv.lock`. `tests/test_python_hardening.py` imported `orchestration.coordinator`/`context_monitor`/`parallel_executor`, so its three orchestration tests and those imports went too; its storage-safety tests are untouched. Suite is now 46 passed, 0 skipped (was 79 passed / 36 skipped).

- [x] **P2-4 Delete unused schema and protocol files.**
  - **Done (2026-09-27):** `migrations/` (52 files), `patches/`, `proto/` deleted. Verified nothing under `src/`, `tests/`, `scripts/`, `install.sh` or `pyproject.toml` reads them.
  - `migrations/`: 51 SQL files plus `MANIFEST.md`
  - `patches/`: diffs against those migrations
  - `proto/`: gRPC definitions
  - No runtime code reads any of them. The lite store has its own inline schema, and the engine owns its own.

- [x] **P2-5 Delete Rust-era scripts and specs.**
  - `scripts/test-server.sh`: runs `./target/debug/mnemosyne serve`
  - `scripts/baseline/verify_baseline_install.sh`
  - `scripts/beads-sync.sh`
  - `spec.md`: a Rust TUI network-panel spec for `src/bin/dash`
  - `benchmark/retrieval/` and `tests/benchmark/`: they drive `mnemosyne recall --hierarchical`, a Rust CLI flag. Retarget them at the lite or engine recall, or delete them.
  - **Review before deleting:** `scripts/safe-shutdown.sh`, `scripts/cleanup-processes.sh`, `scripts/diagnostics/collect-memory-diagnostics.sh` and `scripts/build-diagrams.sh` (it builds D2 diagrams for the Rust architecture).
  - **Done (2026-09-27):** all of the above deleted, including the four to review — they serve the retired server/diagram pipeline and nothing referenced them. `scripts/engine-parity-check.sh` and `scripts/vendor-provider-sync.sh` are kept (both are cited by `LIVE_VERIFICATION.md` / `PATCHES.md`).

- [x] **P2-6 Replace `scripts/install/uninstall.sh`.**
  - It deletes `~/.local/bin/mnemosyne` and calls `mnemosyne config delete-key`.
  - It never removes the plugin link or the provider package, yet the README points users at it.
  - **Fix:** implement the uninstall steps from `integrations/hermes-provider/README.md` (remove the plugin link, `uv pip uninstall mnemosyne-hermes-provider`, keep data by default). `install.sh --uninstall` is one option.
  - **Done:** the 493-line Rust-era script is deleted and `install.sh --uninstall [--purge] [--dry-run] [--yes]` implements the README's steps: removes a plugin symlink or an intact installer-created copy (format-v1 full inventory and canonical source path), uninstalls `mnemosyne-hermes-provider`, and keeps memory unless `--purge` (which also removes `mnemosyne-memory` and `$HERMES_HOME/mnemosyne`, guarded against `/`, `.` and empty paths). It warns when `config.yaml` still selects `memory.provider=mnemosyne`, and refuses to remove unverified real directories. This is also the repo side of P0-5: the live Hermes agent can run it after pulling.

- [x] **P2-7 Remove the "planning deliverable" stubs.**
  - `scripts/verify_backup_auth.sh` and `scripts/redeploy/*_proposed.sh` only print text ("NOT EXECUTED").
  - Also: `deploy/sanitized_example/`, `.auto/deliverables/`, `.auto/retirement/`, `docs/plans/item_*`, `docs/plans/SYNTHESIS.md`.
  - The banners in AGENTS.md and README ("Pivot Complete — Planning Deliverable", "No DB rebuild/redeploy executed") come from the same pass. Remove them too (see P3-1 and P3-2).
  - **Done (2026-09-27):** `scripts/verify_backup_auth.sh`, `scripts/redeploy/`, `deploy/sanitized_example/`, `.auto/deliverables/`, `.auto/retirement/`, `docs/plans/item_*` and `docs/plans/SYNTHESIS.md` deleted. The AGENTS.md/README banners are handled in P3-1/P3-2.

- [x] **P2-8 Move experiment state off `main`.**
  - At the root: `autoresearch.md`, `autoresearch.jsonl`, `autoresearch.sh`, `autoresearch.ideas.md`, `autoresearch-dashboard.md`.
  - Also `experiments/`, `history/seed/`, and `.auto/` (about 60 files, 500 KB, mostly JSONL logs and probes).
  - **Fix:** keep the reproducible benchmark harness in a `bench/` folder with a README. Drop the logs; git history keeps them.
  - **Done (2026-09-27):** `bench/` now holds `measure.sh` (recall latency on the deterministic 3000-row corpus), `autoresearch.sh` (median of `AR_RUNS`, median-of-5 tiebreak near `AR_BASELINE`), `run.sh` (experiment ledger, with the P1-11 exit-code bug fixed) and `README.md` — which is also the benchmark doc P5-5 asks for, including the loop's recorded 0.0051 ms p50 and the explicit note that it is a historical claim, not a current measurement. Root `autoresearch.*`, `experiments/`, `history/seed/` and `.auto/` (55 files) deleted; `.gitignore` now ignores `bench/data/`, `bench/log.jsonl`, `bench/last_measure.txt`.

- [x] **P2-9 Clear clutter.**
  - **Done (2026-09-27):** `Makefile.archive`, `.test-hook-trigger`, `.beads/`, `src/mnemosyne.egg-info/` deleted. `.gitmessage` rewritten without the Claude attribution and without the retired DSPy/SpecFlow track labels (and nothing set `commit.template`, so it is a template you opt into). `.github/ISSUE_TEMPLATE/config.yml` links now point at this repo's README, QUICK_START, AGENT_SETUP and TROUBLESHOOTING.
  - `Makefile.archive`: its `doctor` target imports `mnemosyne_orchestration.config`, which does not exist, and its other targets are placeholders.
  - `.test-hook-trigger`
  - `.beads/`: `daemon.lock` is tracked, and `issues.jsonl` was last touched 2025-11-23.
  - Tracked `src/mnemosyne.egg-info/` and `src/mnemosyne_orchestration.egg-info/`. `.gitignore` already excludes `*.egg-info/`, but these were committed before that rule.
  - `.gitmessage`: it appends "Generated with Claude Code" and "Co-Authored-By: Claude", which breaks AGENTS.md's no-AI-attribution rule. It also carries the old DSPy/SpecFlow track labels.
  - `.github/ISSUE_TEMPLATE/config.yml`: every link points at rand/mnemosyne.

- [x] **P2-10 Purge the stale docs.** Move these to `docs/archive/` or delete them.
  - **`docs/` folders:** `historical/` (40), `plans/` (17), `archive/` (15), `whitepaper/` (12) plus `whitepaper.html` and `whitepaper.md`, `design/`, `v2/`, `specs/`, `test-reports/`.
  - **`docs/` files:**
    - `SESSION_*.md`
    - `FEATURE_BRANCH_STATUS.md`
    - `FD_LEAK_FIX_TEST_RESULTS.md`
    - `DSPY_*.md`
    - `BEADS_INTEGRATION.md`
    - the ICS guides
    - the Rust audits in `docs/security/`
  - **Site assets:** `index.html`, `css/`, `js/`, `overrides/`, `assets/`, `diagrams-d2/`.
  - **Root files:**
    - `AGENT_GUIDE.md`: 80 KB and 187 Rust references
    - `PR_REVIEW.md`, `ROADMAP.md`, `ORCHESTRATION.md`, `EVALUATION.md`, `CONTEXT_LOADING.md`
    - `HOOKS_TESTING.md`, `LLM_TESTING.md`, `MANUAL_TESTING.md`
    - `DOCUMENTATION.md`: 16 broken links
    - `SECRETS_MANAGEMENT.md`: the engine CLI has no `secrets` command (checked in the installed 3.15.1 source)
  - **Target:** about 8 living docs: README, QUICK_START, TROUBLESHOOTING, AGENTS, CONTRIBUTING, CHANGELOG, the provider README, and `docs/AGENT_SETUP.md`.
  - **Done (2026-09-27):** `docs/` went from 255 tracked files to 6: `AGENT_SETUP.md`, `HERMES_INTEGRATION.md`, `MCP_CLIENT_CONFIGS.md`, `TROUBLESHOOTING.md` (promoted to the root in P3-3), plus `archive/RUST_ARCHIVE_REF.md`, `archive/RETIRED_SLIM_PROVIDER.md` and `archive/ARCHITECTURE_RUST.md` (ARCHITECTURE.md was not on this list; it describes the retired product, so it is archived rather than deleted). Every listed folder, site asset and root file is gone. `MCP_SERVER.md` is kept: it documents the lite MCP stdio server, which really ships. Survivor links into the purged set are fixed in P3.

## P3: update docs and contracts to match what exists

- [x] **P3-1 Rewrite the README.**
  - **Banners:** remove the planning-deliverable banners.
  - **Status line:** "Current status (v2.4.0): dynamic profile slice + typed `extends` edges are delivered" describes Rust features. `scripts/check_version_drift.sh` checks that exact string, so change the check together with the text.
  - **Features and architecture:** these all describe the Rust product:
    - LibSQL vector search
    - hierarchical topic tree, reasoning memory, bootstrap, dynamic profile
    - the evolution and evaluation systems
    - the ICS editor with CRDT, vim mode and tree-sitter
    - the Ractor actor diagram
    - the Performance section, whose numbers come from the Rust server
  - **Broken links:** `TODO_TRACKING.md` (twice) and the root `TROUBLESHOOTING.md` do not exist.
  - **Invalid commands:** the Migration section uses `mnemosyne import --from … --namespace … --format json`. The engine's import has no `--from` or `--namespace` flag.
  - **Contributing section:** it still says "Use Beads", "Work Plan Protocol" and "commit before testing".
  - **Suggested structure:**
    1. What this is: a Hermes provider distribution
    2. Quickstart
    3. Verify
    4. The lite surface
    5. The real architecture
    6. Credits
  - **Credits:** credit AxDSan/mnemosyne prominently, since the engine and provider are theirs, and state the relationship to rand/mnemosyne.
  - **Done (2026-09-27):** rewritten to the suggested structure (what this is / quickstart / verify / the lite surface / the real architecture / contributing / credits). The `Current status (v2.4.0): dynamic profile slice + typed \`extends\` edges` line is gone, so `scripts/check_version_drift.sh` was changed with it (P3-5). `**Current Version**: 3.0.0` is kept as the one machine-readable marker the gate reads. The Migration section, the Beads/Work-Plan/"commit before testing" contributing text, the Performance section and the Ractor/ICS/evolution/evaluation/LibSQL feature list are all gone; `TODO_TRACKING.md` and the root `TROUBLESHOOTING.md` links are resolved (the latter now exists). Credits name AxDSan/mnemosyne as the source of the engine and the upstream provider, and state plainly that this is not rand/mnemosyne.

- [x] **P3-2 Rewrite `AGENTS.md` against the real file tree.**
  - **Paths that do not exist:**
    - `python.toml`, `build.py`, `Makefile`
    - `src/mcp|cli|storage|embeddings|agents|ics|tui|api|rpc|coordination|python_bindings|bin|services|evolution|evaluation`
    - `scripts/rebuild-and-update-install.sh`, `build-and-install.sh`, `test-hermes-adoption.sh`
    - `tests/ics_integration_test`
  - **Rules for features that are gone:** `mnemosyne serve`, `mnemosyne secrets`, Ractor actors, Iroh P2P, the `--no-enrich` keyless check, and the Makefile `doctor` target.
  - **Test pointer:** its "fast unit tests" point at the dead adapter (see P2-1).
  - **Docs index:** it points at the missing root `TROUBLESHOOTING.md`.
  - **Done (2026-09-27):** rewritten against the real tree. Every listed path was checked against `git ls-files`; the rules for `mnemosyne serve`, `mnemosyne secrets`, Ractor, Iroh, `--no-enrich` and the Makefile `doctor` target are gone. The "fast unit tests" pointer now names the three real provider gates, the docs index lists only files that exist, and a new "Contracts that must not drift" section carries the facts that were previously scattered (provider id, vendored-snapshot discipline, engine pin, DB precedence, lite MCP tool names, lite DB default, storage-safety rules, keyless requirement).

- [x] **P3-3 Fix the other entry docs.**
  - **Docs:**
    - `QUICK_START.md`: shows binary-install output (`~/.local/bin/mnemosyne`) and `mnemosyne secrets init` / `set`.
    - `MCP_SERVER.md:317`: uses `mnemosyne remember … --no-enrich`.
    - `docs/HERMES_INTEGRATION.md`: imports into the Rust store at :259, calls `serve` "the legacy equivalent" at :147, and lists a `mnemosyne_hierarchy` tool at :197.
    - `docs/TROUBLESHOOTING.md`: uses `cargo test` and `RUST_LOG`. Rewrite it and promote it to the root, where the other docs already link.
  - **Examples:**
    - `examples/hermes/mcp-config.json`: points at `target/release/mnemosyne` with `args: ["serve"]`.
    - `examples/basic-usage/*.sh`: promise "automatic LLM enrichment" and `--format json`.
  - **GitHub templates:**
    - `.github/PULL_REQUEST_TEMPLATE.md`: `cargo test`, `fmt`, `clippy`, `tarpaulin`.
    - `.github/ISSUE_TEMPLATE/bug_report.md`: offers `cargo install` as an install method.
  - **Done (2026-09-27):** `QUICK_START.md`, `MCP_SERVER.md`, `docs/HERMES_INTEGRATION.md` and `TROUBLESHOOTING.md` (moved to the root) rewritten; the three `examples/basic-usage` scripts, `examples/hermes/mcp-config.json`, both GitHub templates and the two stale lines in `integrations/hermes-provider/README.md` fixed. Also fixed outside the listed set: `docs/AGENT_SETUP.md` (its MCP step used `command: mnemosyne`), `docs/MCP_CLIENT_CONFIGS.md` (wrong command, wrong DB path, and a false "dotted names are advertised" claim), `CONTRIBUTING.md` (rewritten — it was Rust-era throughout: clippy, iroh, libsql, tree-sitter, `python.toml`), and the two remaining issue templates.
  - **Deleted rather than fixed:** `examples/mcp-integration/` (439 + 530 lines describing `mnemosyne secrets`, `mnemosyne serve`, `RUST_LOG` and `--format json`) and `examples/workflows/` (both scripts pipe `--format json` into jq against a CLI that no longer exists). `examples/README.md` was rewritten for the three surviving examples. `evolution-config.example.toml` was also deleted: it configured the retired evolution subsystem.

- [x] **P3-4 Fix the CHANGELOG.**
  - The `[Unreleased]` section lists Rust work (`LibsqlStorage`, `src/hierarchy.rs`, `tests/*.rs`, `ci.yml`).
  - Write the pivot entry: vendored provider, lite rename, Rust retirement, installer and doctor.
  - Cut a release. 3.0.0 is the honest version, because the pivot removes the binary and its CLI.
  - **Done (2026-09-27):** the `[Unreleased]` Rust block is replaced by a `[3.0.0] - 2026-09-27` entry covering the whole pivot (provider vendoring, installer and doctor, the lite rename, the Rust retirement, the storage fixes, the deletions), and the header now says plainly that entries up to `2.4.0` describe the retired Rust product. The two trailing version links that pointed at `rand/mnemosyne` are gone. The historical entries are kept as history.

- [x] **P3-5 Use one version source.**
  - Today the version appears in `pyproject.toml` (2.4.0), `mnemosyne_lite.__version__`, the `mcp.py` literal, the provider `pyproject.toml` (0.1.0) and `orchestration.__version__` (0.1.0).
  - Extend `check_version_drift.sh` to cover them all, or read `importlib.metadata` at runtime.
  - **Done (2026-09-27): stronger than either option — the literal was removed.** `pyproject.toml` declares `dynamic = ["version"]` with `[tool.setuptools.dynamic] version = { attr = "mnemosyne_lite.__version__" }`, so there is exactly one version literal in the repo. `scripts/check_version_drift.sh` now fails if a `version =` literal reappears in `pyproject.toml`, if the dynamic wiring breaks, if the README marker drifts, or if the engine pin moves in any of its three carriers. The CLI reads the package (`mnemosyne_lite.__version__`) and `mcp.py` reports the same value, so neither can carry a stale copy. Verified by building a wheel: `mnemosyne_lite-3.0.0-py3-none-any.whl`, METADATA `Version: 3.0.0`. (Orchestration's `__version__` is gone with the module, and the provider is a separate distribution with its own version.)

- [x] **P3-6 Keep CI current.**
  - Bump `actions/checkout@v4` and `astral-sh/setup-uv@v5`; every run shows Node 20 deprecation warnings.
  - `claude-code-review.yml` tells Claude to use "the repository's CLAUDE.md", which does not exist. Point it at `AGENTS.md`.
  - Confirm the `CLAUDE_CODE_OAUTH_TOKEN` secret exists, or remove `claude.yml` and `claude-code-review.yml`.
  - **Done (2026-09-27):** both actions are bumped and pinned to full commit SHAs with the version in a trailing comment (`actions/checkout` v7 → `3d3c42e5…`, `astral-sh/setup-uv` v10.2.0 → `c18668ad…`); the SHAs were resolved with `gh api …/commits/<tag>` rather than assumed. The secret does not exist (`gh secret list` prints nothing) and every historical run of those workflows failed, so `claude.yml` and `claude-code-review.yml` are deleted — which also removes the `CLAUDE.md` instruction, since that file never existed. The lint job now also builds the wheel, so broken dynamic-version wiring fails in CI rather than only in the release pipeline.

- [x] **P3-7 Clean up the lite surface.**
  - Remove the `embed` and `migrate` placeholder commands, which always exit 1 with "blocked".
  - Remove the stale `blocked` and `queue_state` fields from `diagnostics`.
  - Remove the `sys.path.insert` hack at `cli.py:16`.
  - `tools.py` still accepts the retired `mnemosyne-rust` policy owner, and its docstring says "for MCP and the Hermes provider". Fix both.
  - `remember` defaults to the `agent:hermes` namespace on a surface that is explicitly not the Hermes provider. Pick a neutral default.
  - **Done (2026-09-27):** `embed` and `migrate` are gone (they only printed "blocked" and exited 1); `diagnostics` no longer prints the stale `blocked`/`queue_state` fields; the `sys.path.insert` hack is gone (the package uses relative imports and is only reachable as an installed package or with `src` on the path); `tools.py`'s docstring says these are the lite MCP tools, not the provider's, and `mnemosyne-rust` is out of the `policy_owner` allowlist (a client that no longer exists must not capture turns here); the CLI's `remember --namespace` and the MCP `call_tool` default are now `default` instead of the provider's `agent:hermes`. **Contract note:** `agent:hermes` is still accepted everywhere it was — only the default moved, so pass `--namespace agent:hermes` (or the MCP argument) to keep writing where earlier versions put it.

## P4: engineering baseline

- [x] **P4-1 Add linting and type checks.** Nothing is configured today; the old Makefile placeholders say "not configured".
  - ruff for linting and formatting.
  - mypy or pyright on `src/mnemosyne_lite` and `tests`. Exclude the vendored provider.
  - shellcheck on `install.sh` and `scripts/`.
  - pre-commit plus a CI job.
  - **Done (2026-09-27):** ruff (`E`, `F`, `W`, `I`, `UP`, `B`, `SIM`; line-length 100; `force-exclude` on the vendored snapshot) with `ruff format` over `src` and `tests`; mypy over `src/mnemosyne_lite` and `tests`; shellcheck `-S warning` over `install.sh`, `scripts/` and `bench/`. The latest local verification reports `ruff check` clean, `ruff format --check` clean, `mypy` success (14 source files), shellcheck clean, and the pre-commit hooks run the same tools. `.pre-commit-config.yaml` uses `language: system` hooks so local and CI run the same executables and fetch nothing; the CI `lint` job runs the same commands. Fixes it forced: `INDEXES`/`DROPPED_INDEXES` are tuples (shared mutable class constants), `params` lists are annotated `list[Any]`, `contextlib.suppress` in three places, `open()` in context managers, and `.gitattributes` now pins `eol=lf` for the text formats so a Windows checkout no longer disagrees with CI (the `install.sh` blob and the `.sh` worktrees were the live case).

- [x] **P4-2 Make the CI matrix match the support claims.**
  - The README claims Python 3.11–3.14 on Linux, macOS and Windows. CI runs only Python 3.11 on Ubuntu.
  - Run unit tests on Python 3.11–3.13 (and 3.14) across Ubuntu, macOS and Windows.
  - Run the smoke test on Ubuntu and macOS, against both Hermes versions on PyPI (0.18.2 and 0.19.0).
  - Fix the Hermes range claim: older docs listed a later release as tested without published-version or compatibility evidence.
  - **Done:** `.github/workflows/python-ci.yml` tests Python 3.11–3.14 on Ubuntu/macOS/Windows and runs the real-Hermes smoke on Ubuntu/macOS for 0.18.2/0.19.0. Active docs and the provider's supported range now say `>=0.18,<0.20`; the final pushed workflow result remains to be captured.

- [x] **P4-3 Add coverage** with pytest-cov, reported in CI. Add a regression test for the fresh-install `doctor` case from P0-2.
  - **Done:** CI installs `pytest-cov`; `test-all.sh` detects it and emits a `mnemosyne_lite` coverage report in the same pytest run. Fresh-install `doctor` has regression coverage in `tests/test_provider_db_path.py`.

- [x] **P4-4 Rename the top-level `lib` package.**
  - `pyproject.toml` installs it as `lib*`. A generic `lib` in site-packages can collide with other packages.
  - Move it under `mnemosyne_lite` (for example `mnemosyne_lite.storage`). `plans/dev-todo-v2.md` already defers this.
  - **Done (2026-09-27):** `src/lib/storage.py` → `src/mnemosyne_lite/storage.py`; `src/lib/mnemosyne_client.py` → `src/mnemosyne_lite/db_path.py` (git recorded both as renames, so history follows); `src/lib/__init__.py` deleted and the package removed from the tree and from `pyproject.toml`'s `include`/mypy/pyright scopes. The only importers were `cli.py`, `mcp.py`, `tools.py`, four test files and `bench/measure.sh`; all now import from `mnemosyne_lite`. `git` history is the only remaining reference.
  - **Also removed:** `MnemosyneClient` (100 lines in the old `mnemosyne_client.py`). Nothing in the repo, in the docs, or in configuration constructed it — the only reference was `lib/__init__.py`'s own re-export — and the rename breaks `from lib import ...` for any external caller regardless, so keeping a dead class alive under a new name would have preserved nothing.

- [x] **P4-5 Make CLI output machine-readable.**
  - Commands print Python dict reprs such as `{'id': ...}`.
  - Add `--format json|text`. The docs already assume `--format json` exists.
  - **Done:** `mnemosyne-lite` supports `--format json|text` before or after subcommands. JSON success/cancel paths for init, backup, restore, maintenance, remember, bootstrap, diagnostics, recall and list produce one stdout document; text-mode output is preserved. Regression coverage exercises parseable JSON and restore/maintenance safety-copy behavior.

- [x] **P4-6 Move the lite store's default database.**
  - `~/.mnemosyne/mnemosyne.db` sits in the engine's folder.
  - Give lite its own default, with a migration note.
  - **Done (2026-09-27):** the default is now `~/.mnemosyne-lite/mnemosyne.db`, defined once as `DEFAULT_DB` in `mnemosyne_lite/db_path.py` (the CLI's help text reads it from there rather than repeating the literal). **Migration note:** existing lite stores stay where they are; point `--db-path` or `MNEMOSYNE_DB_PATH` at the old file, or move it. Nothing is deleted, and the change fails loudly (`no Mnemosyne database at <new path>`) rather than silently starting an empty store — `mnemosyne-lite diagnostics` prints the resolved path.

- [x] **P4-7 Add Dependabot** for GitHub Actions and pip, and add a `SECURITY.md`.
  - **Done:** `.github/dependabot.yml` updates GitHub Actions and pip dependencies; root `SECURITY.md` documents reporting.

## P5: level-up

- [x] **P5-1 Decide the Windows story.**
  - `install.sh` needs real symlinks, and the smoke test skips on Windows.
  - Yet the support matrix lists Windows, and your own setup runs on it (see P0-5).
  - **Options:** add a copy or junction mode plus a Windows CI lane, or drop Windows from the matrix.
  - **Done:** keep Windows in the Python test matrix and support installer `--copy`/copy fallback with format-v1 digests over the complete package inventory. `doctor` rejects missing, stale, or unexpected files, and installer overwrite/uninstall refuse unverified directories. Windows CI exercises the contract tests; the real-symlink onboarding smoke remains Linux/macOS-only and is explicitly skipped on Windows.

- [x] **P5-2 Send the provider patches upstream. (explicitly skipped)**
  - `PATCHES.md` records local patches P1–P18 and their upstream disposition. None were sent upstream; P5-2 remains explicitly skipped at the user's direction.
  - Each one accepted upstream means less vendored drift to maintain on every re-vendor.
  - The generic ones (P1 to P4) are the easiest; the P0-2 doctor fix belongs there too.
  - **Disposition:** intentionally skipped at the user's direction; no patches or repository data were sent upstream.

- [x] **P5-3 Get early warning of upstream drift.**
  - Add a weekly scheduled workflow that checks PyPI for new `mnemosyne-memory` and `hermes-agent` releases.
  - It should run `scripts/vendor-provider-sync.sh` against the new wheel and the smoke test against the new Hermes, and open an issue if either breaks.
  - **Done:** weekly `.github/workflows/upstream-drift.yml` checks PyPI, compares a newer engine wheel with the vendor snapshot, runs the smoke against the latest Hermes, and opens/comments on one triage issue. The probe now fails visibly on vendor-sync errors and uses the actual `<0.20` Hermes support bound rather than treating 0.20 as supported.

- [x] **P5-4 Switch lite recall to SQLite full-text search.**
  - Recall is currently a substring scan (`instr()`). It also keeps a full copy of all memory text in RAM for each thread (`storage.py:685`), so memory grows with the corpus times the thread count.
  - Replace it with FTS5 and BM25 ranking. `sqlite3` ships FTS5, so recall stays keyless.
  - This gives real relevance ranking and bounded memory, and removes most of the cache code.
  - **Done (2026-09-27):** `memories_fts` is an external-content FTS5 table over `memories.content` (the index only — the corpus is not duplicated), kept in sync by three triggers, with `AFTER UPDATE OF content` so the access-count flush does not reindex. Schema generation 2; a store classified below that (a v1 store, or a legacy one from before the sentinel) rebuilds the index inside the same `BEGIN IMMEDIATE` transaction as the version bump, so it can never open at v2 with an empty index. `recall()` is now `MATCH` ordered by `bm25, importance DESC, created_at DESC, id`; the trailing `id` is load-bearing — BM25 ties are the normal case and without a unique final key a `LIMIT` can drop a different row between two identical queries.
  - **The semantics change is deliberate and documented:** matching is per token (so `al` no longer finds `alpha`) and FTS5's `unicode61` tokenizer folds case *and* diacritics (so `munchen` finds `MÜNCHEN`). A query with no letter or digit has no token to match and FTS5 answers it with an empty result rather than an error, so those queries (`%`, `_`, `\`) keep the literal `instr()` scan over `content_lower` — that is now the only caller of it. Verified: 57 tests pass, including a new `test_v1_store_gains_the_full_text_index` migration test, `test_recall_ranks_best_match_first`, `test_recall_matches_tokens_not_substrings` and `test_equal_rank_order_is_deterministic`; `bench/measure.sh` and `bench/autoresearch.sh` both run (median-of-5 p50 0.0058 ms, p99 0.444 ms, recorded in `bench/README.md`).

- [x] **P5-5 Simplify `storage.py`.**
  - The autoresearch loop cut median search latency from 0.0102 ms to 0.0051 ms (about 5 microseconds).
  - It got there by layering id-tuple batching, patching cached rows in place, per-thread counters, and two caches. That layering is where the P1-1 bug came from.
  - Keep one per-thread cache, and move performance claims into a benchmark doc.
  - **Done (2026-09-27):** the id-tuple batching and the per-thread snapshot cache are gone with the substring scan (`_search_candidates`, `SEARCH_QUERY_CACHE_MAX` and `_SEARCH_MISS` deleted), leaving exactly one cache: the per-thread recall memo, which only holds rows a thread asked for rather than a full copy of the corpus. In-place row patching and the per-thread counters stay — they are not caches, and the counters are what make the memo's version correct. `bench/README.md` is the benchmark doc: it records the new median, states the ±8–20% single-run noise and the 56% spread measured between two runs in one session, and says plainly that the honest reading is "about the same p50, less memory, real relevance ranking, one fewer cache" rather than a speed-up.

- [x] **P5-6 Build a real release pipeline.**
  - Tag, build the wheels, install-test them in a fresh venv, then publish a GitHub release.
  - Pin the README's "fetch `docs/AGENT_SETUP.md` and follow it exactly" URL to a release tag instead of `main`. Agents follow that file as instructions.
  - **Done with one deliberate exception:** `.github/workflows/release.yml` runs only on `v*` tags, builds both wheels, installs/tests them in a fresh venv, and creates a GitHub Release with those tested wheels. No release tag was created/published in this task. The current README has no raw `main` URL for `AGENT_SETUP.md`; it uses a checked-in relative link, so no broken link to a not-yet-existing tag was added. Pin that URL after an authorized release tag containing the runbook exists.

---

## Suggested order

1. **Green `main`:** P0-1 to P0-4, as one small PR. Done when both CI jobs pass and no Pages or release job can fail.
2. **Deletions (P2):** three PRs:
   1. code and tests (P2-1 to P2-6)
   2. docs (P2-10)
   3. experiments and clutter (P2-7 to P2-9)

   Each PR should leave CI green.
3. **Honest docs (P3):** README, AGENTS, QUICK_START and TROUBLESHOOTING, then the CHANGELOG and a release.
4. **Lite bugs:** P1-1 to P1-5 and P1-10, each with a regression test.
5. **Engineering baseline (P4).**
6. **Level-up (P5):** this was the baseline ordering; P5-2 was later explicitly skipped, while P5-3 to P5-6 were handled as recorded above.

## Relation to `plans/dev-todo-v2.md`

That plan's Tracks A and B (storage safety and the canonical provider) are done. Its "release prep" section (§9) was left out of scope, and most of this file is that pass made concrete:

- CI replacement
- the docs overhaul
- a single version source
- deleting dead weight
- the `lib` rename

It also left open the live verification (`LIVE_VERIFICATION.md`), which is still not executed (see P0-5).
