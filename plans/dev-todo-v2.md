# PLAN — Dev Team TODO v2: storage safety (Track A) + canonical provider (Track B)

Source: `mnemosyne-hermes — Dev Team TODO (v2)` (read-only production review, 2026-09-18, review repo `main @ e84bd3e`).
This repo is at `main @ 1baeba6`, clean tree. Planning session is read-only.

## 1. Context

Two problems, one cause: the Hermes provider and the repo drifted apart, and the public lite surface can
damage an existing database.

- **Data safety.** `src/lib/storage.py` mutates a database *before* validating its shape, deletes real
  memories on a 50-char prefix match, and silently leaves the WAL unbounded. Two of these are permanent
  partial mutations on a foreign DB. The lite surface (`src/lib` + `src/mnemosyne`) is public-facing today.
- **Flagship broken.** The one provider with real recall quality (vectors + FTS + graph, ~40 tools) is a
  byte-identical copy of `mnemosyne-memory 3.15.1`'s `hermes_memory_provider`, **not in the repo**, and the
  repo's documented install path cannot even load a provider (§B5). The repo's own slim provider
  (`integrations/hermes/src/mnemosyne_hermes/provider.py`, 280 L, `lib.storage`-backed) is the wrong
  provider to be canonical: keyword-only recall, and it shares every Track A storage bug.

Nothing in the repo currently runs the flagship, so the repo's own promise is untested by dogfooding.

### Verified in this session (HEAD `1baeba6`)

