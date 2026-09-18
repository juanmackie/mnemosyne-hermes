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
| Safety | Only the two class bases reference engine names at import time (verified by AST scan); every other use is behind `initialize()`/`is_available()`, and `is_available()` returns False whenever the guard tripped, so the placeholders are never reached on an unavailable provider. |

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

The remaining vendored files are byte-identical to the 3.15.1 wheel RECORD.
`register_memory_provider(ctx)` was already present upstream and is unchanged.

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
