# Local patches to the vendored Hermes provider

`hermes_memory_provider/` is a **snapshot** of `mnemosyne-memory 3.15.1`
(`VENDORED_FROM.json` records the source, the per-file sha256 and the pin).
Upstream is the source of truth; this file is the ledger of every deliberate
divergence.

## Rules

1. A vendored file may only differ from upstream if it carries a `# LOCAL PATCH:`
   comment at the changed site **and** an entry below.
2. Every entry records: upstream file, date, reason, and whether the fix was
   offered upstream (PR link or "not sent").
3. `tests/test_vendored_provider.py` fails on an undeclared patch or any hash
   change that is not accompanied by a `VENDORED_FROM.json` update, so a sync
   cannot silently drop a local fix.
4. When upstream accepts a fix, delete the patch from this file (and the marker),
   re-sync, and update the hashes.

## Patches

### P1 — `register(ctx)` registers the provider

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`register`) |
| Date | 2026-09-18 |
| Reason | Upstream's `register(ctx)` only registered the `mnemosyne` CLI command, so `plugins.memory.load_memory_provider()` captured no provider from the collector and reached the provider solely through its fallback scan for a `MemoryProvider` subclass. One provider must have one registration path. |
| Upstream | Not sent yet — generally useful (affects every Hermes install of this provider); candidate for an upstream PR. |
| Remedy in sync | If upstream adopts it, drop the marker, delete this entry, re-vendor and update the hashes. |

### P2 — tolerant engine import + reason (`_ENGINE_IMPORT_ERROR`)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (module-level engine imports) |
| Date | 2026-09-18 |
| Reason | The five engine imports were unconditional, so a venv without `mnemosyne-memory` raised `ImportError` at import time: the loader returned `None` and the provider could not even report "unavailable" (Hermes silently fell back to built-in memory). The module now imports in a bare venv. |
| Upstream | Not sent yet — generally useful. |
| Safety | Only the two class bases reference engine names at import time (verified by AST scan); every other use is behind `initialize()`/`is_available()`, and `is_available()` returns False whenever the guard tripped, so the placeholders are never reached on an unavailable provider. `_read_config_key` is guarded too (T3): in a bare venv it no longer raises `ModuleNotFoundError` out of `initialize()`. |

### P3 — `unavailable_reason()`

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`is_available`, `unavailable_reason`) |
| Date | 2026-09-18 |
| Reason | `is_available()` returned a bare `False`, making "engine missing" indistinguishable from "not configured". It now records why (including the exact `uv pip install` line) and exposes it. |
| Upstream | Not sent yet — generally useful. |

### P4 — actionable doctor failure

| | |
|---|---|
| File | `hermes_memory_provider/cli.py` (`doctor`) |
| Date | 2026-09-18 |
| Reason | The missing-engine branch printed only `Diagnostic failed: No module named 'mnemosyne'` and gave a public user no next step. It now names the required engine and the install command. |
| Upstream | Not sent yet — generally useful. |

### P5 — `sync_turn` accepts `messages` (F1, accept-and-store)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`sync_turn`, `_VALID_SYNC_ROLES`, config schema) |
| Date | 2026-09-18 |
| Reason | Hermes passes the full turn message list only to providers whose signature accepts it (`MemoryManager._provider_sync_accepts_messages`) and `run_agent.py` does pass it; upstream's signature lacked `messages`, so tool/function turns were never available to the provider. |
| Behaviour | `messages` is accepted and the tool/function turns in it are stored as `source="conversation_tool"`, importance `0.2`, `metadata={"role", "name"}` — bounded to the last **5** messages and **2000** chars each. Off by default: requires `tool` in `sync_roles` (the existing, documented knob, now accepting `user`/`assistant`/`tool`). Default roles keep upstream behaviour byte-for-byte. |
| Upstream | Not sent yet — generally useful. |

### P6 — `on_memory_write` accepts `metadata` (F2, accept-and-store)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`on_memory_write`) |
| Date | 2026-09-18 |
| Reason | Hermes chooses the call style from the signature (`_provider_memory_write_metadata_mode`) and skipped metadata entirely for upstream's 3-arg hook, so the mirror write lost the metadata Hermes had. |
| Behaviour | `metadata` is passed to the engine's own `remember(metadata=...)` parameter (the engine already has one — no new plumbing). |
| Upstream | Not sent yet — generally useful. |

