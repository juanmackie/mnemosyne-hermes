# Contract audit — vendored `hermes_memory_provider` (mnemosyne-memory 3.15.1)

Scope: the vendored snapshot in `hermes_memory_provider/`, audited against the
`MemoryProvider` ABC and `MemoryManager` of **hermes-agent 0.19.0** in a scratch
venv, with an AST signature diff against the installed **0.18.2** checkout. Both
published versions are in the CI smoke matrix.

Method: AST signature diff of `agent/memory_provider.py::MemoryProvider` vs
`hermes_memory_provider/__init__.py::MnemosyneMemoryProvider`, plus reading the
call sites in `agent/memory_manager.py` and `hermes_cli/backup.py` that invoke
the hooks. This audit covers the vendored file itself; the 2026-09-18 review
covered the repo and the slim rewrite, not this file.

## Hermes 0.19.0 contract baseline

Installed `hermes-agent==0.19.0` into a scratch venv (no dependencies needed for
source inspection) and parsed `agent/memory_provider.py` and
`agent/memory_manager.py` with Python's AST. The baseline diff against 0.18.2
found **no signature changes across all 19 `MemoryProvider` methods**. The
0.19.0 hook signatures are:

```text
sync_turn(user_content, assistant_content, *, session_id='', messages=None)
on_session_switch(new_session_id, *, parent_session_id='', reset=False, rewound=False, **kwargs)
on_pre_compress(messages) -> str
on_delegation(task, result, *, child_session_id='', **kwargs)
on_memory_write(action, target, content, metadata=None)
```

The 0.19.0 `MemoryManager` has 33 methods vs 31 in 0.18.2. Its background
sync contract is material: `sync_all()` submits provider `sync_turn()` calls to
a single-worker serialized executor; the provider call itself is not inline in
the agent turn. The manager added `_forget_background_future`,
`_prefetch_provider`, and `shutdown_drain_state`, added the optional
`external_prefetch_timeout` constructor argument, and made
`_submit_background` accept keyword-only `kind`. There are no
`spawn_context_thread`, `recall_status`, or `identity_signature` symbols in the
0.19.0 package. The first is not a Hermes API available to this supported range;
the latter two are not `MemoryProvider` hooks in these releases.

This is a source/API baseline, not a live gateway run. Discovery and callback
behavior are tested separately by the onboarding lane.

## Conformance summary

| ABC member | Kind | Result |
|---|---|---|
| `name` | abstract | implemented, `-> str`, returns `"mnemosyne"` |
| `is_available()` | abstract | implemented, `-> bool`; `unavailable_reason()` exposes the sanitized failure reason (F11 addressed) |
| `initialize(session_id, **kwargs)` | abstract | implemented, signature matches |
| `get_tool_schemas()` | abstract | implemented, `-> List[Dict[str, Any]]` |
| `handle_tool_call(tool_name, args, **kwargs)` | concrete | implemented **without `**kwargs`**; still call-compatible (F8) |
| `system_prompt_block()`, `prefetch`, `queue_prefetch`, `shutdown`, `on_turn_start`, `on_session_end`, `get_config_schema`, `save_config` | concrete | overridden, signatures match |
| `sync_turn(user, assistant, *, session_id, messages)` | concrete | overridden with optional `messages`; opted-in tool turns are stored (F1 addressed by P5) |
| `on_memory_write(action, target, content, metadata=None)` | concrete | overridden with `metadata=None`; metadata reaches the engine write (F2 addressed by P6) |
| `on_pre_compress`, `on_session_switch`, `on_delegation` | concrete | implemented locally (P16/P17); checkpoint failure cannot abort host compression because Hermes catches hook exceptions (F3) |
| `backup_paths` | concrete | ABC default retained; Hermes backup already covers the default in-home DB, but not external DB paths (F4) |

All four abstract members are implemented with matching signatures, so the class
is instantiable by the loader. The gaps below are all in optional hooks and are
guarded by the manager's introspection, i.e. they degrade rather than crash.

## Findings

### F1 — `sync_turn` does not accept `messages` (lossy, guarded)

`agent/memory_manager.py:547` introspects the signature
(`_provider_sync_accepts_messages`) and, when `messages` is absent, calls
`sync_turn(user, assistant, session_id=...)` without the message list. So the
provider only ever sees the two turn contents, never the full turn messages.
Not a crash; a fidelity ceiling. **Patched as P5** (accept-and-store): the tool
and function turns from `messages` are stored when `sync_roles` opts in.