| Fact | Evidence |
|---|---|
| No `Cargo.toml`, but CI is 100% cargo (fmt/clippy/cargo test/features) → dead | `.github/workflows/ci.yml` |
| No `Makefile` (only `Makefile.archive`); `AGENTS.md` still references Makefile targets | root listing |
| Two provider dirs coexist and neither is the flagship: `integrations/hermes` (`mnemosyne_hermes` 0.1.0, 280-L slim, imports `lib.storage`) and `integrations/hermes-memory-provider` (`mnemosyne_rust_hermes`, Rust-era MCP-client, declares `hermes_agent.memory_providers` entry point the loader ignores) | `integrations/*` |
| **No `mcp` subcommand and no `mcp.py` at HEAD** — the CLI is the whole lite surface; the review's "mcp.py builds storage outside the request loop" item has no target here | `src/mnemosyne/cli.py` (227 L, subcommands: init/remember/recall/list/bootstrap/embed/migrate/backup/restore/maintenance/diagnostics) |
| Existing storage regression suite to extend, not replace | `tests/test_python_hardening.py` (248 L, 9 tests, runs under plain `python3` or pytest) |
| `resolve_db_path()` already validates `DATABASE_URL`/scheme and is **not used by the CLI** | `src/lib/mnemosyne_client.py:14` |
| Engine + flagship present in the local Hermes venv: `mnemosyne_memory-3.15.1.dist-info`, `hermes_memory_provider/` 7 files / 5217 L, `register(ctx)` L3774, `register_memory_provider(ctx)` L3764, `is_available` L1509, `class MnemosyneMemoryProvider(HermesPersonaPromptMixin, MemoryProvider)` L1321 with no-arg `__init__` L1337 | `~/AppData/Local/hermes/hermes-agent/venv/Lib/site-packages` |
| Flagship is **7/7 byte-identical to the wheel RECORD** (sha256 base64url computed this session) | same |
| Live symlink `~/.hermes/plugins/mnemosyne` → `site-packages/hermes_memory_provider` | `ls -la ~/.hermes/plugins` |
| `text_learning.py` (review's "local addition") is **absent on this host** | `find` over `~/` |
| Loader contract: user providers live in `$HERMES_HOME/plugins/<name>/`, need `__init__.py` containing `register_memory_provider` **or** `MemoryProvider`, tries `register(ctx)` with a collector that only honours `register_memory_provider`, then falls back to instantiating the first `MemoryProvider` subclass; **module cached in `sys.modules` by name, reused if it has `__file__`** | `hermes-agent/plugins/memory/__init__.py:43-99,183-320` |
| `install.sh` symlinks the **repo root** into `~/.hermes/plugins/mnemosyne` — no `__init__.py` there → `_is_memory_provider_dir()` False → `load_memory_provider("mnemosyne")` returns `None`. The documented install path cannot work | `install.sh:40-57`, loader above |

### Root causes pinned (not symptoms)

- **A1 partial mutation:** Python's `sqlite3` in legacy isolation mode opens a transaction for DML only, not
  DDL — `ALTER TABLE memories ADD COLUMN content_lower` (L292) autocommits immediately, then the backfill
  `UPDATE`+`commit()` (L299-302) runs before anything checks whether `memories` is really our table. On a DB
  whose `memories` lacks `namespace`, the next query dies and the mutation is already permanent.
- **A3 cold-start race:** `PRAGMA journal_mode=WAL` (L144) runs *before* `PRAGMA busy_timeout=5000` (L159),
  and SQLite's journal-mode change returns `SQLITE_BUSY` without invoking the busy handler when another
  connection holds an incompatible mode. Construction (`__init__` L126-136) does this eagerly and
  unguarded → 16 simultaneous opens die at construction.
- **B3 registration:** `register(ctx)` (L3774) registers a CLI command and then tries sibling `hermes_plugin`
  inside `try/except: pass`; it never calls `ctx.register_memory_provider(...)`. The loader's collector
  therefore captures nothing and the provider only works via the fallback class scan. Two entry points,
  one of which is decorative.

## 2. Decisions (locked)

- **D1 — upstream: vendor + attribution + sync policy.** Repo owns a versioned snapshot of
  `hermes_memory_provider` from `mnemosyne-memory 3.15.1`; upstream stays the source of truth; local patches
  live in an explicit layer; sync is a scripted, gated task. Escalation trigger to a formal fork: local
  patch surface exceeds ~15% of vendored lines, or upstream diverges/abandons the Hermes path.
- **D2 — keep the lite surface** (`src/lib` + `src/mnemosyne`), fixed and labelled "standalone CLI, not a
  Hermes provider".
- **D3 — scope: Track A + Track B** in one plan, executed in that order, as independent commits. Deferred to a
  follow-up "release prep" pass: full CI replacement, docs overhaul, version single-source + tag, dead-weight
  deletion, PyPI naming/publishing, lite feature work.
- **D4 — verification host: this Windows dev box.** The engine, the flagship, the editable installs and the
  live symlink are all inspectable here. Any live-gateway action (symlink swap, gateway restart) is a
  separately authorised step, not part of this plan's default execution.
- **Fallback:** if Track B stalls on the contract audit, Track A ships alone.

## 3. Track A — Storage safety (no gates; ship first)

One file plus the CLI. All fixes are small and in-place; no new abstractions, no new deps.

- **A1 Guard-before-mutate (BLOCKER).** Before any DDL/DML in `_init_schema`, classify the target:
  absent/0-byte file → fresh store; else require the Mnemosyne sentinel and shape (add
  `MNEMOSYNE_USER_VERSION = 1`, set via `PRAGMA user_version`; require `memories` to have
  `id, content, namespace, importance`; if the table exists but the shape is foreign → raise
  `StorageSchemaError` naming the observed columns, having written nothing). Run the ALTER + backfill +
  `user_version` bump inside **one explicit `BEGIN IMMEDIATE` … `COMMIT`** with `ROLLBACK` on error, using
  `isolation_level=None` (or `BEGIN` before DDL) so the ALTER is actually transactional. Reject non-SQLite
  files (header check) before opening.
- **A2 WAL counter (BLOCKER).** Move the counter entirely onto `self._local` (write
  `self._local._checkpoint_counter = getattr(self._local, "_checkpoint_counter", 0) + 1` and compare that).
  Keep write-path-only checkpointing and the 4 MB bound.
- **A3 Cold-start race (BLOCKER).** In `_new_conn`: set `busy_timeout` first, then read
  `PRAGMA journal_mode`; only attempt the WAL change when it isn't already `wal`, with a small bounded retry
  (e.g. 5 × 50 ms) and tolerance when another connection already holds WAL. Make construction resilient:
  retry `_init_schema` on `SQLITE_BUSY`, and on failure close the connection and raise a clear
  `StorageError` (no bare `OperationalError`, no half-open state). The CLI catches it and prints one line +
  exit 1 instead of a traceback.
- **A4 Destructive dedup.** `consolidate(auto_apply=True)` deletes **only exact duplicates** —
  same `namespace` and identical normalized full `content`; delete in one transaction, keeping the
  highest-importance/first row, and re-count before returning. The 50-char-prefix grouping stays as
  *proposals only* (`duplicate_groups`), never as a delete criterion. CLI `--auto-apply` requires an explicit
  confirmation (`--yes` or interactive) and prints every candidate (id + 80-char preview) first.
- **A5 `_row_to_dict` name-based.** Map by column name (`row.keys()`), not position, so a legacy/foreign
  column order can't silently shift fields. Keep the existing dict keys as the public contract.
- **A6 Empty-store guard + `--db-path` ordering.** Route the CLI through `resolve_db_path()` (already exists,
  `src/lib/mnemosyne_client.py:14`). Read-only commands (`recall`, `list`, `bootstrap`, `diagnostics`,
  `maintenance`) refuse when the DB file is missing instead of fabricating a fresh one; only `init` and
  `remember` may create. Add `--db-path` to each subparser via a shared `parents=[common]` parser so
  `mnemosyne remember --db-path X` works, keeping the top-level flag for compatibility.
- **A7 Tooling truth.** `diagnostics` reads live `PRAGMA journal_mode` / `synchronous` / `user_version` from a
  real connection instead of hardcoding `"synchronous": "FULL"`, and reports the version from package
  metadata rather than the literal `"2.4.0"`. `restore` either accepts gzipped SQL dumps or refuses with a
  message that names the accepted format (today it tries to `sqlite3.connect()` a `.gz` and fails
  confusingly); `backup` keeps writing a real SQLite backup via the `.backup` API.
- **A8 Tests + docs.** Extend `tests/test_python_hardening.py`: foreign-shaped DB refuses with **zero** writes
  (assert file bytes + no `-wal` before/after), legacy store migrates atomically (interrupt mid-migration →
  unchanged), WAL stays under bound over sustained writes, 16 concurrent cold opens succeed, prefix-identical
  memories are *not* deleted while exact duplicates are. Document the ~3–4× size growth of the
  `content_lower` index migration in `README.md` + the `migrate` help text.

## 4. Track B — Canonical provider (gated on the contract audit)

### B1 Vendoring mechanics (the six plumbing items)

1. **Pin + hash manifest.** Land the 7 files under `integrations/hermes-provider/hermes_memory_provider/`
   (package name unchanged — the vendored `__init__.py` imports `hermes_memory_provider.*` absolutely).
   `VENDORED_FROM.json`: source package `mnemosyne-memory`, version `3.15.1`, upstream URL
   `https://github.com/AxDSan/mnemosyne`, per-file sha256, plus the wheel RECORD check command. A test in CI
   fails when a vendored file drifts from the manifest without a manifest update in the same commit.
2. **Local patch layer.** Patch markers `# LOCAL PATCH:` + `integrations/hermes-provider/PATCHES.md`
   (date, upstream file, reason, upstream-PR status). Step 0: reconcile the review's `text_learning.py`
   (absent on this host) — either the snapshot is exactly 7 files or that file joins the patch layer.
3. **Sync policy as a task.** `scripts/vendor-provider-sync.sh`: fetch the new wheel, diff vendored vs new,
   print per-file drift, exit non-zero if the manifest is stale. Documented in the provider README as the
   required procedure on every upstream release.
4. **License hygiene.** `NOTICE` at repo root + a header line in the vendored README: "Portions vendored from
   mnemosyne-memory 3.15.1 © Abdias J (AxDSan), MIT". Local patches stay MIT.
5. **Registration discipline.** The vendored copy is *the* `mnemosyne` provider. Docs and the installer must
   state that upstream's bundled copy must not also be installed; `install.sh` verifies the plugin path points
   at the vendored package before writing config.
6. **Escalation trigger** (D1) recorded in `PATCHES.md`.

### B2 Contract audit (before canonicalisation)

Audit the vendored file against the ABC in `hermes-agent/agent/memory_provider.py`. Record findings in
`integrations/hermes-provider/CONTRACT_AUDIT.md`, then fix in the patch layer:

- `name` property → `"mnemosyne"`; `is_available()`; `initialize(session_id, **kwargs)`;
  `system_prompt_block()`; `prefetch(query, *, session_id="") -> str`; `sync_turn(...)`;
  `get_tool_schemas() -> List[dict]`; `handle_tool_call(tool_name, args, **kwargs) -> str` (JSON string);
  `shutdown()`; `on_pre_compress(messages) -> str`; `backup_paths()`.
- Specifically check: tool results are JSON strings and never bare objects; `recall_status` /
  `identity_signature` / `on_pre_compress` semantics against what the manager expects; tool-schema shapes vs
  `handle_tool_call` dispatch names; error paths return a JSON error string rather than raising.
- ABC conformance is currently implicit: `MemoryProvider` is imported inside a `try/except` that falls back to
  `object` (L1265-1270) — a vendored build with no Hermes present silently becomes a non-provider. Make that
  a loud, recorded condition instead of a silent fallback.

### B3 One registration

- `register(ctx)` must call `ctx.register_memory_provider(MnemosyneMemoryProvider())` (guarded) so the loader's
  collector path works; keep the `MemoryProvider` subclass so the fallback scan also works. No third entry point:
  drop the `hermes_agent.memory_providers` entry-point from the Rust-era provider and retire
  `integrations/hermes`' provider role (remove `provider.py` + its editable install; leave a one-line pointer
  to the vendored package).
