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
| Reason | `MNEMOSYNE_DATA_DIR` silently outranks `$HERMES_HOME`, splitting logs from the DB, and no line stated where memory actually lives. |
| Behaviour | One INFO line at init with resolved DB, provider package, engine version and `HERMES_HOME`; a WARNING when the DB sits outside `$HERMES_HOME`. Never refuses to start. (The Hermes-range warning was removed by P30.) |
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

### P14 — supported Hermes range reflects audited releases (P4-2) — superseded by P30

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

### P20 — `mnemosyne_update` resolves every row `mnemosyne_get` resolves

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_update`, `_update_get_visible_memory`, `_refresh_updated_embedding`) |
| Date | 2026-10-03 |
| Reason | The engine's `update_working` matches `working_memory WHERE id = ? AND session_id = ?`, while `get` (and `invalidate`) match `session_id = ? OR scope = 'global'` and fall back to `episodic_memory`. An ID that `mnemosyne_get` had just returned therefore came back `not_found` from `mnemosyne_update` whenever the row was a global memory written by another session (every new CLI session has a new `session_id`) or had been consolidated to episodic memory. Agents worked around it with remember + invalidate, leaving duplicates. |
| Behaviour | On an engine miss the handler retries with the engine's own visibility predicate over working then episodic memory, refreshes the row's embedding (best effort, as upstream) and clears the query cache. A row private to another session stays `not_found` for both tools. The success payload gains `memory_store`. An update with neither `content` nor `importance` returns an error instead of `not_found`. |
| Upstream | Not sent yet — engine bug (`BeamMemory.update_working` predicate); the real fix belongs in the engine, after which this patch can be dropped. |

2026-10-04 amendment: the fallback now obtains its predicate and bind parameters
from `_session_scope_filter` / `_session_scope_params`, using a single captured
runtime cross-session setting. The engine fix is carried in
[`../engine-patches/`](../engine-patches/README.md) and applied by `install.sh`,
so engine MCP benefits too. P20's episodic fallback and caller validation remain
necessary; a working-row engine fix does not retire all of this patch.

### P21 — `mnemosyne_validate` honours session/global visibility

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_validate` existence check and all four mutations) |
| Date | 2026-10-03 |
| Reason | The existence check used an unscoped `WHERE id = ?`, so any session could attest — and read the content of — private rows owned by another session, while `get`/`update`/`invalidate`/`forget` all returned `not_found` for the same IDs. Found by the first engine-backed run of the contract lane (Check C). |
| Behaviour | The check and every mutation use the engine's own visibility predicate (`session_id = ? OR scope = 'global'`), so validate agrees with the other ID tools on found / not found. Collaborative attestation of visible (own and global) rows is unchanged; shared-surface rows keep working through the surface beam (scope `global`). |
| Upstream | Not sent yet — upstream visibility decision; the real fix belongs in the engine, after which this patch can be dropped. |

2026-10-04 amendment: the existence check and all four mutations share a clause
and matching binds built by the engine scope helpers from one runtime snapshot.
Cross-session enabled permits foreign-session private rows consistently with
the patched engine ID tools; disabled preserves session/global visibility.

### P22 — consolidation yields foreground access during model work

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (initialization, auto-sleep, session-end, shutdown) |
| Date | 2026-10-08 |
| Reason | The independent auto-sleep worker held the foreground Beam lock for its entire `sleep()`. A join timeout freed turn sync but left later prefetch/tool calls blocked for the remaining consolidation duration. The untracked worker could also overlap later triggers or outlive host-backend cleanup. |
| Behaviour | One tracked worker captures session/database/author/channel and canonical owner/context, owns an independent connection, and calls the audited engine's lock-aware sleep path. Auto-sleep never joins on the turn-sync path; overlapping auto/session-end triggers reuse work without consuming another reflection call. Database access remains coordinated, while inference yields the foreground lock. Shutdown closes admission and drains either trigger; overrun cleanup only clears the backend registration this provider owns. Initialization keeps admission closed throughout retargeting and refuses active-worker reinitialization. Worker connections close even after failure. |
| Verification | `tests/test_provider_consolidation.py`: deterministic stalled-model regression fails against the previous snapshot; foreground sync/prefetch/recall proceed before inference is released. Real engine SQL tracing checks protected database operations alongside independent connections and write→read coherence. Lifecycle regressions cover duplicate triggers, state capture, shutdown, ownership and failure cleanup. |
| Upstream | Not sent yet. The inference/database split follows the transferable background-work separation described in [Honcho research](../../docs/HONCHO_RESEARCH.md); no Honcho code or service dependency is imported. Requires the audited engine diffs in `../engine-patches/`. |

### P23 — bounded, measurable automatic context

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (source classification, prompt instructions, prefetch, cache, diagnostics) |
| Date | 2026-10-08 |
| Reason | Raw tool/delegation rows could silently enter context as facts; total output had no aggregate ceiling; prompt advertised unavailable tools and incorrectly deprecated active native memory. |
| Behaviour | Tool/delegation sources are raw and excluded from automatic injection by default; explicit capture/manual recall remain available. The complete block has an 8,000-character default ceiling, prioritizes identity, and marks truncation/omission. Instructions reflect configured tools and active native memory. PII-safe timings/counts/failures are available in diagnostics. An opt-in bounded exact-query cache keys visibility, DB generation and custom-source revisions; queue warming uses Hermes' existing serialized callback lane. |
| Verification | `tests/test_provider_prefetch.py`, `tests/test_provider_eval.py`, and `bench/hermes_prefetch_eval.py` exercise public engine-backed output, cache coherence, output limits and synthetic relevance outcomes. |
| Upstream | Not sent yet; preserve these declared changes during sync. |

### P24 — native ownership and Hermes lifecycle compatibility

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (mirror ownership, setup schema, author provenance, backup/identity/status/compression hooks) |
| Date | 2026-10-08 |
| Reason | Native corrections retained stale mirrors; the setup wizard included advanced controls; eligible external stores were omitted from backup discovery; newer host lifecycle APIs were unsupported. |
| Behaviour | Durable native-entry ownership permits exact retirement with authoritative entry metadata, preserving unrelated memories. The setup schema has four essentials while advanced reads remain compatible. Current-speaker metadata is separate from visibility. Fresh-provider backup discovery and identity signatures read configuration without DB initialization. Checkpoint API v2 is advertised only on capable hosts and archives full normalized evidence durably/idempotently; legacy compression remains best effort. Successful context can provide newer recall-status badges. |
| Verification | `tests/test_provider_lifecycle.py` includes native ownership/restart, author capture, backup/WAL restore, capability gating and failure propagation through extracted pinned Hermes main methods. Fixtures identify the upstream source commit and hashes. This is distinct from a live gateway run. |
| Upstream | Not sent yet; published Hermes release support and engine pin remain unchanged. |