### F2 — `on_memory_write` does not accept `metadata` (drops it, guarded)

`memory_manager.py:903` (`_provider_memory_write_metadata_mode`) resolves the
call style from the signature. The vendored provider takes three positional
arguments, so the mode is `"none"` and the metadata dict is dropped before the
mirror write (`source=f"builtin_memory_{target}"`, hardcoded importance/scope).
**Patched as P6** (accept-and-store): metadata is handed to the engine's own
`remember(metadata=...)` parameter.

### F3 — pre-compression callback cannot enforce fail-closed checkpoints

The provider now overrides `on_pre_compress` (P17): it returns bounded
user/assistant excerpts for the compression prompt and optionally writes an
atomic v1 checkpoint under `$HERMES_HOME/mnemosyne/checkpoints/` when
`memory.mnemosyne.require_checkpoint` is true. The provider method raises
`CheckpointError` for invalid/oversized content or I/O failure. However,
`agent/memory_manager.py::on_pre_compress` catches `Exception` from every
provider, logs at debug, and continues with the remaining hooks. Therefore the
provider cannot guarantee that Hermes aborts compression; claiming this flag is
fail-closed end-to-end would be false. We deliberately do not monkey-patch
Hermes or use `BaseException` to escape its callback boundary. The limitation is
explicitly documented in the config schema and here; a true fail-closed
contract requires an upstream Hermes API change. The optional checkpoint is
local, bounded (100 text messages / 256 KiB), atomic, stored in an owner-only
`0700` POSIX directory with private temporary files, and keyed by a hash of the
transcript session id; default behavior writes no snapshot.

### F4 — `backup_paths()` is not overridden (documentation constraint, not a code fix)

`hermes_cli/backup.py:164` calls `backup_paths()` on a **freshly loaded,
un-initialized** provider (explicitly "no network, no init"), so any patch
reading `self._beam.db_path` would return `[]` in practice. Worse, the caller
(`backup.py:355-372`) **skips paths outside the home directory by design**
(`skipped_external`), so declaring an out-of-home `MNEMOSYNE_DB_PATH` would not
back it up anyway.

Consequence to document rather than patch: the engine's default store,
`$HERMES_HOME/mnemosyne/data/mnemosyne.db`, is inside `HERMES_HOME` and is
already covered by `hermes backup`. A store relocated outside the home directory
is **not** covered by `hermes backup` — that is a Hermes constraint
(`backup_paths` can only carry in-home paths into `_external/`).

### F5 — `register(ctx)` originally never registered the provider (fixed)

Before local patch P1, `register(ctx)` registered a CLI command, then tried
`from hermes_plugin import register` inside `try/except: pass`; it never called
`ctx.register_memory_provider(...)`. The loader's collector path
(`plugins/memory/__init__.py` `_ProviderCollector`) therefore captured nothing
and reached the provider only through its fallback scan for a `MemoryProvider`
subclass. P1 adds explicit registration; loader tests and onboarding smoke check
that only one provider is registered.

### F6 — silent `MemoryProvider = object` fallback

`hermes_memory_provider/__init__.py` imports the ABC inside `try/except ImportError` and
substitutes `object`. Without Hermes on the path the class silently stops being
a provider instead of failing loudly. Keep the import tolerance (it is what makes
the module importable in a bare venv for tests), but the *availability* path
must be loud — see F11.

### F7 — `_mnemosyne_root = Path(__file__).resolve().parent.parent`

That is the **directory containing the package**, which is why the vendoring
location matters:

- the import guard inserts it into `sys.path`, so in this repo it resolves to
  `integrations/hermes-provider/`;
- it is the base of the shared-surface store
  (`integrations/hermes-provider/data/shared/mnemosyne.db` when unset) — do not
  let a stray `data/` directory appear there;
- the sibling `hermes_plugin` package it tries to import does not exist in this
  repo, so that branch stays a silent no-op; the provider's tools come from
  `get_tool_schemas()`, not from `hermes_plugin`.

### F8 — tool results are JSON strings (conformant)

`handle_tool_call` returns `json.dumps(...)` for normal, unknown-tool, exception
and unavailable paths, including a structured
`{"status": "memory_unavailable", ...}` payload when initialization failed.
`_init_error_reason()` is the sanitized
reason accessor (truncated to 200 chars, whitespace-collapsed). This is the
"tool results as JSON strings" contract, and it holds.

### F9 — `recall_status` / `identity_signature` are not Hermes 0.18/0.19 hooks