- Assert in tests: loading the plugin yields exactly one provider, `name == "mnemosyne"`, no dual registration.

### B4 Fail loud, never hollow

- `is_available()` must probe the engine import *and* expose the reason (e.g.
  `unavailable_reason() -> str` capturing the `ImportError` text) instead of the bare `False` at L1509.
- The provider's own `hermes mnemosyne doctor` (`cli.py:135`) must exit non-zero when the engine is missing;
  today it prints passed-checks and swallows the exception into `print(f"Diagnostic failed: {e}")` + `return 1`
  only in the outer `except`. Make the missing-engine case an explicit FAIL with the install command.
- Loader smoke test (CI): import the plugin with (a) no `mnemosyne` engine → `is_available()` False with a
  reason and doctor FAILS; (b) engine present → exactly one provider registered as `mnemosyne`. Run it against
  the *vendored* directory, not site-packages.

### B5 Install story

`install.sh` is currently broken (§1). Rewrite it to:

- install the vendored package into the Hermes venv with `uv pip install` (works for pip-less / root-owned
  `/opt/hermes/.venv`-style layouts; detect the venv by `$HERMES_HOME`, `VIRTUAL_ENV`, and
  `/opt/hermes/.venv` candidates rather than assuming `pip`);
- symlink `$HERMES_HOME/plugins/mnemosyne` → the vendored package dir (the dir that actually contains
  `__init__.py` with `register_memory_provider`), and verify the loader can find it;