### P7 — `sys.path` shadow guard (T7)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` and `cli.py` (module-top `sys.path.insert`) |
| Date | 2026-09-18 |
| Reason | Both files inserted `Path(__file__).resolve().parent.parent` unconditionally. For a **copied** (not symlinked) install at `$HERMES_HOME/plugins/mnemosyne`, that is the plugins directory, which contains `mnemosyne/` — inserting it shadowed the engine's `mnemosyne` package and every engine import failed with `No module named 'mnemosyne.core'`. Observed while validating the clean-user onboarding path on Windows (MSYS `ln -s` copies). |
| Behaviour | The insert only happens when the sibling dir actually holds `hermes_memory_provider`; `register()` only reaches for `hermes_plugin` when that sibling exists. A symlinked repo checkout is unchanged. |
| Upstream | Not sent yet — generally useful. |

### P8 — `db_path` override (T3)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`__init__`, `_apply_provider_config`, `get_config_schema`, `initialize`) |
| Date | 2026-09-18 |
| Reason | `MNEMOSYNE_DB_PATH` was documented as a preserved contract (README, INSTALL.md, provider README) but nothing read it: `initialize()` always called `BeamMemory(session_id=...)`, so a set env var silently pointed nowhere and the provider used the engine default. |
| Behaviour | Precedence kwargs > `memory.mnemosyne.db_path` > `MNEMOSYNE_DB_PATH` > engine default (`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes`). `db_path` wins over `profile_isolation` (bank) and warns when both are set. Only the Hermes config surface is consulted, not the engine's own config singleton. |
| Upstream | Not sent yet — generally useful. |

### P9 — startup location + Hermes-range logging (T6/T8)

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` (`initialize`) |
| Date | 2026-09-18 |
| Reason | `MNEMOSYNE_DATA_DIR` silently outranks `$HERMES_HOME`, splitting logs from the DB, and no line stated where memory actually lives. Nothing declared the supported Hermes range either, though `hermes mnemosyne` depends on Hermes' plugin CLI discovery internals. |
| Behaviour | One INFO line at init with resolved DB, provider package, engine version and `HERMES_HOME`; a WARNING when the DB sits outside `$HERMES_HOME`; a WARNING when the detected `hermes-agent` version is outside the supported range. Never refuses to start. |
| Upstream | Not sent yet — repo-specific diagnostics. |

### P10 — doctor gates its exit code (T4/T6/T8)

| | |
|---|---|
| File | `hermes_memory_provider/cli.py` (`doctor`) |
| Date | 2026-09-18 |
| Reason | `doctor` printed `Checks passed: 16/43` and returned 0 regardless, so a clean-user acceptance gate was a false green. |
| Behaviour | Five explicit critical checks (engine importable, provider registered exactly once, DB resolved + writable, DB integrity, canonical provider deployed) decide the exit code; the engine's own diagnostics remain informational. The header prints the resolved DB, provider package, engine version and Hermes range. |
| Amendment (2026-09-27) | The "DB resolved + writable" check failed on every fresh install: `_db_writable` required the DB's immediate parent to exist, and `install.sh` deliberately creates nothing. It now walks up to the nearest existing ancestor, checks that one is writable, and reports "will be created". Regression test: `tests/test_provider_db_path.py::test_db_writable_fresh_install_parents_missing`. |
| Upstream | Not sent yet — repo-specific acceptance contract. |

### P11 — CLI `db_path` + provenance check (T3/T7)

| | |
|---|---|
| File | `hermes_memory_provider/cli.py` (CLI `BeamMemory`/`Mnemosyne` construction, `check_provider_provenance`) |
| Date | 2026-09-18 |
| Reason | The CLI opened `BeamMemory(session_id=...)` with no `db_path`, so `hermes mnemosyne stats/sleep/inspect/export/import` could hit a different store than the running provider. The retired `integrations/hermes` tree and copied installs were indistinguishable from the canonical provider. |
| Behaviour | The CLI resolves the same DB path as the provider and passes it to `BeamMemory`/`Mnemosyne`. `check_provider_provenance` requires the plugin target to resolve under `integrations/hermes-provider/hermes_memory_provider`, flags the retired `integrations/hermes` tree, and verifies the `register_cli`/`mnemosyne_command` handler contract. |
| Upstream | Not sent yet — repo-specific provenance. |

### P12 — explicit annotations so static analysis can run (P4-1)

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` (`detect_hermes_version`, `mnemosyne_command`) |
| Date | 2026-09-27 |
| Reason | Three inference artefacts made the provider un-analysable: `import agent` named a module that only exists inside a Hermes install, so it read as an undeclared hard dependency; the inferred `dict[str, str]` for `_beam_kwargs` made `BeamMemory(**_beam_kwargs)` look like a type error; and `beam = None` made every later `beam.<method>()` look like an attribute access on `None`. |
| Behaviour | No runtime change. `import agent` became `importlib.import_module("agent")` inside the same `try`/`except`; `beam` and `_beam_kwargs` carry explicit `Any` / `dict[str, Any]` annotations. Verified: `mypy hermes_memory_provider/cli.py` — no issues (see `pyproject.toml` in this directory for the module-level override that keeps the rest of the snapshot out of the check). |
| Upstream | Not sent yet — generic; upstream carries all three complaints. |