Neither symbol appears in the vendored snapshot nor in the audited Hermes
0.18.2/0.19.0 packages, and neither belongs to `MemoryProvider` in the
supported range. We deliberately do not add dead, undocumented methods with
invented signatures. Identity capture remains `_capture_identity_signals` /
`_identity_fichas`; recall diagnostics is the `mnemosyne_recall_diagnostics`
tool. Revisit only when a supported Hermes release defines a caller and
contract for these names.

### F10 — engine coupling (the pin is a contract)

`__init__.py` imports from the engine at module import time:
`mnemosyne.core.episodic_graph`, `mnemosyne.core.beam`,
`mnemosyne.batch_tool`, `mnemosyne.hermes_config`,
`mnemosyne.integrations.hermes_persona_prompt`, plus lazy
`mnemosyne.core.memory.Mnemosyne`, `mnemosyne.diagnose`, `mnemosyne.core.*`
inside handlers. Hence the pin `mnemosyne-memory[embeddings]>=3.15.1,<3.16` in
`pyproject.toml`, and hence "engine missing" must be loud (F11) rather than a
hollow provider.

### F11 — `is_available()` must explain engine failures (addressed)

`is_available()` still returns the ABC-required boolean, but now records the
caught engine/import failure and exposes a whitespace-collapsed, bounded reason
through `unavailable_reason()`. The Hermes discovery path therefore receives
`False` while doctor and diagnostics can report why instead of presenting a
silent no-op. The bare-venv loader regression and sanitized reason are covered
by the provider loader/doctor contract tests (P2/P3).

### F12 — `sync_roles` now accepts `tool`

Consequence of P5: `_VALID_SYNC_ROLES` gained `"tool"` and the config-schema
description documents it (last 5 messages, 2000 chars, importance 0.2). Default
(`["user"]`) behaviour is unchanged.

### F13 — direct `sync_turn()` calls are synchronous; Hermes dispatch is not

`MnemosyneMemoryProvider.sync_turn()` performs Beam writes inline. This is
intentional for the supported Hermes path: AST/source inspection of both 0.18.2
and 0.19.0 confirms `MemoryManager.sync_all()` submits it to a single-worker,
serialized background executor, so the user-facing turn does not wait for DB
work. A smoke regression test now holds a fake DB write open and verifies
`sync_all()` returns before release, then drains the executor and reads the
persisted fact back; it also prints the direct-call baseline against the same
injected delay. A provider-level second turn-write worker was rejected: it would make
Hermes `flush_pending()` report completion before the write is durable and
would duplicate ordering/lifecycle management. Direct callers of the provider
method still block and should use Hermes `MemoryManager.sync_all()`.

