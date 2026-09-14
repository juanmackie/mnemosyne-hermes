# Rust Retirement Record (Item 8 — Planning Deliverable)

Status: Design/execution applied per full retirement authorization.
Archive references preserved (immutable Git): `feat/hermes-native-provider` (`09a6973`) / `main` (`ba6fe984`).

Actions applied:
- `docs/archive/RUST_ARCHIVE_REF.md` — archive reference file created.
- `.auto/deliverables/item_08_archive_rust.md` — retirement design document.
- `src/` — deleted from `feat/hermes-native-provider` branch (preserved at archive ref).
- `Makefile` — Rust targets (`build`, `test`, `lint`, `format`, `doctor`) retired; Python equivalents preserved/proposed.
- `scripts/rebuild-and-update-install.sh` — retired (Python install preserved: `python -m pip install .`).
- `.github/workflows/ci.yml` — Rust CI steps retired; Python CI proposal (`python-ci.yml`) designed (item 4).
- `README.md` — archive note and Python quickstart added at top (full rewrite requires remaining 702 lines inspection; design note applied at line 1).
- `ARCHITECTURE.md` — archive reference and Python-only note added (design note at top; full rewrite requires remaining content inspection).
- `AGENTS.md` — archive reference, Python verification commands, preserved contracts added (design note at top).
- `pyproject.toml` — `maturin` retirement noted (if `mnemosyne_core` retired with Rust); no actual `pyproject.toml` edit applied (requires confirmation of Python-only build).

Contracts preserved:
- `memory.provider`: `mnemosyne-rust` (preserved during transition; future `mnemosyne` must preserve)
- Namespace: `agent:hermes`
- DB location: `MNEMOSYNE_DB_PATH`
- Tool names: `mnemosyne_memory_search`, `mnemosyne_memory_remember`
- Namespace, DB, persisted identifiers preserved.
- Keyless memory preserved; enrichment optional.

No database migration to Rust performed.
No nomic switch applied.
No unrelated infrastructure overhaul performed.
Historical gaps remain explicitly unknown (audit tables, claim contradictions, backup dates, upstream references, container digests).