### P13 — a verified copy is an acceptable plugin target (P5-1)

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` (`check_provider_provenance`, `_db_writable`) |
| Date | 2026-09-27 |
| Reason | Windows cannot create a directory symlink without Developer Mode, so `install.sh --copy` places a copy at `$HERMES_HOME/plugins/mnemosyne`. Provenance was a pure path check, so `doctor` refused that install even though the files were byte-identical to the canonical package. The initial marker only hashed `__init__.py` and `cli.py`, leaving helper modules and plugin metadata unverified; installer replacement/uninstall also treated any `PROVENANCE.json` as proof of ownership. Separately, `os.access` raises rather than returning False for a path it cannot encode, which turned a bad DB path into a traceback instead of a failed check. |
| Behaviour | `check_provider_provenance` still refuses the retired `integrations/hermes` tree first, and still requires the canonical path when the plugin really is under `integrations/hermes-provider/`. Otherwise it accepts only format-v1 provenance with an exact SHA-256 inventory of all regular package files (excluding runtime `__pycache__` directories and only the root marker; top-level `.pyc` and nested marker-named files are included); changed, missing, additional, symlinked, malformed, or legacy unversioned copies fail. The installer overwrites/uninstalls a real directory only when this inventory verifies and its recorded source matches the current canonical package path, never based on marker presence alone. The handler-function checks (`register_memory_provider`, `register`, `register_cli`, `mnemosyne_command`) apply to both origins. `_db_writable` gained `_writable`, which reports an unencodable path as "not writable" instead of raising. |
| Upstream | Not sent yet — repo-specific provenance. |

The remaining vendored files are byte-identical to the 3.15.1 wheel RECORD.
`register_memory_provider(ctx)` was already present upstream and is unchanged.

### P14 — supported Hermes range reflects audited releases (P4-2)

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` (`SUPPORTED_HERMES_RANGE`, `TESTED_HERMES_VERSIONS`, `check_hermes_version`) |
| Date | 2026-09-27 |
| Reason | The old range and tested-version list exceeded the published releases actually audited for this provider. Claiming compatibility beyond the audited contract was misleading. |
| Behaviour | The accepted range is now `>=0.18,<0.20`; the tested list is 0.18.2 and 0.19.0. `check_hermes_version` refuses 0.20+ with an out-of-range diagnostic. The API baseline was AST-compared across 0.18.2 and 0.19.0; see `CONTRACT_AUDIT.md`. |
| Upstream | Not sent yet — compatibility support decision. |

### P15 — context-preserving thread helper; keep turn durability under Hermes (P2)

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`spawn_context_thread`, auto-sleep/session-end workers) |
| Date | 2026-09-27 |
| Reason | The supported Hermes releases do not export `spawn_context_thread`; the provider previously started its auxiliary sleep workers with raw `threading.Thread`, losing the caller's `contextvars`. Separately, provider `sync_turn` is synchronous when invoked directly. |
| Behaviour | A local `spawn_context_thread` fallback copies the caller's context into the existing sleep worker call sites. Turn sync remains inline inside the provider because Hermes 0.18.2/0.19.0 already dispatch `MemoryManager.sync_all()` through a single serialized background worker; adding another worker would undermine manager drain/order guarantees. The Linux/macOS Hermes smoke asserts this with a gated slow DB write and round-trip, and the helper's context propagation has a bare test. Direct provider callers must use `MemoryManager.sync_all()` when they need non-blocking behavior. |
| Upstream | Not sent yet — local compatibility helper. |

### P16 — opt-in parent-side delegation capture (P2 hooks)

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`sync_roles`, `on_delegation`) |
| Date | 2026-09-27 |
| Reason | Hermes calls `on_delegation` with the parent-side task/result, but the upstream provider inherited the no-op. Delegation transcripts can contain sensitive data, so capture must not become a new default. |
| Behaviour | Add `delegation` to the existing `sync_roles` allowlist. Only when explicitly enabled does the provider store bounded task/result text (`<=4096` chars) with source `conversation_delegation`; default `['user']` behavior and the `agent:hermes` memory namespace are unchanged. Filter/skip-context checks and the Beam access lock still apply. |
| Upstream | Not sent yet — opt-in local policy. |