Hermes 0.18.2/0.19.0 expose no `spawn_context_thread` helper. The provider now
uses a local compatibility helper that copies `contextvars` into its existing
sleep workers; it uses stdlib `threading.Thread` internally and does not add a
second turn-sync worker. The helper has an engine-free context propagation test.
The Linux/macOS onboarding smoke measures the direct blocking baseline and the
`MemoryManager.sync_all()` dispatch path with an injected DB delay, then drains
the executor and confirms persistence. GitHub Actions run
[36311054419](https://github.com/juanmackie/mnemosyne-hermes/actions/runs/36311054419)
passed all four Hermes 0.18.2/0.19.0 × Ubuntu/macOS variants: observed dispatch
was 0.8–1.9 ms versus a 217.3–318.4 ms direct baseline. These are single-run
observations under the smoke's injected delay, not general performance claims.

### F14 — plugin metadata and bundled/user discovery collision (addressed)

Added `hermes_memory_provider/plugin.yaml` with the provider metadata and hook
names Hermes' plugin CLI discovery consumes, and included it in the provider
wheel. The onboarding smoke seeds the same provider name in Hermes' bundled
root and the installer-created user symlink, then asserts one discovered row,
bundled-root precedence, one CLI command, and a provider loaded through
`load_memory_provider()`. It also checks one `register(ctx)` result, one manager
provider, and the real `doctor`/`memory status` path. GitHub Actions run
[36311054419](https://github.com/juanmackie/mnemosyne-hermes/actions/runs/36311054419)
passed all four Ubuntu/macOS × Hermes 0.18.2/0.19.0 smoke variants, including
`doctor` exit 0 and exactly one discovered provider despite the collision.

### F15 — all engine tools exposed by default (addressed)

The provider now defaults to the four core tools (`mnemosyne_remember`,
`mnemosyne_recall`, `mnemosyne_stats`, `mnemosyne_forget`). Operators must set
`memory.mnemosyne.tools: ["*"]` to expose all 40 engine tools; explicit subsets
and an empty list remain supported, while a mixed wildcard or unknown name is
rejected. `docs/HERMES_INTEGRATION.md` owns the canonical 40-name table and the
provider suite covers default, wildcard, subset, and invalid configuration.

### F16 — shared SQLite access bypassed turn-sync serialization (addressed)

Hermes `MemoryManager.handle_tool_call()` dispatches directly without flushing
its background `sync_all()` worker. The engine's active Beam connection permits
cross-thread use (`check_same_thread=False`), so provider entry points must
serialize complete operations, not only individual SQLite statements. The
existing lock covered turn sync, bank recall, and diagnostics, but omitted
explicit tools, identity/model prefetch, and built-in
memory mirroring.

P19 extends that lock to those entry points and makes it reentrant for nested
diagnostic access. Custom prefetch callbacks run outside the lock. Five
engine-free regressions in `tests/test_provider_db_path.py` cover paused real
SQLite transactions, failed turn rollback, and diagnostic reentrancy. This is
provider-boundary evidence; the changed provider has not been exercised on a
live gateway.

### F17 — provider ↔ engine behaviour is now covered by a CI-enforced lane

`CONTRACT_AUDIT.md` up to F16 audits provider ↔ Hermes signatures. Nothing
audited provider ↔ engine behaviour: the CI tests job installs only
`pytest pytest-cov`, every provider test uses `FakeBeam`, the one
engine-present case in `tests/test_provider_loader.py` prints "skip" and
returns without the engine, and the onboarding smoke only exercises doctor,
registration and one `sync_turn → remember` round trip. No tool handler was
called against the real engine. The `mnemosyne_update` / `mnemosyne_get`
mismatch (P20) reached a live agent for exactly this reason, and the same
investigation found `mnemosyne_forget` has the same gap for episodic rows.

`tests/test_provider_engine_contract.py` closes the gap. Against the pinned
real engine in a temp dir, through the public `handle_tool_call` (so the P19
lock and `has_tool` are covered):

- Check A (static): AST-scan of the snapshot for every
  `self._beam.<attr>` / `self._surface_beam.<attr>`; each must exist on a real
  `BeamMemory` instance. Catches engine renames on a pin bump with no
  hand-maintained list.
- Check B: a minimal-valid-args table covering exactly `ALL_TOOL_SCHEMAS` (a
  new upstream tool cannot be skipped). Each call must return parseable JSON
  with no `memory_unavailable` and no unhandled-exception shape. Sleep, import,
  export and model_refresh use `dry_run` / temp paths.
- Check C: ID visibility matrix over own-session working, global-from-another-
  session, private-from-another-session, episodic (via the engine's own
  `consolidate_to_episodic`, which writes a new summary row and leaves the
  working source in place), and shared-surface rows. Two recorded
  divergences, both asserted strictly so they fail when fixed: `forget` on
  episodic rows returns `not_found` while `get` resolves them, because the
  engine's `forget_working` is working-only; `validate` on episodic rows
  returns `memory_not_found` for the same reason (it only queries
  `working_memory`). Whether to patch episodic deletion is a product
  decision; the lane records both rather than changing them. A third
  candidate divergence — `validate` resolving private rows from other
  sessions through its unscoped lookup — is patched as P21 instead, because
  it returned another session's content to any caller.
- Check D: write → read coherence (remember, update, invalidate, forget
  reflected by recall and get).

2026-10-04 engine visibility audit extension: the strict lane additionally
calls BEAM update and the engine MCP handler directly, so P20 cannot hide an
engine failure. Regressions cover foreign-session global updates, ID/session
bind decoys, both cross-session modes, all P21 mutations, one runtime snapshot,
absent/foreign legacy mirrors, mirror rollback and event behavior, nested MCP
batch rollback, and session-local remember dedup. The engine diffs and original/
patched hashes live in `../engine-patches/`; `install.sh` and the pinned-engine
CI lane apply them. The cross-session toggle explicitly authorizes these ID
operations across sessions as well as recall; disabled preserves private-row
isolation. Bank isolation and working-only forget/validate remain unchanged.
Engine parity accepts only these exact audited diffs, checking original hashes
against wheel RECORD; other engine drift still fails. The dependency pin and
Hermes loader/type/lint exclusions are unchanged.

The lane runs in `test-all.sh` (skips with a visible line when the engine is
absent), strict under `./test-all.sh --require-engine`
(`MNEMOSYNE_REQUIRE_ENGINE=1`: missing engine fails), as the `engine-contract`
CI job (Python 3.11 and 3.13, provider + pinned engine installed), and weekly
against the newest engine release (ignoring the pin) with the result in the
drift issue body, so a pin bump becomes a checked decision. A re-vendor or pin
change must pass `./test-all.sh --require-engine`.

## Status

| Finding | Disposition / evidence |
|---|---|
| F1 | Fixed: `sync_turn(..., messages=...)` stores opted-in tool turns (P5); delegation task/result capture is a separate bounded `on_delegation` hook (P16). Provider tests cover bounds and opt-in. |
| F2 | Fixed: `on_memory_write(..., metadata=...)` forwards metadata to engine remember (P6). |
| F3 | Deliberate limit: P17 adds bounded excerpts and optional atomic checkpoints; Hermes catches hook exceptions, so the flag cannot fail compression closed. |
| F4 | Documented Hermes constraint: default in-home DB is backed up; external DB paths are skipped by Hermes backup. |
| F5 | Fixed: `register(ctx)` registers the provider and CLI command (P1); real loader is exercised by onboarding smoke. |
| F6 | Deliberate compatibility: bare-venv import fallback remains for tests, while unavailable engine failures are reported by F11. |
| F7 | Deliberate layout contract: provider root is the vendoring directory; the sys.path guard prevents a copied install from shadowing the engine. |
| F8 | Conformant: all tool-call result paths return JSON strings, including unavailable/error cases. |
| F9 | Deliberately not added: supported Hermes has no `recall_status` or `identity_signature` caller/contract; diagnostics and identity capture use existing APIs. |
| F10 | Deliberate pin: provider imports engine internals and stays within `mnemosyne-memory[embeddings]>=3.15.1,<3.16`. |
| F11 | Fixed: `is_available()` keeps the required boolean and `unavailable_reason()` exposes a bounded sanitized reason; loader/doctor tests cover it. |
| F12 | Fixed: `sync_roles` accepts `tool` and defaults remain unchanged. |
| F13 | Deliberate concurrency contract: Hermes serializes provider turn writes on its background executor; no second turn-write worker is added. P22 tracks the separate consolidation worker and removes its auto-sleep join from turn sync. Run 36311054419 passed all four real-Hermes variants; injected-delay dispatch measured 0.8–1.9 ms vs 217.3–318.4 ms direct. |
| F14 | End-to-end verified: plugin metadata and collision assertions pass on Ubuntu/macOS with Hermes 0.18.2/0.19.0 in run 36311054419; doctor exits 0 and exactly one provider is discovered. |
| F15 | Fixed: four-tool default, explicit all-tools opt-in, and one canonical 40-tool table. |
| F16 | Fixed: shared-connection tools, prompt reads, and mirror writes serialize with background turn capture (P19); real SQLite transaction and nested-lock regressions pass locally. |
| F17 | Covered: provider ↔ engine behaviour is driven by the engine-backed contract lane (Checks A–D; episodic forget/validate divergences recorded strictly, validate visibility patched as P21). Lane passes against the pinned engine (3.15.1, verified 2026-10-03); CI enforcement pending the branch push. |

Every future local provider change must go through `PATCHES.md` +
`VENDORED_FROM.json` (enforced by `tests/test_vendored_provider.py`).

## 2026-10-08 consolidation concurrency amendment (P22)

The previous auto-sleep join timeout left the worker holding the shared Beam
lock. A deterministic regression reproduces blocked prefetch after that timeout
against the prior snapshot. P22 tracks one independent consolidation connection,
keeps SQLite operations serialized with foreground access, and yields only for
model and embedding computation through audited engine patches. Turn writes
remain durable before `sync_turn()` returns; consolidation is asynchronous and
best effort, without a persistent job queue.

`tests/test_provider_consolidation.py` gates real pinned-engine summarization,
model refresh and degradation, traces worker SQL under the shared lock, and
verifies concurrent foreground write/read coherence. Source-content claim checks
reject stale summaries after edits, and degradation compares current content and
tier before replacement. Worker proposal enrichment checks current content after
embedding before storing derived data. Lifecycle tests cover single admission,
session snapshots, reinitialization, shutdown and host-backend ownership.
Conflict validation rechecks both source contents after model work before
invalidating a row.

The engine pin, provider registration and default tool surface remain unchanged.
Verification uses a temporary copy of the pinned engine with exact audited
patches. This amendment does not claim live-gateway verification or exercise a
real LLM request; model waits and outputs are deterministic test doubles.