### P25 — directory-plugin dependency declaration

| | |
| --- | --- |
| File | `hermes_memory_provider/pyproject.toml` (new local metadata, no upstream wheel original) |
| Date | 2026-10-08 |
| Reason | Newer Hermes PM owns managed environments and requires directory plugins to declare dependencies for admission. |
| Behaviour | Declare the unchanged audited engine pin inside the deployed plugin. Bash and native PowerShell installers deploy/verify it before invoking `hermes pm install`, then verify the selected generation and engine patch hashes. Legacy installation remains supported. The metadata is included in provider wheels and in `VENDORED_FROM.json`'s metadata hashes. |
| Verification | Installer fixture suites, pin-drift gate, and `tests/test_vendored_provider.py`. |
| Upstream | Local deployment metadata; preserve during provider sync. |

Candidates that deliberately were **not** patched live in `CONTRACT_AUDIT.md`
(they need a product decision, not a mechanical fix).

### P26 — thread-owned audit persistence and partial-history health

| | |
| --- | --- |
| Files | `hermes_memory_provider/audit.py`, `hermes_memory_provider/__init__.py` |
| Date | 2026-10-09 |
| Reason | One initialization-thread SQLite connection dropped worker events; read errors looked like empty history. Updates had no recorded event and deletion events lacked durable target ownership. |
| Behaviour | Short-lived per-operation connections, a 100 ms busy wait, classified-store checks before writes, lifecycle serialization and sanitized failure counters. Read-only history raises on failed reads. Session/global selection excludes unknown ownership, while explicit operator selection permits broader inspection. Successful remember/update/forget/invalidate/private validation events retain target scope/session; history remains partial and best effort. Diagnostics include audit health without another default tool. |
| Verification | `tests/test_provider_audit.py`, `tests/test_provider_inspection.py`, and the real-audit lineage case in `tests/test_provider_engine_contract.py`: threads, locked stores, shutdown, replaced/foreign-store refusal, scoped/deleted events, valid bounded metadata, and read errors. |
| Upstream | Local provider integration; not sent upstream. Preserve during re-vendor. |