### P17 — session and pre-compression hooks

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`on_session_switch`, `on_pre_compress`) |
| Date | 2026-09-27 |
| Reason | The Hermes 0.18.2/0.19.0 ABC exposes these callbacks, but the provider inherited no-ops. The process may rotate transcript IDs without reinitializing the provider. |
| Behaviour | `on_session_switch` updates only the transcript ID and resets per-transcript counters for reset/rewind; it never rotates the persistent memory session namespace. `on_pre_compress` returns bounded user/assistant excerpts and optionally atomically writes a local v1 checkpoint when `require_checkpoint` is set. It limits transcript payload to 100 messages and caps the serialized UTF-8 JSON file at 256 KiB. The checkpoint directory is owner-only (`0700` on POSIX), files use `mkstemp` permissions, and symlinked checkpoint directories are refused on POSIX. On write failure the method raises `CheckpointError`. **Limit:** Hermes MemoryManager catches provider callback exceptions and continues compression, so this cannot be end-to-end fail-closed; schema/docs explicitly say so. `recall_status`/`identity_signature` remain absent because neither supported Hermes version defines/calls them (F9). |
| Upstream | Not sent yet — local hooks. |

### P18 — curated default Hermes tool surface

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`DEFAULT_TOOL_NAMES`, `_configured_tool_schemas`, config schema) |
| Date | 2026-09-27 |
| Reason | Exposing all 40 engine tools by default bloated Hermes' prompt and made advanced/destructive operations available without an explicit deployment decision. |
| Behaviour | Default exposure is the four core tools: remember, recall, stats, and forget. Existing explicit subsets and `[]` remain supported. `memory.mnemosyne.tools: ["*"]` opts into the full 40-tool list; wildcard mixed with names is rejected. Unknown names still fail loudly. The canonical name/default table lives in `docs/HERMES_INTEGRATION.md`; README and AGENTS link to it. |
| Upstream | Not sent yet — local exposure policy. |

### P19 — serialize shared Beam access with background turn capture

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_beam_access_lock`, tool dispatch, prefetch, `on_memory_write`) |
| Date | 2026-10-01 |
| Reason | Hermes dispatches `sync_turn` on a background worker without draining it before tools or prompt reads. Those paths reused the active Beam's SQLite connection outside its existing lock. A concurrent recall could expose uncommitted content, and a built-in mirror write could commit a failed turn's transaction. |
| Behaviour | Tool dispatch, built-in prefetch reads (including identity/model slots), and built-in memory mirroring share the turn-sync lock. Custom prefetch callbacks run outside the lock. The lock is reentrant so diagnostics can keep their existing internal critical section. Hermes still owns asynchronous turn dispatch and flushing. Engine-free regressions use a paused real SQLite transaction to verify isolation and rollback, and exercise the nested diagnostic lock. |
| Upstream | Not sent yet — shared-connection concurrency fix. |

Candidates that deliberately were **not** patched live in `CONTRACT_AUDIT.md`
(they need a product decision, not a mechanical fix).

## Sync procedure (run on every upstream release)

```bash
# 1. Get the new artifact, then diff vendored vs upstream
scripts/vendor-provider-sync.sh /path/to/site-packages/hermes_memory_provider

# 2. Triage every reported difference:
#    - upstream-only change -> re-vendor the file, update VENDORED_FROM.json
#    - local patch lost    -> re-apply it with the marker, keep the entry below
#    - new local need       -> add the marker + an entry here

# 3. Prove it
python tests/test_vendored_provider.py
python -m pytest tests -m 'not integration' -q
```

Widen the engine pin (`>=3.15.1,<3.16`) only after re-running the contract audit
in `CONTRACT_AUDIT.md` — the provider imports engine internals, so the range is
part of the contract, not a preference.

## Escalation trigger (vendor -> formal fork)

Promote this snapshot to a formal, separately-versioned fork if either holds:

- the local patch surface exceeds **~15% of vendored lines** (the vendored
  package is ~5.2k lines), or
- upstream diverges from or abandons the Hermes provider path (e.g. no working
  provider in a new release, or the Hermes integration is removed upstream).

Until then, vendoring keeps the fix/pin/hotfix ability that a bare dependency
does not, without the rename and dual-versioning cost of a fork.
