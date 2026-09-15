# M0 Summary — What was done, what's blocked, what's next

Created at: `48a2197`. Ponytail full mode active (lazy/minimal). No destructive source edits; `src/` unchanged.

## Completed (observable artifacts)

- `docs/plans/item_01_m0_baseline.md` — M0 deliverable with explicit blockers documented.
- `plans/M0_BASELINE.md` — this file: summary, completed/blocked/next.
- `plans/parity_matrix.md` — capability mapping (all implementation/test rows: design/blocked until upstream source + DB clone exist).
- `scripts/baseline/verify_baseline_install.sh` — preserved executable design; status message updated to reference M0 artifacts and blockers.

## Blocked (explicit, not hidden)

- `mnemosyne-memory 3.15.1` upstream source: MISSING. Not fabricated. Next step: fetch from PyPI or source repo (see `docs/plans/item_02_python_baseline.md`, `.auto/deliverables/item_02_python_baseline.md`).
- Deployed binary (`mnemosyne` / `MNEMOSYNE_BIN`): NOT AVAILABLE (`which mnemosyne` empty; `python -c "import mnemosyne_memory"` fails). Next step: install from release binary or build after upstream source available.
- Real DB file (`MNEMOSYNE_DB_PATH` or default `~/.hermes/mnemosyne/mnemosyne.db`): NOT FOUND in repo. Next step: locate on deployed host; create consistent `.backup` snapshot; verify `PRAGMA integrity_check`; capture per-table counts and content hashes.
- September 13 backup: NOT FOUND. Next step: regenerate from current DB if DB exists.
- Patch conversion (`.patch` → `.diff`) upstream context: MISSING (only `.auto/evidence/link_type_fix.patch` present; no upstream reference for unified diff context).

## Not done (intentionally, per ponytail + M0 scope)

- No M1 (shared Python core + executable): `src/` unchanged; no CLI/MCP/Hermes wiring changes.
- No M2 (data model adoption): no upstream schema to adopt; no migrations executed.
- No M3 (capture/knowledge pipeline): not implemented.
- No M4 (retrieval/embedding parity): not implemented; embedding identity (`bge-small-en-v1.5`) preserved as contract reference only.
- No M5 (maintenance/security/self-host): design preserved only.
- No contradictory docs deleted (preserved as archive/reference); only a single consistent M0 reference added.

## Assumption (disclosed)

Because the user said "proceed and complete all items" without clarifying which milestone/item, and the upstream source is an explicit M0 blocker per the spec (`docs/plans/item_02_python_baseline.md`: "The `mnemosyne-memory 3.15.1` upstream source is MISSING... must be fetched externally"), the smallest safe default was M0 artifacts only. Any M1–M5 work requires resolving the upstream source blocker first.

## Next smallest step (recommended — requires user authorization to proceed beyond M0)

Fetch `mnemosyne-memory 3.15.1` source (or confirm an alternative upstream reference). Once available:
1. Execute `scripts/baseline/verify_baseline_install.sh` in an isolated venv (after building/installing the binary).
2. Capture `mnemosyne --version`, container digest (`docker inspect`), and adapter contract results.
3. Locate DB at `MNEMOSYNE_DB_PATH`; run `.backup`, `integrity_check`, and per-table count/hash capture.
4. Only after (1)–(3) complete, proceed to M1 (shared core + executable) with verified baseline.
