# Hermes provider configuration

Set these keys under `memory.mnemosyne` in Hermes' config. The setup wizard
asks only about `db_path`, `profile_isolation`, `default_scope`, and `tools`.
Existing advanced configurations remain supported.

```yaml
memory:
  provider: mnemosyne
  mnemosyne:
    default_scope: session
    sync_roles: [user]
    auto_sleep: true
    sleep_threshold: 50
```

| Key | Default | Behavior |
| --- | --- | --- |
| `db_path` | unset | Config path wins over `MNEMOSYNE_DB_PATH`, then the engine default; also wins over profile isolation. |
| `profile_isolation` | `false` | Separate per-profile banks when no explicit DB path is set. |
| `default_scope` | `session` | Default for explicit remembers **and automatic turn capture**. Use explicit global scope for intentional durable facts; changing this globally also broadens raw autosave. |
| `tools` | four core tools | See the [canonical exposure table](HERMES_INTEGRATION.md#canonical-tool-names-and-default-exposure). `[]` hides tools; `["*"]` opts into all tools. |
| `auto_sleep` | `true` | Allow threshold-triggered background consolidation. `MNEMOSYNE_AUTO_SLEEP_ENABLED` is the environment equivalent. Optional model work inherits Hermes' model. |
| `sleep_threshold` | `50` | Working-memory count before automatic consolidation. |
| `reflect` | `{disabled_for_cron: true, max_calls_per_session: 3}` | Reflection guardrails. A negative call limit disables the cap. Environment equivalents: `MNEMOSYNE_REFLECT_DISABLED_FOR_CRON`, `MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION`. |
| `ignore_patterns` | `[]` | Regex filters for automatic capture; list or comma-separated string. |
| `skip_contexts` | `cron,flush,subagent,background,skill_loop` | Contexts that skip memory initialization/capture/injection. Empty enables all. Environment: `MNEMOSYNE_SKIP_CONTEXTS`. |
| `sync_roles` | `[user]` | `assistant`, `tool`, and `delegation` require explicit opt-in. `[]` disables autosave. Identity extraction also requires `user`. Environment: `MNEMOSYNE_SYNC_ROLES`. |
| `shared_surface_path` | `data/shared/mnemosyne.db` | Separate shared-surface database. |
| `shared_surface_read` | `false` | Merge shared-surface results into explicit recall, with bank tags. |
| `require_checkpoint` | `false` | Legacy bounded local compression snapshot. Supported Hermes 0.18.2/0.19.0 catches hook failures, so this cannot guarantee compression aborts. |
| `vector_type` | `int8` | Reserved setting; not wired to BeamMemory at runtime. |

## Automatic context controls

| Environment variable | Default | Behavior |
| --- | --- | --- |
| `MNEMOSYNE_PREFETCH_TOTAL_CHARS` | `8000` | Complete returned block ceiling, including identity/model/bank/custom sources. Truncation and omitted items are visibly marked. Identity receives priority. Characters are a deterministic size proxy, not actual tokenizer counts. |
| `MNEMOSYNE_PREFETCH_CONTENT_CHARS` | profile limit | Optional per-item cap in addition to the total cap. |
| `MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE` | `false` | Explicit opt-in to silently inject raw tool/delegation captures. Capture and manual recall remain available independently. |
| `MNEMOSYNE_PREFETCH_CACHE_ENABLED` | `false` | Experimental bounded exact-query cache and `queue_prefetch` warming on Hermes' existing serialized worker. Enable only after measuring useful hits for your sessions. |

Cache keys include the exact-query digest, session, profile, canonical owner,
runtime visibility, render controls, and local/external SQLite generation.
Entries expire after 30 seconds. Custom source callbacks must supply a stable
`cache_revision` for caching to be eligible. Caching creates no writer queue.

`get_prefetch_diagnostics()` and the opt-in `mnemosyne_recall_diagnostics` tool
expose counts, source contributions, output size, truncation, lock wait, recall
time, cache hits/misses, and sanitized failure classes. They omit queries and
memory content. Diagnostics distinguish a legitimate empty result from a
provider failure. Newer Hermes' `recall_status()` hook supplies a badge only
for successful nonempty context; legacy hosts retain their callback behavior.

Hermes native `MEMORY.md`/`USER.md` remain useful for concise profile anchors.
Mnemosyne supplies searchable facts and episodic evidence. Skills carry
procedures; `session_search`, when exposed by Hermes, is the transcript fallback.
Dynamic recall stays in per-turn context while provider instructions stay static.

## Consolidation status

The existing opt-in `mnemosyne_diagnose` tool includes a `consolidation` snapshot
for the provider's automatic/session-end worker. It reports `idle`, `running`,
`succeeded` or `failed`, the last trigger, epoch start/finish times, monotonic
duration in milliseconds, and an error class if the worker failed. Reused and
skipped trigger counts, stopping state and a shutdown-timeout flag help explain
why another trigger did not start a worker. These fields contain no memory text,
session identifiers, paths or error messages.

Status is in memory for this provider instance and resets on reinitialization.
It does not survive a process restart or represent a durable job ledger. The
snapshot covers the existing tracked worker; a manually invoked sleep tool has
its own response. No token/cost values are inferred. The four default tools and
reflection budgets are unchanged.

## Lifecycle compatibility

Native mirror replace/remove retires only an owned engine row identified by
authoritative `previous_content` or an explicit native entry ID. Ownership is
persisted alongside the engine store. Legacy callers lacking that identity
cannot safely request destructive correction; unrelated matches are never
deleted by broad text search. Existing historical mirrors cannot be retroactively
identified safely.

Native mirrors carry a target provenance marker so engine deduplication does
not claim an identical manually stored fact. Multiple native associations may
share a row; retirement waits for the last association. Exact-ID invalidation
also expires consolidated episodic mirrors while preserving historical evidence.

Per-turn author metadata, when Hermes supplies it, is captured as provenance
and does not change session/global visibility. A cheap, uninitialized
`identity_signature()` lets newer gateways invalidate cached providers when
relevant configuration changes.

The provider advertises checkpoint API v2 only to a host exposing that API.
It writes the complete normalized evidence durably and idempotently before
acknowledging compression. End-to-end abort on failure additionally requires
Hermes' `compression.checkpoint_required` gate. These newer hooks are tested
against pinned main source; published support remains 0.18.2/0.19.0, whose
compression hooks are best effort. See the [contract audit](../integrations/hermes-provider/CONTRACT_AUDIT.md).

`backup_paths()` resolves configuration without initialization or opening the
database. Hermes can snapshot declared stores outside `HERMES_HOME` only when
they remain inside the operating-system user's home. Stores on arbitrary
external volumes still need an independent backup. SQLite snapshots include
committed WAL contents; lifecycle tests verify the host helper and restore.