- never rewrite a whole `config.yaml` (merge the `memory.provider: mnemosyne` key; back up first);
- never create or open the DB before the user confirms the resolved path — print
  `MNEMOSYNE_DB_PATH` and the fallback path, and require `--yes` for non-interactive runs;
- pin the engine (`mnemosyne-memory[embeddings]>=3.15.1,<3.16`) and document that the provider imports engine
  internals (`mnemosyne.core.*`, `mnemosyne.batch_tool`, `mnemosyne.hermes_config`,
  `mnemosyne.integrations.hermes_persona_prompt`), so the range is a real upgrade policy.

### B6 Minimum docs delta (not the full rewrite)

`integrations/hermes-provider/README.md` (install, provider id, DB paths, sync policy), plus edits only where
the current text is actively false: `install.sh` usage, `README.md`'s provider section, and
`docs/HERMES_INTEGRATION.md:136-137` ("This repository does not package it"). The full docs overhaul stays in
the follow-up pass.

### B7 Live re-verify (separately authorised; D4)

Engine in the Hermes venv → symlink at the vendored dir → gateway restart → `hermes doctor` PASS in the
gateway venv → prefetch returns real rows from the intended store → one `sync_turn` visible in that store →
no double-writes with the lite CLI surface → compression checkpoint exercised → tool count logged by the
manager. Document the gateway module-cache rule (provider code changes need a restart; fresh CLI probes mask
staleness) in the provider README.