### P27 — bounded read-only provider inspection CLI

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` |
| Date | 2026-10-09 |
| Reason | Existing CLI inspection was keyword search only, without source-reference or recorded-history views. |
| Behaviour | `inspect --id` and `history --id` read existing stores without engine initialization or DDL. Session/global selection is the default; `--all-sessions` is explicit local-operator selection. Content/history are bounded and metadata is allowlisted. History supports JSON/text and labels partial coverage. Existing positional search is preserved. CLI also exposes the P28 Markdown projection while leaving engine JSON export unchanged. |
| Verification | `tests/test_provider_inspection.py` and `tests/test_provider_evidence.py`: scope/lineage exclusions, failures, content limits and read-only DB behavior. |
| Upstream | Local CLI extension; not sent upstream. Preserve during re-vendor. |

### P28 — exact references and Markdown evidence projection

| | |
| --- | --- |
| Files | `hermes_memory_provider/evidence.py` (new local module), `hermes_memory_provider/__init__.py`, `hermes_memory_provider/cli.py` |
| Date | 2026-10-09 |
| Reason | Source/author fields did not consistently carry exact host message IDs; a readable evidence view needed to preserve engine ownership and visibility. |
| Behaviour | Turn/identity capture adds references only for uniquely matching host messages with explicit IDs; explicit tool references remain caller assertions. Inspection marks stored origins unauthenticated; Markdown renders origin labels as stored claims. Shared read-only classification/snapshots show eligible engine rows and original lineage. Optional Markdown export escapes content, marks missing evidence and truncation, caps rows/content/index/lineage, and publishes a new file without touching the DB or overwriting an existing target. No transcript verification, Git writes, schema migration, pin widening or second memory backend. |
| Verification | `tests/test_provider_evidence.py`, provider inspection tests and engine-contract lineage checks cover exact/ambiguous/missing IDs, explicit origin, WAL snapshots, legacy engine tables, store refusal, source limits and export/DB compatibility. |
| Upstream | New local helper rather than copied memoryrepo code; design informed by `docs/MEMORYREPO_PROPOSED_PLAN.md`. Preserve module during re-vendor. |

### P29 — observable consolidation lifecycle

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-10-09 |
| Reason | The tracked automatic/session-end worker had no bounded status snapshot for operators. |
| Behaviour | Existing diagnosis includes in-memory worker state, trigger, start/finish times, monotonic duration, sanitized error class, reused/skipped trigger counts and shutdown-timeout state. Immutable snapshot replacement permits status reads without acquiring worker or foreground locks. Existing worker, budgets, tool exposure and engine ownership are retained; no durable job ledger or inferred cost fields. |
| Verification | `tests/test_provider_maintenance.py` and existing consolidation tests cover blocked model work, success/failure, overlap, worker construction/close/start failures, shutdown and reinitialization. Sleep/close `SystemExit` propagates while finalizing failed status. |
| Upstream | Local provider diagnostics; preserve during re-vendor. |

### P30 — no Hermes version requirement

| | |
| --- | --- |
| Files | `hermes_memory_provider/cli.py`, `hermes_memory_provider/__init__.py` |
| Date | 2026-10-10 |
| Reason | The provider enforced a `>=0.18,<0.20` Hermes range. The owner no longer wants a Hermes version requirement; a newer Hermes must not produce a warning or a failed check. |
| Behaviour | `SUPPORTED_HERMES_RANGE`, `TESTED_HERMES_VERSIONS` and `check_hermes_version` are removed, and `initialize` no longer logs a range check. `detect_hermes_version` stays; `doctor` prints the detected version for diagnosis only and the version never changes its exit code. |
| Verification | `tests/test_provider_db_path.py` (doctor/version surface), `tests/test_vendored_provider.py` (hashes and declarations). The onboarding smoke installs the newest Hermes. |
| Upstream | Not sent yet; repo-specific. Preserve during re-vendor. |

### P31 — selected-profile config, store resolution and read-only doctor

| | |
| --- | --- |
| Files | `hermes_memory_provider/configuration.py` (new local helper), `hermes_memory_provider/__init__.py`, `hermes_memory_provider/cli.py` |
| Date | 2026-10-10 |
| Reason | Engine-global config fallback could bleed settings or seed files. Provider banks, CLI, identity, backups and doctor could resolve different stores; reinitialization retained prior settings and ordinary SQLite opens could recreate a disappeared store. |
| Behaviour | Read selected-home YAML without writes, reject malformed/unreadable config, reset profile settings before each initialization, and share path resolution across surfaces. Explicit paths beat isolation; refuse lossy bank names and conflicting identity. Open the exact store through BeamMemory with config seeding disabled. Identity still tracks config changes for cache invalidation; backup includes the store actually open. Doctor uses Hermes' active home and read-only SQLite; `--no-fix` avoids constructors and diagnostic logs. Supplemental doctor checks cover runtime dependencies. Provider diagnostic repair gets the explicit active path and profile log directory. |
| Verification | `tests/test_provider_profiles.py` drives public initialization, config refusal, bank ambiguity, identity/backup consistency, reinitialization, active-home doctor and a disappearing-file race. Fresh subprocesses use the real audited engine constructors with two profiles, with and without `MNEMOSYNE_DATA_DIR`, and verify write isolation plus no default DB or config creation. Existing engine-contract, lifecycle and consolidation tests remain required. This is isolated runtime evidence, not live-gateway deployment verification. |
| Upstream | Not sent. Requires the P31 amendments to the audited engine patches in `../engine-patches/`: import-time default DB initialization removed, optional config seeding on constructors, and explicit diagnostic paths. Engine pin and P30 remain unchanged. |

### P32 — preserve source-reference identifier namespaces

| | |
| --- | --- |
| File | `hermes_memory_provider/evidence.py` |
| Date | 2026-10-10 |
| Reason | Hermes internal row IDs and platform message IDs can legitimately differ or collide; comparing them as one namespace dropped valid references. |
| Behaviour | Prefer explicit platform IDs, otherwise internal IDs, and record `message_id_namespace`. Normalize nonnegative integer host IDs. Check uniqueness within the selected namespace and session; refuse invalid or duplicated selected IDs. Preserve old references without inventing a namespace, and label the namespace or its absence in Markdown. All origins remain unauthenticated stored claims. |
| Verification | `tests/test_provider_profiles.py` and `tests/test_provider_evidence.py` cover cross-namespace collisions, same-namespace duplicates, integer IDs and legacy references. |
| Upstream | Local evidence helper; preserve during re-vendor. |

### P34 — loader resolution without PyYAML (Hermes 0.22+)

| | |
|---|---|
| File | `hermes_memory_provider/configuration.py` (`_mapping`, new `_yaml_safe_load`) |
| Date | 2026-10-15 |
| Reason | Hermes 0.22 dropped PyYAML from its venv (ruamel-only), so the hard `import yaml` raised `ModuleNotFoundError` and every profile-config read failed with `ProfileConfigError` — the provider could not resolve its DB path at all. |
| Behaviour | `_yaml_safe_load` resolves a loader this install actually ships: PyYAML when present (Hermes <= 0.21), then the internal `hermes_yaml.safe_load` (0.22+), then bare ruamel with YAML 1.1 semantics as a last resort. Same parse result on every Hermes release; no version pin. |
| Verification | `hermes mnemosyne doctor --no-fix` against a 0.22.0 image (PyYAML-free venv) resolves the DB and passes the config checks; the vendored-snapshot gate covers the hash update. |
| Upstream | Not sent; generally useful (affects every Hermes >= 0.22 install of this provider). |

### P33 - recall cutoff in prefetch cache identity

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-10-13 |
| Reason | Enabling an engine score cutoff must not reuse a previously cached broad result. |
| Behaviour | Include `MNEMOSYNE_RECALL_SCORE_FLOOR` in the exact-query prefetch cache key. Changing the cutoff refreshes context even when the query and database are unchanged. |
| Verification | `tests/test_provider_prefetch.py::test_prefetch_cache_observes_recall_score_floor_changes` fails with stale context before this patch and passes after it. |
| Upstream | Preserve while the audited engine recall cutoff is local. |

### P35 — consolidation survives a summariser TypeError

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (new `_exc_location`, new `_sleep_with_local_only`, the `sleep_beam.sleep()` call in the consolidation worker) |
| Date | 2026-10-26 |
| Reason | Session-end consolidation runs in a worker. Any `TypeError` raised by the summariser path escaped `beam.sleep()`, so the whole batch stayed unconsolidated until the next maintenance pass and the log showed only `TypeError: <redacted>`, hiding the raising frame. |
| Behaviour | The worker catches the `TypeError`, logs the raising `file:line` through `_exc_location()`, and retries the same batch once with the host LLM switched off (`_sleep_with_local_only()`), so the engine takes its local AAAK path and still consolidates. Rows already marked are skipped on the retry, so a retry cannot double-consolidate. The engine-side companion change is `_safe_summarise()` in `integrations/engine-patches/core-beam.py.patch`. |
| Verification | `tests/test_provider_consolidation.py` (retry path, log content, no double consolidation); injecting a `TypeError` into `summarize_memories` aborted `sleep()` before the patch and consolidates via AAAK after it. |
| Upstream | Not sent; the engine-side half is carried as an audited engine patch, so keep both halves together. |

### P59 — align prefetch overfetch with the default output budget

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-11-27 |
| Reason | Prefetch requested 16 candidates for a default five-row output. On frozen real conversations, the larger window admitted weaker neighbors and lowered automatic-context precision. |
| Behaviour | The overfetch floor is ten candidates, matching `profile.top_k * 2` for the default profile. Larger configured output budgets still scale normally. Filtering, deduplication, source policy, character limits and response contracts are unchanged. |
| Verification | `tests/test_provider_prefetch.py::test_prefetch_uses_ten_candidate_overfetch` pins the engine request. A predefined real-conversation dev sweep measured prefetch recall 0.26 -> 0.28, entry precision 0.112 -> 0.139, mean context 1151.7 -> 959.16 characters, with a small p50 improvement. Holdout confirmation is required before release. |
| Upstream | Not sent. |
| Numbering | P54 remains reserved; P59 follows P58. |

### P61 — lexically connected recent session context

| | |
|---|---|
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-11-27 |
| Reason | Semantic recall can miss a recent same-session user message that shares meaningful query vocabulary, weakening conversation continuity. |
| Behaviour | Prefetch may reserve one of its existing output slots for the most recent visible, unexpired, non-superseded `scope='session'` user row when it shares at least one meaningful token with the query. Global rows are excluded, exact duplicates are not re-added, and `MNEMOSYNE_PREFETCH_RECENT_USER=0` disables the source. Source text, output caps and character budgets are unchanged. |
| Verification | `tests/test_provider_prefetch.py::test_prefetch_adds_lexically_connected_recent_session_user` covers admission, no-overlap abstention, global-row exclusion and the disable switch. `test_prefetch_cache_observes_recent_user_changes` pins cache invalidation. Session-scoped and global real-conversation workloads check relevance and non-regression; holdout confirmation is required. |
| Upstream | Not sent. |
| Numbering | P54 remains reserved; P61 follows P60 (P60 was discarded). |

## Sync procedure (run on every upstream release)

**Triage decision (2026-11-10): keep `mnemosyne-memory 4.0.0` outside the
supported range for now.** `scripts/upstream-drift-check.py` reports `vendored
3.15.1, latest 4.0.0`. The 4.0.0 provider tree has substantial module and API
drift, and the newest-engine contract fails on `core/beam.py` before provider
contract tests. Keep the pin at `>=3.15.1,<3.16`; do not widen it or re-vendor
4.0.0 as part of unrelated work. Issue [#42](https://github.com/juanmackie/mnemosyne-hermes/issues/42)
records the decision. Revisit through a separate compatibility port that maps
the changed and removed modules, preserves local patch behavior, and passes
`CONTRACT_AUDIT.md` plus `./test-all.sh --require-engine` against 4.0.0. Keep
issue #42 open until that evidence is green.

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

**The first trigger is already met.** Measured 2026-11-09 against the
`mnemosyne-memory 3.15.1` sdist (`pip download --no-binary :all:`, so this is
the real upstream tree, not a copy of ours):

| | Lines |
| --- | --- |
| Upstream `hermes_memory_provider` | 5,217 |
| This snapshot | 9,326 |
| Net added | 4,109 (**78.8%**) |
| Of which new local-only modules | 1,208 (`configuration.py`, `evidence.py`, `review_ledger.py`, `source_check.py`) |

The threshold was 783 lines. Four of the seven upstream files are untouched
(`hermes_llm_adapter.py`, `persona_adapter.py`, `persona_tools.py`,
`sync_adapter.py`); the growth is concentrated in `__init__.py` (+1,874),
`cli.py` (+775) and `audit.py` (+252).

The consequence is concrete and already visible: the hash ledger mostly records
our own edits, a patch-regeneration commit silently dropped a hunk and left
`main` red for two weeks (see `../engine-patches/README.md`), and none of the
43 patches has been offered upstream.

### Decision (2026-11-10): promote to a formal fork

The measured threshold is met by a wide margin and the patch-regeneration
failure demonstrates the maintenance cost of carrying this much local code as
a nominal upstream snapshot. We will promote the provider to a separately
versioned fork. This records the direction; it does not change the provider id,
engine pin, installer, or shipped source in this commit. Until the fork is
extracted and its migration is reviewed, keep the current digest gate,
`mnemosyne-memory[embeddings]>=3.15.1,<3.16` pin, and strict engine-contract
lane. The extraction work must preserve the documented Hermes contracts and
must not put a competing `mnemosyne` distribution or import package ahead of
the engine. General fixes that still apply cleanly upstream can be offered
separately; the fork decision does not replace that review.

### P36 — atomically claim pending approval writes

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_apply_pending`) |
| Date | 2026-11-07 |
| Reason | Concurrent apply calls could both read a pending JSON record and commit duplicate memories before either removed it. |
| Behaviour | An exclusive `.applying` claim is created before reading/applying. A concurrent call fails closed. The claim is retained on ambiguous errors or crashes so a possibly committed write is not silently replayed; operators must inspect and recover that record. |
| Verification | `tests/test_provider_pending.py::test_concurrent_pending_apply_only_applies_once` gates a blocked first write and concurrent second apply; the fake BEAM must receive exactly one call. |
| Upstream | Not submitted separately; equivalent atomic claim helpers are now present in both the [repo-path provider](https://github.com/mnemosyne-oss/mnemosyne/blob/main/hermes_memory_provider/__init__.py) and [standalone provider](https://github.com/mnemosyne-oss/mnemosyne/blob/main/integrations/hermes/src/mnemosyne_hermes/__init__.py). |

### P37 — isolate concurrent prefetch cache payloads

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`prefetch`, `_prefetch_bank`, `_ensure_prefetch_state`) |
| Date | 2026-11-07 |
| Reason | Concurrent prefetch calls shared the last recall IDs and bank counts. One call could cache another query's recall IDs, so a later cache hit updated recall statistics for the wrong memories. |
| Behaviour | Keep recall IDs and bank counts in thread-local per-call state and snapshot that state into the query's cache entry. Aggregate diagnostic fields remain best effort. |
| Verification | `tests/test_provider_pending.py::test_prefetch_cache_payload_keeps_recall_ids_per_call` forces query A to pause after recall while query B overwrites the old shared fields; each cache entry must retain its own IDs. |
| Upstream | Not ported: the current upstream [repo-path provider](https://github.com/mnemosyne-oss/mnemosyne/blob/main/hermes_memory_provider/__init__.py) serializes the complete prefetch path under its Beam-access lock and has no analogous per-query prefetch cache. This patch targets the pinned snapshot's retired cache architecture. |

### P38 — make turn capture atomic

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`sync_turn`, `_atomic_sync_turn_writes`) |
| Date | 2026-11-07 |
| Reason | A later role write could fail after an earlier `remember()` had committed, leaving a partial user/assistant/tool turn. |
| Behaviour | Under the existing per-provider BEAM lock, use the pinned engine's `_deferred_commits` transaction to defer all `remember()` and enrichment commits until every selected role and identity write succeeds. Roll back the transaction on failure; refuse to write if the connection is not an idle deferred-commit BEAM connection. Retries then start from a clean turn, and concurrent calls serialize before opening their transaction. |
| Verification | `tests/test_provider_pending.py::test_sync_turn_failure_rolls_back_prior_role_write_and_retry_completes` and `::test_concurrent_sync_turns_commit_as_whole_turns`; real-engine regressions in `tests/test_provider_engine_contract.py` verify rollback, retry, and concurrent serialization. |
| Upstream | Candidate under [E09 in issue #828](https://github.com/mnemosyne-oss/mnemosyne/issues/828); not submitted pending maintainer guidance. The current upstream engine has `_deferred_commits`, but its provider also records in-memory verbatim-ledger proofs during each capture. A port must roll those back along with database writes; the pinned 3.15.1 helper cannot be copied unchanged. |

### P39 — doctor flags an engine without the audited patches

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` (`_engine_accepts_seed_config`, `doctor`) |
| Date | 2026-11-08 |
| Reason | Published `mnemosyne-memory` 3.15.1 has no `seed_config` constructor argument (see P31). After an engine reinstall without the audited patches the provider fails at runtime with `TypeError: ... unexpected keyword argument 'seed_config'` and nothing points at the cause (issue #37). |
| Behaviour | `doctor` adds a critical check that `BeamMemory.__init__` and `Mnemosyne.__init__` accept `seed_config`. A failure names the classes and the fix (`scripts/apply_engine_patches.py`). No runtime path changes; the argument stays required, with no fallback. |
| Verification | `tests/test_provider_db_path.py::test_engine_accepts_seed_config_check` runs the check against fake engine modules with and without the parameter. |
| Upstream | Not sent. Retire with the engine patches once an audited release ships `seed_config`. |

### P40 — source-grounded lessons and bounded manual memory review

| | |
| --- | --- |
| Files | `hermes_memory_provider/__init__.py`, `cli.py`, `evidence.py` |
| Date | 2026-11-08 |
| Reason | Apply the components-of-agent-memory findings using existing capture, evidence, consolidation and injection rather than introducing another store or background model loop. |
| Behaviour | Prompt guidance leaves salience to the agent, distinguishes hypotheses from tested lessons, records supporting evidence and keeps injected anchors small. `hermes mnemosyne review` reads at most 200 visible working/episodic rows and emits at most 100 mechanical review candidates with explicit coverage/truncation. References remain unauthenticated lookup aids; no factual verification, automatic merging, deletion, model call, engine initialization, new registration or database writes. |
| Verification | `tests/test_provider_inspection.py` covers visibility, byte-identical reads/refusals, limits, missing originals, duplicates and truncated-prefix exclusion; `tests/test_provider_prefetch.py` checks guidance. Both suites are registered in `scripts/checks.sh`. |
| Upstream | Not sent. |

### P41 — one-memory source checking with cited correction proposals

| | |
| --- | --- |
| Files | `hermes_memory_provider/cli.py`, `source_check.py` |
| Date | 2026-11-08 |
| Reason | The mechanical review queue cannot detect a plausible false note. Add a small, manually invoked semantic experiment before considering recurring reflection. |
| Behaviour | `review --id ID --evidence FILE` checks one visible complete memory against an explicitly supplied bounded transcript excerpt. Reuses `HermesAuxLLMBackend.complete` once, without engine construction, backend registry changes, extra credentials, transcript discovery or writes. Strict JSON verdicts distinguish supported/contradicted/insufficient; only contradictions may propose a replacement. Citations must name supplied messages and contain exact quotes. Existing references must match session, message and explicit namespace; legacy namespaces fail closed. Missing references are labelled operator-selected. Neither source authentication nor semantic entailment is claimed. Original/evidence SHA-256 hashes identify proposal inputs. |
| Verification | `tests/test_provider_source_check.py` (registered in `scripts/checks.sh`) covers citation/schema rejection, uncertainty/preservation, source bounds and namespace association, read-only CLI visibility, and scorer regressions. `bench/source_check_eval.py --run-model` evaluates eight authored synthetic cases via the existing Hermes model; deterministic unit-test judges validate plumbing only. |
| Upstream | Not sent. No automatic apply or scheduler is introduced. |

### P43 — provenance-backed source references for review

| | |
| --- | --- |
| Files | `hermes_memory_provider/evidence.py`, `source_check.py`, `cli.py` |
| Date | 2026-11-09 |
| Reason | Existing engine provenance rows link legacy memories to recorded sessions and turns even when `metadata_json` has no source reference. |
| Behaviour | Read-only snapshots use provenance as an unauthenticated source association when metadata lacks a reference. Review queues default to candidates with a reference or visible lineage; `--all-candidates` exposes the complete gap. Inspection output preserves provenance references, and source checking enforces transcript session, message and namespace matching. |
| Verification | `tests/test_provider_evidence.py`, `tests/test_provider_inspection.py` and `tests/test_provider_source_check.py` cover provenance-backed snapshots, inspection and citation association. |
| Upstream | Not sent. |
### P42 — measured explicit-correction loop

| | |
| --- | --- |
| Files | `hermes_memory_provider/cli.py`, `hermes_memory_provider/review_ledger.py` (new local module) |
| Date | 2026-11-09 |
| Reason | The review workflow produced a cited proposal but could not distinguish a user's explicit correction from a speculative check, and had no local way to measure whether its proposals were accepted or later proved wrong. |
| Behaviour | `review --id ID --evidence FILE --correction` reuses the P41 proposal-only source checker and requires the excerpt to contain a `user` message; `--correction-message-id` optionally pins which user message carried the correction. It appends one content-free event to `<db>.review-ledger.jsonl` beside the resolved store: review/trigger/verdict IDs, memory ID/store, session/visibility selector, original/evidence/replacement SHA-256, correction message ID and timestamp, with no memory text, transcript text, citation quotes, reason or replacement. A non-contradicted verdict is recorded too, without a proposal. `review-outcome --proposal ID --outcome accepted\|rejected\|corrected_again` reads the current memory with the recorded selector, reports `observed` (`matches_original`, `matches_proposal`, `changed_otherwise`, `missing`) plus unverified-outcome warnings, and stores that observation and source. `review-pending` lists proposals with no outcome. If a new explicit correction's original hash equals a prior proposal's exact replacement hash and proposes a different replacement, the prior proposal is marked `corrected_again` with source `observed_rerun`; this is the only inferred outcome. `review-stats` folds the ledger into counts, `accepted_verified`/`accepted_unverified`, `acceptance_rate`, `verified_acceptance_rate`, `false_correction_rate`, outcome sources and observed states. Rates are `null` until a proposal has a recorded outcome. The ledger is a local diagnostic sidecar, not a second memory authority; writes refuse symlinks and non-regular files, and malformed lines fail loudly. |
| Verification | `tests/test_provider_review_ledger.py` (registered in `scripts/checks.sh`) covers content exclusion, deterministic IDs, duplicate dedupe, successive outcomes, supported/insufficient reviews, malformed/truncated lines, unknown proposals, symlink refusal, CLI proposal→outcome→stats flow, read-only plain review, observed acceptance verification, pending lists, and exact-replacement supersession without duplicate rerun effects. `bench/source_check_eval.py` now reports `correction_pass_rate` and `false_proposal_rate` over its authored cases. |
| Upstream | Not sent. Deliberately no automatic apply, background scan, transcript fetch, schema change, tool registration or code self-modification. |

### P44 — doctor audits the resolved engine; the unavailable banner names the fix

| | |
| --- | --- |
| Files | `hermes_memory_provider/cli.py`, `hermes_memory_provider/__init__.py` |
| Date | 2026-11-10 |
| Reason | A desktop profile reported `BeamMemory.__init__() got an unexpected keyword argument 'seed_config'` at session start while the hourly cron path (a separate venv) ran the patched engine normally. The interpreter loading the plugin was never verified to resolve the same patched engine the installer put there. `install.sh` audits the patch digests when it applies them, but nothing re-checked them afterwards: an engine that lost every patch hunk except `seed_config` (the state a manual `patch(1)` attempt with `.rej` rejects leaves behind) passes the P39 signature check, and an unpatched importable copy passes install verification outright. The banner sent operators to the log without the command that explains and fixes the failure. |
| Behaviour | `doctor` prints the resolved engine file next to the version on the "engine importable" line, and adds a critical check "engine matches audited patches": the files the running interpreter resolves (`beam.py`, `memory.py`, `entities.py`, `llm_conflict_detector.py`, `diagnose.py`, `mcp_tools.py`) are hashed and compared against `integrations/engine-patches/manifest.json` `patched_sha256` digests. A mismatch fails doctor with the differing file names and the repair (`./install.sh --python <this interpreter>`). When no manifest sits beside the provider (a copied plugin install or a wheel without the repo) or the engine package has no filesystem `__file__`, the audit is skipped and printed as a `[note]` instead of gating. The audit reads bytes only; `doctor --no-fix` remains constructor-free and write-free (P31). The unavailable-session banner now names `hermes mnemosyne doctor --no-fix` as the diagnosis step. Install-time verification (`install.sh`, `install.ps1`) now also fails when the resolved engine imports but `BeamMemory`/`Mnemosyne` reject `seed_config`, instead of reporting a successful install against a shadowing unpatched copy. |
| Verification | `tests/test_provider_db_path.py::test_engine_patch_audit_*` run the audit helper against synthetic manifests and engine trees (all-patched, one file at the original digest, missing file, missing manifest, missing `__file__`). `install.sh` and `install.ps1` engine-selection gates were exercised by review; end-to-end coverage rides on the existing installer harness. No engine source is changed. |
| Upstream | Not sent. Retire the audit with the engine patches once an audited upstream release supplies `seed_config` and the audited behaviour. |

### P45 — absolute self-imports bind to the deployed package

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-11-10 |
| Reason | The `mnemosyne-memory` wheel ships a top-level `hermes_memory_provider` package into site-packages (unpatched upstream: `__init__.py`, `cli.py`, `audit.py`; local-only modules such as `evidence.py`, `review_ledger.py`, `source_check.py` and `configuration.py` do not exist there). Lazy absolute imports inside the deployed provider (`from hermes_memory_provider.evidence import ...`, used by the review, source-check, audit, prefetch and DB-path paths) resolved against that upstream copy in copied installs (`./install.sh --copy`, Windows without Developer Mode): the deployed process silently ran unpatched provider code, and the local-only modules raised `ImportError` wherever they were not wrapped. Symlinked installs were unaffected (the T7 path insertion makes the deployed package importable under its canonical name). Found while investigating the desktop unavailable-engine report (the engine wheel bundles a same-named, same-claim provider — the exact "which copy won?" ambiguity from that report, one level up). |
| Behaviour | At module import, the deployed package aliases itself into `sys.modules["hermes_memory_provider"]` when it is a proper package (`__path__` set), via `setdefault`. Symlinked installs behave as before (same files, now one instance instead of two). Copied installs now bind every absolute self-import to the deployed package. No import is made earlier or later than before; no name is removed from `sys.modules`. |
| Verification | `tests/test_provider_loader.py::test_copied_install_absolute_imports_bind_to_deployed_package` builds a copied install beside a synthetic unpatched `hermes_memory_provider` on `sys.path` and asserts the evidence module and the registered provider resolve to the deployed copy. Manual check: the banner/doctor path is unaffected when no shadow exists. |
| Upstream | Not sent. Upstream's own deployment mode is the bundled package; this alias only affects processes that load the deployed snapshot, which is this repository's install path. |

### P46 — identify the engine in gateway initialization failures

Initialization failures include the running Python executable and the resolved
`mnemosyne` package file or namespace paths, plus the doctor command. This makes
interpreter drift visible in the existing unavailable response and gateway log.
Availability and session initialization check both engine constructor signatures
and reject namespace packages before opening the store. Doctor reports the same
interpreter and package details on import failure. Doctor and the installers
require the imported package to match the installed distribution, even if the
doctor hash audit is unavailable. Regression coverage is in
`tests/test_provider_db_path.py` and `tests/test_install.py`.

### P47 — doctor's fix path re-applies the audited engine patches

| | |
| --- | --- |
| File | `hermes_memory_provider/cli.py` (`_repair_engine_patches`, `doctor`) |
| Date | 2026-11-11 |
| Reason | Issue #55: a Hermes PM dependency-generation rebuild installs a fresh upstream `mnemosyne-memory` wheel, so the audited patches vanish from the selected generation and `hermes mnemosyne doctor --no-fix` fails the `seed_config` and digest checks until the applier is re-run by hand. The repo cannot hook Hermes PM, and re-running the whole installer for this is heavyweight. Doctor runs under the selected generation's interpreter, so its fix path is the repair that targets the active generation — but its gate raised `SystemExit` before any fix could run, and its failure messages named only the installer or the applier script. |
| Behaviour | In fix mode (no `--no-fix`), when the `engine patches applied` or `engine matches audited patches` critical checks fail and the imported engine matches the installed distribution (P46 guard), doctor loads the audited applier (`scripts/apply_engine_patches.py`, located beside the manifest that `_engine_patch_audit` verifies against) and re-applies the audited patch set in-process. The applier refuses any source that is not the audited upstream revision, keeps verified originals, writes atomically, and is idempotent — doctor can never invent or force a patch, and a shadowing engine copy is refused. After a file-changing repair the failed checks are re-evaluated against the on-disk digests (the modules this process already imported stay pre-repair code), the report shows them as PASS with a restart note, and doctor skips the engine runtime diagnostics (they would describe stale in-process behavior) with an explicit restart instruction. `--dry-run` prints the applier's plan under `[note]` without writing. `--no-fix` — and therefore `scripts/update.sh` — never attempts a repair. Copied provider installs (no manifest beside them) keep pointing at `./install.sh`. |
| Verification | `tests/test_doctor_patch_repair.py` starts from a fresh stub engine generation (no `seed_config`, digests off the audited set) against the real provider, real applier and a fixture manifest: `--no-fix` fails and writes nothing, fix mode repairs and doctor exits 0, a fresh `--no-fix` passes, a repeated PM rebuild is repaired again, an unknown source revision is refused byte-identically, and `--dry-run` plans without writing. In-process wiring tests assert the repair only runs in fix mode when a patch check failed. The installer lane (`tests/test_install.py::test_pm_rebuild_reapplies_audited_patches_to_selected_generation`) re-applies the patches to a PM-selected generation after a simulated rebuild. |
| Upstream | Not sent. The applier and manifest are this repository's local deployment machinery; upstream has no engine-patches tree to repair. |

### P48 — exact structured labels narrow fully labeled recall results

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` |
| Date | 2026-11-15 |
| Reason | The synthetic PrecisionMemBench replay exposed canonical names and aliases as separate seed fields, but the provider's recall candidates did not return their stored metadata. The tested rerankers missed the active-pass floor. |
| Behaviour | After private engine recall, the provider reads metadata for each returned ID. It narrows private candidates only when every row has a valid `canonical_name` or `aliases` label and at least one label appears as an exact token phrase in the query. Missing, malformed, invalid, mixed or non-matching labels preserve engine results. Label sizes and counts are bounded. Shared-surface results are merged later and are not filtered by P48. |
| Verification | `tests/test_provider_db_path.py` covers exact label narrowing, missing and mixed metadata, unmatchable labels, and deeply nested invalid JSON. `bench/precisionmembench/run_eval.py` maps structured names and aliases into metadata and marks reports `structuredLabelMetadata: true`; the synthetic PMB replay measured 39/43 active passes with 1 missing belief. The full 12,559-probe synthetic scorecard seeds no structured labels, so it verifies fallback compatibility and the three recorded opt-in engine flags, not P48's narrowing path. It measured 1.000 tool answerable recall, 0/2,851 fabricated returns, 0 expired facts, 0/628 planted secrets stored, 0 probe errors and 0.982 prefetch answerable recall. No live store was read. |
| Upstream | Not sent. |

### P49 — bounded fuzzy structured-label evidence

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_recall`, `_query_contains_label`, `_within_one_edit`) |
| Date | 2026-11-16 |
| Reason | P48 accepted only exact label token phrases. A misspelled query token ("typscript", "mongod", "eror") therefore left the engine's fuzzy lexical candidates unfiltered, and semantically adjacent but unrelated beliefs entered the context. |
| Behaviour | Under the same "all private rows fully labeled" guard, a query window whose token sequence differs from a label by at most one token, and that token by at most one Damerau edit (insert, delete, substitute, transpose), counts as label evidence. Exact phrases still match first. Unlabeled, malformed, mixed, or non-matching rows keep engine order; shared-surface rows are still merged after this step. Token length/count bounds are unchanged. |
| Verification | `tests/test_provider_label_recall.py` covers the one-edit matcher and the all-labeled guard; `tests/test_provider_db_path.py` retains the P48 metadata gating coverage. `research/autoresearch-results.tsv` records the autoresearch iterations on the pinned public suite. The full synthetic 12,559-probe corpus seeds no structured labels, so P49 is inert there by construction. |
| Upstream | Not sent. |

### P50 — zero-result fuzzy query expansion from structured labels

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`__init__`, `_handle_remember`, `_handle_recall`, `_index_search_labels`, `_expand_query_with_labels`) |
| Date | 2026-11-16 |
| Reason | A two-edit transposition ("typsecript") produced zero engine candidates, so no result-side filter could recover the expected memory. The engine's lexical path does not do Damerau transpositions. |
| Behaviour | On remember, single-token structured labels from metadata are collected into a bounded (<=2048 entry) per-provider index. On recall that returns no rows, the query is retried once with any token within one Damerau edit of an indexed label replaced by that label. The original query still drives P48/P49 evidence checks. Stores without structured labels keep an empty index and never retry. |
| Verification | `tests/test_provider_label_recall.py` covers the bounded index, the one-shot retry, the no-retry path and unlabeled stores; `research/autoresearch-results.tsv` records the autoresearch iteration on the pinned public suite. The synthetic corpus seeds no structured labels, so this path is inert there. |
| Amendment (2026-11-16) | The shared one-edit check used by P49/P50 also requires the leading two characters to agree (a swap of the first two is still allowed). This preserves the benchmark's prefix guard: "mango" is one edit from "mongo" but must not expand to it. |
| Upstream | Not sent. |

### P51 — abstain when no labeled row matches the query

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_recall`) |
| Date | 2026-11-17 |
| Reason | The pinned public PrecisionMemBench replay measured five over-return failures, all of them fully labeled rows with zero label evidence for the query (e.g. "what's our stack?" -> b-react-entity). P48 kept engine order in the no-match case, so the semantic guess was served with no structured support. |
| Behaviour | Under the same "all private rows fully labeled" guard, when no row's label is an exact or one-edit match for a query window, recall returns zero rows instead of the engine's guess. Rows with any label evidence still narrow as in P48/P49. Unlabeled, malformed or mixed rows keep engine results. Relation expansions resolve in the adapter after recall and are unaffected; shared-surface rows still merge after this step. |
| Verification | `tests/test_provider_label_recall.py::test_abstains_when_no_labeled_row_matches_the_query` and `test_abstains_only_when_every_row_is_labeled` pin the path engine-free. PrecisionMemBench replay: 43/43 active passes, 0 over-returns, 0 missing beliefs (score 4300). The 800-probe held-out synthetic sweep stayed green (tool answerable recall 1.0, 0 fabricated rows from 183 fabricated probes, 0 expired served, 0 errors) — the path is inert on unlabeled stores. |
| Upstream | Not sent. |

