# Contract audit — vendored `hermes_memory_provider` (mnemosyne-memory 3.15.1)

Scope: the vendored snapshot in `hermes_memory_provider/`, audited against the
`MemoryProvider` ABC and the memory manager of **hermes-agent 0.18.2** (the
version installed in the local Hermes venv).

Method: AST signature diff of `agent/memory_provider.py::MemoryProvider` vs
`hermes_memory_provider/__init__.py::MnemosyneMemoryProvider`, plus reading the
call sites in `agent/memory_manager.py` and `hermes_cli/backup.py` that invoke
the hooks. This audit covers the vendored file itself; the 2026-09-18 review
covered the repo and the slim rewrite, not this file.

## Conformance summary

| ABC member | Kind | Result |
|---|---|---|
| `name` | abstract | implemented, `-> str`, returns `"mnemosyne"` |
| `is_available()` | abstract | implemented, `-> bool` (**reason not exposed** — F11) |
| `initialize(session_id, **kwargs)` | abstract | implemented, signature matches |
| `get_tool_schemas()` | abstract | implemented, `-> List[Dict[str, Any]]` |
| `handle_tool_call(tool_name, args, **kwargs)` | concrete | implemented **without `**kwargs`**; still call-compatible (F8) |
| `system_prompt_block()`, `prefetch`, `queue_prefetch`, `shutdown`, `on_turn_start`, `on_session_end`, `get_config_schema`, `save_config` | concrete | overridden, signatures match |
| `sync_turn(user, assistant, *, session_id, messages)` | concrete | overridden **without `messages`** (F1) |
| `on_memory_write(action, target, content, metadata=None)` | concrete | overridden **without `metadata`** (F2) |
| `on_pre_compress`, `backup_paths`, `on_session_switch`, `on_delegation` | concrete | **not overridden** — ABC defaults used (F3, F4) |

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

### F3 — `on_pre_compress` is not overridden

`conversation_compression.py:634` and `memory_manager.py:892` call it; the ABC
default returns `""`, so Mnemosyne contributes nothing to the compression
summary prompt. Because `sync_turn` runs every turn, nothing is *lost* that was
not already synced — but pre-compression extraction does not happen. This is the
hook the TODO's "`on_pre_compress` semantics" item points at; in this version
the provider simply does not participate in it. **Decision needed** (leave as
upstream chose, or implement).

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

### F5 — `register(ctx)` never registers the provider

`__init__.py:3774-3793`: `register(ctx)` registers a CLI command, then tries
`from hermes_plugin import register` inside `try/except: pass`. It never calls
`ctx.register_memory_provider(...)`, so the loader's collector path
(`plugins/memory/__init__.py` `_ProviderCollector`) captures nothing and the
provider is only reached by the loader's fallback scan for a `MemoryProvider`
subclass. Two entry points, one of them decorative. Addressed in B3.

### F6 — silent `MemoryProvider = object` fallback

`__init__.py:1265-1270` imports the ABC inside `try/except ImportError` and
substitutes `object`. Without Hermes on the path the class silently stops being
a provider instead of failing loudly. Keep the import tolerance (it is what makes
the module importable in a bare venv for tests), but the *availability* path
must be loud — see F11.

### F7 — `_mnemosyne_root = Path(__file__).resolve().parent.parent`

That is the **directory containing the package**, which is why the vendoring
location matters:

- it is inserted into `sys.path` (line 69-71), so in this repo it resolves to
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
`{"status": "memory_unavailable", ...}` payload when initialization failed
(`__init__.py:2460-2490`). `_init_error_reason()` (line 1479) is the sanitized
reason accessor (truncated to 200 chars, whitespace-collapsed). This is the
"tool results as JSON strings" contract, and it holds.

### F9 — `recall_status` / `identity_signature` do not exist in this version

Neither symbol appears in the vendored snapshot nor in hermes-agent 0.18.2.
Identity handling is `_capture_identity_signals` / `_identity_fichas`; recall
diagnostics is the `mnemosyne_recall_diagnostics` tool. The TODO's named
symbols do not map onto this artifact — recorded so a future audit does not hunt
for them.

### F10 — engine coupling (the pin is a contract)

`__init__.py` imports from the engine at module import time:
`mnemosyne.core.episodic_graph`, `mnemosyne.core.beam`,
`mnemosyne.batch_tool`, `mnemosyne.hermes_config`,
`mnemosyne.integrations.hermes_persona_prompt`, plus lazy
`mnemosyne.core.memory.Mnemosyne`, `mnemosyne.diagnose`, `mnemosyne.core.*`
inside handlers. Hence the pin `mnemosyne-memory[embeddings]>=3.15.1,<3.16` in
`pyproject.toml`, and hence "engine missing" must be loud (F11) rather than a
hollow provider.

### F11 — `is_available()` returns a bare `False`

`__init__.py:1509` catches `Exception` around `_get_beam_class()` and returns
`False` with no reason. Combined with a missing engine in the venv, the live
result is `available ✓` + silent no-op (the review's worst-UX finding).
Addressed in B4.

### F12 — `sync_roles` now accepts `tool`

Consequence of P5: `_VALID_SYNC_ROLES` gained `"tool"` and the config-schema
description documents it (last 5 messages, 2000 chars, importance 0.2). Default
(`["user"]`) behaviour is unchanged.

## Status

Patched: F1/F2 (P5/P6, accept-and-store), F5 (P1, registration), F11 (P2/P3,
loud availability), plus P4 (actionable doctor). Deliberately **not** patched:
F3 (`on_pre_compress` — upstream's choice, the engine has no
message-accepting extraction API to forward to) and F4 (`backup_paths` — Hermes
skips out-of-home paths by design, so a patch would be decorative). F6, F7, F9,
F10 are recorded behaviour/coupling notes, not defects. Every future local change must go through `PATCHES.md` +
`VENDORED_FROM.json` (enforced by `tests/test_vendored_provider.py`).