## 5. Files to modify

**Track A**
- `src/lib/storage.py` — A1, A2, A3, A4, A5
- `src/mnemosyne/cli.py` — A3 exit path, A4 confirm gate, A6, A7
- `src/lib/mnemosyne_client.py` — reuse `resolve_db_path`; no behaviour change expected
- `tests/test_python_hardening.py` — A8 (extend)
- `README.md` — A8 size-growth note + lite-surface labelling (D2)

**Track B**
- New: `integrations/hermes-provider/{README.md,NOTICE,PATCHES.md,VENDORED_FROM.json,pyproject.toml,CONTRACT_AUDIT.md}`
  and `integrations/hermes-provider/hermes_memory_provider/*.py` (7 vendored files, byte-identical first commit)
- New: `scripts/vendor-provider-sync.sh`, `tests/test_provider_loader.py`
- `install.sh` — B5
- `integrations/hermes/src/mnemosyne_hermes/provider.py` — retire the provider role (pointer + removal)
- `integrations/hermes-memory-provider/pyproject.toml` — drop the dead entry point
- `docs/HERMES_INTEGRATION.md`, `README.md` — B6 minimum edits
- `.github/workflows/ci.yml` — add only the two new tests to whatever runner is used in this pass (full CI
  replacement is deferred)

## 6. Reuse — do not rebuild

| Need | Existing thing |
|---|---|
| Storage under test | `PythonMemoryStorage` (`src/lib/storage.py`), `MNEMOSYNE_DB_PATH` handling |
| Path validation / scheme rejection | `resolve_db_path()` (`src/lib/mnemosyne_client.py:14`) + `tests/test_python_hardening.py::test_resolve_db_path_rejects_non_sqlite` |
| Regression harness | `tests/test_python_hardening.py` (plain-assert + pytest dual mode), `test-all.sh` |
| Provider ABC + loader | `hermes-agent/agent/memory_provider.py`, `hermes-agent/plugins/memory/__init__.py` (read-only, upstream) |
| Engine + flagship artifact | `mnemosyne-memory 3.15.1` in the local Hermes venv; the RECORD-based byte check already validated 7/7 |
| Provider CLI/doctor | vendored `hermes_memory_provider/cli.py:135` |

## 7. Steps

**Track A — storage safety (independent commits, no gates)**

- [x] A0 Reproduce: foreign-shaped DB (memories without `namespace`) → confirm `content_lower` is added and rows
      rewritten before the failure; 16 concurrent cold opens → confirm the crash. Capture before/after file hashes.
