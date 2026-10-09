# Memoryrepo review: proposed improvement plan

**Status:** Original proposed plan; phases 0–4 and the bounded phase 5 status slice implemented locally following user authorization. See [implementation record](MEMORYREPO_IMPLEMENTATION.md).  
**Date:** 2026-10-09, Australia/Brisbane.  
**Local baseline:** `5a1eb57cd844a2c58c5a5c5c330df04fffaca306`.  
**Upstream baseline:** [`supermemoryai/memoryrepo` at `54f7ff3065df8af687dc5c04a4bb17d6611e5fd1`](https://github.com/supermemoryai/memoryrepo/tree/54f7ff3065df8af687dc5c04a4bb17d6611e5fd1).

Three `gpt-6-luna` agents at high reasoning reviewed upstream implementation, Hermes-provider fit, and lite/validation fit. Their findings were checked against local source and two isolated reproductions. The existing untracked `MEMORYREPO_RESEARCH.md` and `OPENUI_RESEARCH.md` were preserved.

## Recommendation

Improve how users inspect memories and trace them to evidence. Start with the provider's audit reliability and a bounded inspection CLI. Add exact source references after proving what the pinned engine retains; offer Markdown as an optional read-only projection. Address the independent lite count-error defect in a small separate change.

Memoryrepo's useful patterns are source-linked facts, visible revisions, and conservative handling of stale writes. These are demonstrated by its [memory conventions](https://github.com/supermemoryai/memoryrepo/blob/54f7ff3065df8af687dc5c04a4bb17d6611e5fd1/src/server/memory.ts#L41-L59), [conflict checks](https://github.com/supermemoryai/memoryrepo/blob/54f7ff3065df8af687dc5c04a4bb17d6611e5fd1/src/server/memory-agent.ts#L316-L347), and [maintenance flow](https://github.com/supermemoryai/memoryrepo/blob/54f7ff3065df8af687dc5c04a4bb17d6611e5fd1/src/server/memory-agent.ts#L358-L465). Transfer the behaviors through our existing engine and Hermes interfaces.

## Findings that determine the scope

| Upstream pattern | Current project | Decision |
| --- | --- | --- |
| Short entry index plus searchable topic files | Provider already has hybrid retrieval, native profile anchors, an 8,000-character context ceiling, and prefetch diagnostics | Keep the existing context design; require evidence before adding another index or cache |
| Source references attached to facts | Explicit remember accepts source/metadata; turn capture has author provenance; exact message references are not consistently attached | Add capability-gated references using existing metadata first |
| Git revisions visible to the user | Internal audit events and specialized canonical/validation history exist; CLI lacks a general by-ID history view | Repair audit persistence, then expose bounded inspection |
| Incremental dreams with conflict handling and logs | Tracked engine consolidation and foreground access during inference already exist | Reuse this worker; consider status reporting after inspection work |
| Browsable linked Markdown | Provider exports whole-store engine JSON; lite has capped cards | Add an optional read-only Markdown projection after access rules are defined |
| Search before capture and explicit evidence | Lite already has describe-first guidance and stable card IDs; stored context/date are omitted from cards | Add small card improvements separately, without a schema migration |

Local foundations are documented in [provider configuration](HERMES_CONFIGURATION.md), [P22–P24](../integrations/hermes-provider/PATCHES.md), [lite cards](../src/mnemosyne_lite/cards.py), and the [existing provider evaluation](../bench/HERMES_PREFETCH_RESULTS.md).

Two defects were independently reproduced without opening a user memory database:

- **Audit events can disappear across threads.** `AuditLog` retains a SQLite connection created during initialization and catches subsequent errors. With a temporary DB, one initialization-thread event and one worker-thread event were attempted; only the former persisted. Query/count errors also become empty/zero results. See [audit implementation](../integrations/hermes-provider/hermes_memory_provider/audit.py#L47). The strict engine fixture currently [stubs audit initialization](../tests/test_provider_engine_contract.py#L146).
- **Lite MCP can conceal broken count queries.** A synthetic storage object returned one recall row but raised `StorageError` from both count methods. `call_tool` still returned `ok: true`, `count: 1`, `total: 1`. Its [fallbacks](../src/mnemosyne_lite/tools.py#L158) obscure the distinction between a valid result and a failed store read.

The upstream also has limitations we should improve on: citations are model-written conventions, and [source lookup](https://github.com/supermemoryai/memoryrepo/blob/54f7ff3065df8af687dc5c04a4bb17d6611e5fd1/src/server/transcripts.ts#L83-L100) can fall back to unrelated recent messages when the exact message is missing. Git commit and maintenance-cursor updates are separate persistence operations. No test/evaluation suite was found in the reviewed snapshot; it provides no measured evidence of a retrieval advantage over our implementation.

## Proposed implementation sequence

Effort estimates are relative scope, not delivery dates. Each slice should remain independently reviewable.

### 0. Characterize evidence and access boundaries — small

**Owner:** provider contract tests and documentation; engine investigation only.

Document behavior through remember → get → update/validate → consolidation → export. Check source/metadata retention, summary-to-original lineage, canonical/validation-history limits, and current session/profile visibility. Identify which supported Hermes hosts provide stable message references. Distinguish local operator inspection from agent-session inspection; define their access policy before adding commands.

Read-only inspection of installed patched engine 3.15.1 found that episodic rows retain `summary_of`, while `get()` omits that field; automatic sleep creates summary metadata rather than copying all input metadata. Existing JSON export includes all sessions. These observations motivate the round-trip audit; they do not establish end-to-end evidence preservation or scoped export safety. The engine-side source is covered by the [audited patch inventory](../integrations/engine-patches/README.md).

**Done when:** fixtures record exact retention/loss and visibility behavior, including unavailable/deleted references. Any missing engine capability is assigned upstream or to the audited engine-patch process. No schema migration or pin widening in this slice.

### 1. Repair audit persistence and add provider inspection — medium

**Owner:** provider `audit.py`, `__init__.py`, `cli.py`, relevant tests and documentation.

1. Use a connection strategy compatible with thread ownership, provider locking, transactions, and shutdown. Prefer one connection per calling thread; do not merely disable SQLite's thread check.
2. Preserve best-effort mutation logging, with bounded overhead and observable failure counts/classes. History reads must report storage failure explicitly rather than return empty history.
3. Add a successful-update event and document which operations remain uncovered. Allowlist metadata; avoid arbitrary source payloads or memory content in diagnostic logs.
4. Extend the existing `hermes mnemosyne` CLI with bounded `inspect --id` and/or `history --id` output. Show current record, available source information, and recorded events with text/JSON output. Command names are provisional until phase 0 establishes the access model.

**Done when:** regression coverage reproduces then fixes worker-thread event loss; real audit initialization is exercised in an engine-backed lane; injected initialization/write/read failures, lock contention, concurrent callers, result limits and shutdown are covered. Own/global/foreign session/profile behavior matches the chosen access policy. Deleted-record events require trustworthy event ownership; a join to live rows is insufficient. Old events with insufficient access metadata are handled conservatively.

Describe the view as **partial recorded history**, with failures/coverage visible. This slice does not promise complete revisions, atomic memory+audit commits, or undo. Keep the four default Hermes tools unchanged.

Audit initialization must not introduce DDL or persistent pragmas ahead of database classification; refused files must remain byte-identical.

### 2. Attach and inspect exact source references — medium, depends on 0–1

**Owner:** provider capture/source normalization and host-capability tests; engine changes only if phase 0 proves necessary.

Use existing metadata for a structured session/message reference when Hermes supplies a stable ID. Preserve explicit caller-supplied references as asserted references, distinguished from host-attached ones. Keep reference origin, author identity, access scope, and semantic support distinct. Extend inspection to display references and, where a supported host lookup exists, fetch bounded access-controlled evidence.

Trace consolidated records to eligible original rows when the engine provides lineage. Do not imply automatic sleep copies source metadata or that an exact source identifier proves the fact is true. Treat transcript search as an aid to finding evidence.

**Done when:** supported references round-trip through the paths characterized in phase 0; lineage does not reveal inaccessible originals; legacy hosts, missing/deleted messages and unsupported lookups return explicit unavailable results. Never derive a message ID from turn order or silently substitute recent messages for the cited source.

### 3. Add optional Markdown evidence export — medium, depends on 0–2

**Owner:** provider CLI/export layer and tests.

Render eligible engine rows into readable Markdown with stable IDs, available source references, and clearly labeled provenance gaps. Use a deterministic bounded index and detailed topic files if warranted. Generate from a consistent snapshot and explicitly select the access scope. Preserve the existing JSON export contract.

**Done when:** exported IDs and references match the selected eligible rows; source DB remains unchanged; no stale cache is used; session/profile exclusions and index bounds hold. Safe filenames, Markdown escaping and output-directory handling are covered. Do not add Git commits/pushes, Markdown import synchronization, or another authoritative store.

### 4. Improve lite reliability and evidence display — small, separate track

**Owner:** `tools.py`, `cards.py`, optional CLI/storage read helpers, lite tests.

First propagate failed `count_matching()`/`count()` reads through the existing MCP error path. A legitimate no-match remains a successful empty result; failed totals must not appear exact. Document existing bounded prefetch fallback totals separately from full matching counts.

Then add optional bounded context/date lines to cards using existing fields. Free-form `context` is descriptive metadata, not a verified citation. Preserve text tables, JSON compatibility, schema v3, tool names/aliases, and `sync_turn` skip semantics. Consider CLI by-ID inspection or JSONL export only if needed after provider work; they are not prerequisites for the fixes.

**Done when:** injected post-recall count failures yield `isError: true`; true empty and valid searches retain their success contracts. Long/missing/multiline context and timestamps render within documented bounds. Read/export commands do not invent missing stores, and foreign-file refusals stay byte-identical. A namespace filter controls selection; lite namespaces do not become an authentication boundary.

### 5. Evaluate optional maintenance improvements — status slice implemented in follow-up

Borrow visible maintenance status through existing diagnostics: trigger, start/end, state and sanitized error class. Reuse the tracked consolidation worker and reflection controls. Add reliable token/cost fields only if the engine or Hermes proxy supplies measured usage; omit unavailable values.

For any future change to consolidation claims, checkpoints or stale-record handling, first reproduce a concrete engine gap and specify conflict/interruption/crash behavior. Engine lifecycle/transaction changes belong upstream or in the existing audited patch process. Do not infer that upstream's Git conflict logic supplies SQLite concurrency safety.

## Verification and measures of success

For implementation, run focused regressions, the strict pinned-engine lane for provider changes, `./test-all.sh --skip-llm`, `bash scripts/checks.sh`, and applicable pre-commit checks. Register new gates in `scripts/checks.sh`. Any vendored edit requires its local-patch marker, `PATCHES.md` entry, and updated manifest hashes/bytes/lines in the same change. Run the onboarding smoke on its supported Linux/macOS lane for provider CLI/discovery changes; live gateway behavior remains a separate verification requirement.

Reuse the public-prefetch evaluation to guard required identities/facts, prohibited operational captures, visibility and the aggregate 8,000-character ceiling. Add synthetic inspection/provenance cases; grade exact IDs, source availability, access exclusions, bounded output and failure behavior. Record latency separately from correctness, especially audit overhead under contention. No recall, latency, token-cost, or answer-quality improvement is established by this review.

A dedicated lite recall-quality corpus is a later useful addition, following our own provider-evaluation practice. It is not borrowed evidence that memoryrepo performs better. Keep fixtures synthetic, reports reproducible, and grader failure cases explicit.

## Product boundaries

Preserve the `mnemosyne` provider's single registration, four default tools, engine pin, DB precedence, keyless operation and active-Hermes-model inheritance. Preserve the lite surface as a standalone schema-v3 SQLite product outside the Hermes venv. Keep engine schema/ranking ownership upstream.

The hosted Git vault, Cloudflare scheduler/accounts, OpenRouter model selection, notes editor and graph UI are outside this proposal. They add a separate application and dependencies without a demonstrated improvement to our provider. Implement the transferable ideas locally through existing surfaces.

**Recommended first implementation:** phases 0–1, with the lite count-error fix as a separate small change. Source attachment and Markdown export follow the characterization results.

## Detailed review notes

- [Upstream implementation review](MEMORYREPO_UPSTREAM_REVIEW.md).
- [Provider fit and acceptance checks](MEMORYREPO_PROVIDER_REVIEW.md).
- [Lite fit and validation gaps](MEMORYREPO_LITE_REVIEW.md).

These are original review artifacts. The proposal itself changed no product code;
the later authorized implementation is documented in [the implementation record](MEMORYREPO_IMPLEMENTATION.md).
