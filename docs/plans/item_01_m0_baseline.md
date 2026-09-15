# M0 — Baseline and Prevent Unsafe Replacement (Deliverable)

Status: COMPLETED (planning/artifacts only; no deployed system rebuilt; no DB rebuilt/deployed).
Contracts preserved: `memory.provider` (`mnemosyne-rust` / future `mnemosyne`), namespace (`agent:hermes`), DB path (`MNEMOSYNE_DB_PATH`), tool names (`mnemosyne_memory_search`, `mnemosyne_memory_remember`), persisted identifiers.

Reference commit (checked out): `48a2197b47cc049f0ff4e422d31f1ba3e07924a6`.
Upstream source commit referenced (NOT present in repo): `78506708aae344635e01a24f67a7319efc36fce9` (PyPI `mnemosyne-memory 3.15.1`).

## Completed deliverables

1. **Pinned release reference** — `mnemosyne-memory 3.15.1` is the target upstream artifact. It is MISSING from the repository and remains an explicit blocker for any adoption/rebuild that requires its source. Documented in `docs/plans/item_02_python_baseline.md`, `docs/plans/item_07_deploy_repro.md`, `.auto/deliverables/item_02_python_baseline.md`, and below.
2. **Baseline verification script** — `scripts/baseline/verify_baseline_install.sh` preserved as executable design (not executed against a deployed binary because `mnemosyne` binary and upstream source are unavailable). No fabricated verification output added.
3. **Parity matrix** — `plans/parity_matrix.md` maps live capabilities to implementation/test/completion status. All rows show BLOCKED or DESIGN ONLY until upstream source and DB clone are available.
4. **Contradictory documentation replaced/archived** — `docs/plans/item_01_m0_baseline.md` (this file) provides a single, consistent M0 reference. Obsolete/stale docs (`docs/archive/IMPLEMENTATION_COMPLETE.md`, `docs/archive/RUST_ARCHIVE_REF.md`) preserved as archive; not updated unless explicitly targeted.
5. **Unsafe init disabled** — No new production initialization path added through the simplified backend. The existing adapter (`mnemosyne_rust_hermes`) requires a deployed binary (`MNEMOSYNE_BIN`). Without that binary and without the upstream `mnemosyne-memory` source, no write path to an unverified schema is possible. The design guard holds.

## Explicit blockers (not presumed parity)

- `mnemosyne-memory 3.15.1` upstream source URL/reference: NOT FOUND in repo (`docs/plans/item_02_python_baseline.md`, `.auto/deliverables/item_02_python_baseline.md`).
- Container image digest / Hermes binary sha256: NO EVIDENCE in repo (`.auto/evidence/claim_001.json` is empty/placeholder).
- Deployed binary version (`mnemosyne --version` output): NOT AVAILABLE in repo (`which mnemosyne` returns nothing; `python -c "import mnemosyne_memory"` fails).
- Real DB file (`mnemosyne.db`) for integrity/test: NOT FOUND in repo (`find . -name '*.db'` excludes repo-level DB; no `MNEMOSYNE_DB_PATH` DB present in working directory or `~/.hermes/mnemosyne/data/`).
- September 13 backup file: NOT FOUND (confirmed absent by `find` searches); must be regenerated from current DB state on deployed host or left as unconfirmed.
- Patch `.auto/evidence/link_type_fix.patch` exists but upstream context for conversion (`.patch` → `.diff`) is missing.

## Database health baseline (design, not executed)

The design requires:
- `sqlite3 <DB> "PRAGMA integrity_check;"` → must return `ok`.
- `sqlite3 <DB> ".backup to /tmp/isolate/restore_target.db"` → must succeed.
- `sqlite3 /tmp/isolate/restore_target.db "PRAGMA integrity_check;"` → must return `ok`.
- Per-table counts and deterministic typed-content hashes must match before any migration or rebuild.

These checks are NOT executed in this deliverable (no DB file available). They are recorded as required pre-conditions in `scripts/baseline/verify_baseline_install.sh` and `docs/plans/item_01_backup_auth_fix.md`.

## What was NOT done (intentionally minimized per ponytail rules)

- No upstream source fetched or fabricated.
- No DB rebuilt, migrated, or copied without consistent snapshot state.
- No M1 (shared Python core) implementation edits (`src/` unchanged; no CLI/MCP/Hermes wiring changes).
- No M2 (data model) migration design applied (no source schema to adopt).
- No M3 (capture/knowledge) or M4 (retrieval/embedding) or M5 (maintenance/security) work started.
- No contradictory docs deleted (preserved in `docs/archive/` and `docs/plans/`); only a single consistent M0 reference added.

## Next smallest step (recommended)

Fetch/verify the upstream `mnemosyne-memory 3.15.1` source from PyPI or its source repository (`https://pypi.org/project/mnemosyne-memory/3.15.1/`). Once available, execute `scripts/baseline/verify_baseline_install.sh` in an isolated environment and capture:
- Installed binary version (`mnemosyne --version`).
- Container digest (`docker inspect` / `podman inspect`).
- Actual DB file at `MNEMOSYNE_DB_PATH` (copy through SQLite `.backup`, verify `integrity_check`, capture per-table counts and content hashes).
- Real adapter contract test results (provider id, namespace, checkpoint version, tool schemas).

Only after these artifacts exist should M1 (shared Python core + executable) proceed.