- [x] A1 Schema classification + sentinel + single-transaction migration (`BEGIN IMMEDIATE`, `user_version`, fail closed).
- [x] A2 Fix the checkpoint counter (`self._local`); bound WAL under sustained writes.
- [x] A3 Bound `journal_mode` change behind the busy timeout/retry; resilient construction; CLI prints one error line.
- [x] A4 Exact-duplicate-only deletion + confirmation gate; prefix groups remain proposals.
- [x] A5 Name-based row mapping.
- [x] A6 `resolve_db_path` in the CLI + empty-store refusal + per-subcommand `--db-path`.
- [x] A7 Live pragmas + real version in diagnostics; `restore` accepts `.gz` or refuses clearly.
- [x] A8 Tests (A0 cases + dedup + concurrency) green via `python tests/test_python_hardening.py` **and** pytest;
      size-growth + standalone-only labelling in `README.md`.

**Track B — canonical provider (gated on B2)**

- [x] B0 Reconcile the artifact: confirm the 7 files + whether `text_learning.py` exists on the review host;
      state the snapshot exactly.
- [x] B1 Vendor into `integrations/hermes-provider/` with manifest, NOTICE, PATCHES layer, sync script; CI drift check.
- [x] B2 Contract audit written to `CONTRACT_AUDIT.md`; fixes applied in the patch layer only (no silent upstream edits).
- [x] B3 Single registration + retire the slim provider and the dead entry point.
- [x] B4 `is_available()` reason + doctor FAIL on missing engine; loader smoke test in CI (bare venv and engine venv).
- [x] B5 `install.sh` rewritten (uv, venv detection, symlink to the real package, config merge, DB-path confirm, engine pin).
- [x] B6 Minimum docs delta (provider README + the false statements only).
- [x] B7 Live re-verify checklist executed **only when separately authorised**; results recorded with the exact
      commands and observed rows.

## 8. Verification

**Track A (runnable here, no live data)**

```bash
python tests/test_python_hardening.py          # plain asserts, no pytest required
python -m pytest tests -m 'not integration' -q # repo suite
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
```

- Foreign DB → `StorageSchemaError`, file hash unchanged, no `-wal`/`-shm` created.
- Legacy-store migration interrupted mid-way → DB unchanged (atomic).
- 16 threads × cold `PythonMemoryStorage(path)` → all succeed, WAL bounded after sustained writes.
- `consolidate(auto_apply=True)` on two memories sharing a 50-char prefix → nothing deleted; exact duplicates → one row per group.
- `mnemosyne recall --db-path /nonexistent/mnemosyne.db` → refuses, exit 1, no file created.
- `mnemosyne diagnostics` on a live store → prints the real `journal_mode`/`synchronous`/`user_version`.

**Track B (static + loader-level here; live only when authorised)**

- Manifest drift check fails when a vendored file is edited without the manifest.
- Loader smoke test: bare venv → `is_available()` False + reason, doctor non-zero; engine venv → one provider named `mnemosyne`.
- `install.sh --dry-run` prints the resolved venv, DB path, symlink target; refuses to write before confirmation.
- Post-swap live checklist (B7) is **unverified** until executed on a host with the gateway; say so.

## 9. Out of scope (follow-up "release prep" pass)