### P52 — one-edit fuzzy evidence needs two characters on the shorter side

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_within_one_edit`) |
| Date | 2026-11-17 |
| Reason | A one-edit delete between a one-character query token and a two-character label token ("s" vs "TS") counted as evidence under P49. That match cannot be a typo; it only attaches label narrowing to noise. |
| Behaviour | The shared one-edit check returns false when the shorter side is a single character, after the exact-match short-circuit. Insertion of one character into a two-plus-character token is unchanged. Applies to P49 narrowing and the P50 expansion index. |
| Verification | Evaluator 2026-11-17: 43/43 active passes, 0 over-returns, 0 missing (score 4300, unchanged). 800-probe held-out sweep green (recall 1.0, 0 fabricated rows, 0 expired, 0 errors). No fixture relies on single-character fuzzy pairs. |
| Upstream | Not sent. |

### P53 — restore the query once more before abstaining

| | |
| --- | --- |
| File | `hermes_memory_provider/__init__.py` (`_handle_recall`, `_label_matched_rows`) |
| Date | 2026-11-17 |
| Reason | P51 abstains on fully labeled rows with zero label evidence. For two-edit-typo queries, the engine can return rows that all lack evidence — the restored query (P50) ranks the right row, but it was never issued because the first call was non-empty. |
| Behaviour | When P48/P49 narrowing finds no match in a fully labeled set and the P50 expansion rewrites the query, recall retries once with the restored query before abstaining. Evidence is checked against the original query (as in P50). A mixed-metadata retry keeps engine order (P48 contract); a fully labeled retry with no match abstains (P51). Extracted the metadata-read and evidence pass into `_label_matched_rows`. |
| Verification | Evaluator 2026-11-17: 43/43, 0 over-returns, 0 missing (score 4300). 800-probe held-out sweep green. Engine-free coverage: `test_abstaining_recall_retries_with_restored_query`, `test_abstaining_recall_without_expandable_tokens_returns_zero`, all prior label-recall tests (23 pass). |
| Upstream | Not sent. |

### P55 — model and engine provenance on stored lessons

| | |
| --- | --- |
| Files | `hermes_memory_provider/__init__.py` (`_handle_remember`, `_handle_recall`, `_handle_get`, `_prefetch_bank`, `system_prompt_block`, the `_stamped_metadata` / `_provenance_line` helpers and the `_provenance_*` / `_row_metadata` reads), `hermes_memory_provider/hermes_llm_adapter.py` (`active_model_id`, `_clean_model_id`) |
| Date | 2026-11-19 |
| Reason | P40 separates hypotheses from tested lessons but stores no record of the model or engine that produced one. Hermes users switch models often, so after a switch a recalled lesson implied it had been learned under the current model. The Self-Improvement Memory work (arXiv:2611.18856v1) shows that a lesson which survives a model or benchmark switch carries its provenance: treat entries as prior experience, not current instruction. |
| Behaviour | Every lesson write records `metadata.provenance = {model, hermes_version, engine_version, recorded_at}` on the existing write: `mnemosyne_remember`, a `remember` operation inside `mnemosyne_batch`, and an approved `mnemosyne_apply_pending` replay (the stamp is made when the write is staged — that is when the proposing model is known — and the approved replay preserves it, so `recorded_at` stays the proposal time even if approval comes days later under another model). Turn capture (`sync_turn`, `on_delegation`, `on_memory_write`) is deliberately not stamped: it stores transcript rows, not lessons, and its worker thread cannot see the per-turn runtime model. No schema change, no new table, no new key, no network call, no registration. The model id comes from `agent.auxiliary_client._read_main_model()` — the helper Hermes' own plugin-LLM facade uses, which prefers the live per-turn runtime override and falls back to `config.yaml` `model.default` — and from an explicit `MNEMOSYNE_MODEL_ID` declaration when the host reports nothing (measurement arms, scripts). Any failure stores `model: null`; a value is never guessed and never synthesised from the current provider. Recall rows and `mnemosyne_get` gain one `learned_under` field ("learned under model X, engine 3.15.1, …"), and prefetch injection gains at most one indented line per row. Legacy rows without provenance render byte-identically. Ranking, tool exposure and store layout are unchanged. `system_prompt_block` extends the P40 guidance with the lesson shape (root cause, what was tried, what was expected, what happened, failed attempts kept, one short structured label for the root cause) and with "a recalled lesson is prior experience from the model and engine named on it". |
| Verification | `tests/test_provider_provenance.py` (engine-free): the host helper and the declared fallback (the host answer wins), `null` for an unknown model, stamping without mutating caller metadata, legacy rows unchanged on all three surfaces, `mnemosyne_get` decoding the stored `metadata_json` string, exactly one extra line per prefetch row, malformed metadata ignored or passed through, guidance text present. `tests/test_provider_engine_contract.py::test_lessons_carry_the_model_and_engine_they_were_learned_under` proves the same round trip against the real pinned engine: the stamp persists, `get`/`recall`/prefetch each show one line, and an undeclared, hostless write stores `model: null`. `tests/test_vendored_provider.py` gates the snapshot hashes and this entry. `bench/scorecard` carries the carried-lesson probe (`bench/sim` kind `carried_lesson`) that measures the same lesson written under one model and queried under another; see `bench/scorecard/README.md`. The model-id read itself is recorded in [../../research/hermes-model-id-spike.md](../../research/hermes-model-id-spike.md). |
| Upstream | Not sent. Depot-specific: upstream has no equivalent of `MNEMOSYNE_MODEL_ID`, and the model-id read depends on a private Hermes helper that fails to `null` rather than to a wrong value. |
| Numbering | P54 stays reserved for the P51 opt-out switch named at the end of `research/research_log.md`; it is planned, not landed, so this patch takes P55. |

### P56 — doctor notes a gateway that predates the engine on disk

| | |
| --- | --- |
| Files | `hermes_memory_provider/cli.py` (`_engine_newer_than_boot`, `_gateway_engine_freshness_note`, doctor note wiring) |
| Date | 2026-11-20 |
| Reason | A running gateway keeps imported engine code for the life of the process. A PM generation rebuild or patch re-apply after its boot leaves fresh-process doctor green while the live process serves stale code; on one host turn capture silently stopped for two days while MCP recall kept answering, because the gateway had booted onto an unpatched generation. |
| Behaviour | Doctor compares the newest engine source mtime (excluding `__pycache__`) against the running gateway's boot time (`gateway.pid` record, `/proc/<pid>/stat` starttime, `/proc/stat` btime). When engine files are newer it prints one `[note]` line recommending a restart. Informational only: the exit status is unchanged, and unreadable inputs (no `gateway.pid`, dead pid, non-Linux) yield no note. |
| Verification | `tests/test_provider_doctor_freshness.py` (engine-free): mtime comparison including `__pycache__` exclusion, missing/garbage records fail soft, live-process note on Linux. `tests/test_vendored_provider.py` gates the snapshot hashes and this entry. |
| Upstream | Not sent. Depot-specific: reads Hermes' `gateway.pid` record and Linux `/proc`. |
| Numbering | P54 stays reserved for the P51 opt-out switch; P55 landed, so this patch takes P56. |