Full Python CI replacement + `check_notes.sh`/`claude*.yml` decisions; docs overhaul (false claims table, real DB
paths, 11-tool table, `mcp.servers` vs `mcp_servers`, QUICK_START fiction); version single-source
(2.4.0 / 2.2.0 / 0.1.0 + vendored source version) + changelog + tag; dead-weight deletion (`Makefile.archive`,
`patches/`, `proto/`, `plans/`, `spec.md`, `deploy/`, `benchmark/`, Rust-era `tests/e2e`+`tests/manual`,
17 `*_AUDIT_TASK_*.md`, `*.egg-info`, `uv.lock` refresh); PyPI naming (`mnemosyne-hermes` is upstream's);
lite-surface feature work; adding a `mnemosyne mcp` server (doesn't exist at HEAD — do not document a surface
that isn't there).

## 10. Risks

- **A1 is the riskiest edit.** The sentinel/shape check must accept every DB the current code legitimately
  created (including a DB already migrated to `content_lower` and one created pre-`user_version`), or it
  locks users out of their own store. Mitigation: classify by table shape first, set the sentinel as a
  backfill for recognized stores, and test both legacy shapes before the foreign-refusal case.
- **Vendored-engine coupling.** The provider imports engine internals; the pin is a real contract. If the
  range is loosened later, the smoke test must import the engine by version and fail loudly on mismatch.
- **Review-host facts not reproducible here** (the live symlink pointing at the slim rewrite, `text_learning.py`,
  `/opt/hermes/.venv`). The plan records this host's state as evidence; the Linux/Docker path is documented,
  not proven, until B7 runs there.

---

## 11. Verification log (v3 reviewer pass) — host-pinned

**Host pin:** `AFS_166`, Windows 10, CPython 3.11.15, SQLite 3.53.1,
hermes-agent 0.18.2, engine `mnemosyne-memory 3.15.1`. All numbers below are from
this host; anything host-dependent is labelled as such.

| # | Item | Reconciled result | Command |
|---|---|---|---|
| V2 | Refusal corpus | **137/137** banks refused, **0** byte-level mutations, **0** `-wal` sidecars. Census: 137 `*.db` files under `~/.mnemosyne`, all 137 with a `memories` table, all 137 with a `namespace` column, all exactly 24 columns. The "200 banks" figure is **not reproducible here** — different host. | `python -c` census + read-only copies through `PythonMemoryStorage` |
| V6a | Bank count | 137 on AFS_166 (the original claim); corpus test added as an opt-in check (`MNEMOSYNE_BANK_CORPUS`, skips in CI). | `python tests/test_python_hardening.py` |
| V6b | Shadowing (my claim was **wrong**) | Real manifestation is file-level, not package-level: exactly **2** paths collide — `mnemosyne/__init__.py`, `mnemosyne/cli.py`. Overwriting them leaves `import mnemosyne.core.beam` **working** (the engine's `core/` survives) but breaks `from mnemosyne import Mnemosyne` (**ImportError**), changes `mnemosyne.__version__` to the repo's, and replaces `Scripts/mnemosyne` (engine `run_cli` → repo `main`). Separately, with `src/` first on `sys.path`, `import mnemosyne.core` raises `ModuleNotFoundError`. README corrected; `install.sh` guard kept. | engine-tree copy + repo files overwritten, then import test |
| V6c | `test-all.sh` claim | **Green here, not 1/21**: `71 passed, 45 skipped`; adapter suite `21/21 OK` under four invocations (`unittest discover -t .`, `pytest`, `pytest -I`, and discovery from a foreign cwd). The "green only via contamination" theory does not hold for these commands on this host. | `./test-all.sh --skip-llm`, `python -I -m pytest integrations/hermes-memory-provider/tests -q` |
| V3 | Manifest + drift gate | 7/7 byte-identical to the wheel RECORD at vendor time; drift gate fails on a one-byte change and the sync script reports `DIFFERS`. | `python tests/test_vendored_provider.py`, `scripts/vendor-provider-sync.sh` |
| V4 | Loader protocol | Exactly one provider, `name == "mnemosyne"`; bare venv (engine blocked) imports, `is_available() is False` with reason, `doctor` → 1. | `python tests/test_provider_loader.py` |
| V5 | `install.sh` | `--dry-run` leaves `HERMES_HOME` empty (no symlink, no config); non-interactive without `--yes` exits 1. | `./install.sh --dry-run --hermes-home "$(mktemp -d)"` |
| V6d | Cold-start crash | **Not reproducible here**: 0/96 failing opens (6 rounds × 16 processes). Mechanism pinned instead: `journal_mode=WAL` ran before `busy_timeout`, and a lock outliving the timeout fails the open. | 6 × 16 concurrent `PythonMemoryStorage` processes |
| D6 | Engine patch parity | On this host **every** `mnemosyne/*.py` file hashes equal to the wheel RECORD, and there are **0** engine `.py` files the wheel did not ship. `hardening.py`, `hotness.py`, `memory_diff_audit.py` do not exist here (the RECORD's only "hardening" hit is `integrations/hermes/tests/test_prefetch_hardening.py`). The divergence must be re-checked on the live host during the swap. | per-file sha256 vs RECORD |
| — | Live state | **Untouched**: `~/.hermes/plugins/mnemosyne` still → `site-packages/hermes_memory_provider`; the installed provider is 7/7 byte-identical to the wheel (my patches went only into `integrations/hermes-provider/`). | `readlink -f`, sha256 vs RECORD |
| — | Latent defect found | The **retired** adapter's tool path imports top-level `lib.storage` (`mnemosyne_rust_hermes/provider.py:149`), which only resolves when `src/` is on `sys.path`. Retired as a provider, so recorded, not fixed. | grep |

### v3 decisions applied (2026-09-18)

| Decision | Taken | Implemented as |
|---|---|---|
| V0 | commit + push to a feature branch, no PR | `fix/storage-safety-and-canonical-provider` @ `28ccf8f` (pushed to origin) |
| D2 | keep the lite surface | `src/mnemosyne_lite`, still standalone-only |
| D5 | rename the lite distribution/package | `mnemosyne-lite` / `mnemosyne_lite` in `pyproject.toml` + script `mnemosyne-lite`; no longer collides with the engine's `mnemosyne` package or script |
| D6 | accept for now, re-check at the swap | `scripts/engine-parity-check.sh` (81/81 parity here; stray/modified file → exit 1) + LIVE_VERIFICATION step 0 |
| F1 | patch, accept-and-store | P5: `sync_turn(messages=...)`; tool turns stored when `sync_roles` includes `tool` (last 5, 2000 chars, importance 0.2) |
| F2 | patch, accept-and-store | P6: `on_memory_write(metadata=...)` → engine `remember(metadata=...)` |
| F3 | leave unpatched | recorded: the engine has no message-accepting extraction API to forward to |
| backup_paths | no patch (agreed) | recorded: Hermes skips out-of-home paths by design |

Verified after the patches: `python tests/test_provider_loader.py` (3 checks,
including the real `hermes-agent` introspection asserting `messages` is now
accepted and metadata mode is `keyword`), `python tests/test_vendored_provider.py`
(3), `pytest tests -m 'not integration'` (73 passed), `./test-all.sh --skip-llm`
green.

### Still open after this pass

- Live swap (D4): the checklist in
  `integrations/hermes-provider/LIVE_VERIFICATION.md` (now 11 steps + engine
  parity) — symlink re-point, gateway restart, prefetch/sync/db-path acceptance —
  not executed, not authorised.
- `lib` remains a top-level generic import name (no engine collision, but it is a
  collision risk with other `lib` packages). Deferred with the release-prep pass.
- The lite CLI's default DB path (`~/.mnemosyne/mnemosyne.db`) sits inside the
  engine's scratch/bank directory. The A1/A6 guards make it safe (refusal, not
  damage), but a lite-only default would be less confusing.
- Release-prep (out of scope): CI replacement, docs overhaul, version
  single-source, dead-weight deletion, PyPI naming.

### V1 re-run on the pushed branch (fresh clone, hermetic)

Cloned the pushed ref into an empty directory (so no dirty-tree or cross-test
contamination) with `core.autocrlf=true`, which is why `.gitattributes` pins the
vendored files as `-text`: without it a Windows clone gets CRLF and the hash gate
fails for no real reason. In the clone:

```
python tests/test_python_hardening.py      -> 17 checks passed
python tests/test_vendored_provider.py     -> 3 checks passed (bytes survived the clone)
python tests/test_provider_loader.py       -> 3 checks passed
python -m pytest tests -m 'not integration'-> 73 passed, 45 skipped
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -> 21 OK
./test-all.sh --skip-llm                   -> completed, green
bash -n install.sh scripts/*.sh            -> OK
```
