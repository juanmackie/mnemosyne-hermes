# Memory stack research: public brains and safe adoptions

_Checked 2026-08-28. Sources are primary repositories or first-party product documentation wherever available._

## Repository / product inventory

| Item | Public source found | What is actually public | License / copy boundary |
|---|---|---|---|
| GBrain | [garrytan/gbrain](https://github.com/garrytan/gbrain) | Markdown pages as the durable plane, PGLite/Postgres derived index, signal detection, entity links, citations, scheduled dream/doctor jobs, MCP/CLI | MIT. Preserve notice for copied code or substantial text. |
| mem0 | [mem0ai/mem0](https://github.com/mem0ai/mem0) | `add`/`search` memory API, extraction and dedup/conflict paths, entity links, hybrid retrieval; platform decay is separately documented | Apache-2.0. Preserve license/NOTICE and mark copied modifications. |
| Letta | [letta-ai/letta-code](https://github.com/letta-ai/letta-code) (current source); [letta-ai/letta](https://github.com/letta-ai/letta) is now a landing/archive repo | Always-visible memory blocks, searchable archival memory, Git-backed MemFS, explicit memory tools, asynchronous sleep-time/reflection agent | Apache-2.0 for the source. Docs, trademarks, and hosted service are not code grants. |
| Zep / Graphiti | [getzep/graphiti](https://github.com/getzep/graphiti) | Temporal entity edges, validity windows, episodes/provenance, BM25/vector/BFS/RRF search recipes | Apache-2.0. Preserve notices and patent terms. Zep's hosted engine is proprietary. |
| Sylph | [getnao/sylph](https://github.com/getnao/sylph) | Git/Markdown company brain, `AGENTS.md`/`CONTEXT.md`, domain folders, skills, draft→review→publish→insights loop | README claims MIT; verify the exact commit's notices before copying because the fetched root LICENSE was not found. |
| DIY Claude Code + git | No single canonical repository | Markdown instructions, grep/search, pull requests and human review as the authority boundary | Reimplement the pattern; do not copy an arbitrary team's private prompts/data. |
| Pletor Brain | [Pletor plugin](https://github.com/pletor-ai/claude-code-pletor-plugin), not a Brain implementation | Public product docs describe brand rules, assets, proven content, and agent context; Brain internals are not public | Plugin is Apache-2.0; Brain data/service and trademarks remain proprietary. |
| Gorgias Cortex | No verified public Cortex repository; see [Gorgias org](https://github.com/gorgias) and [engineering context-layer article](https://medium.com/gorgias-engineering/building-a-context-layer-from-the-ground-up-d6f72713915a) | Public company/engineering descriptions of an internal context layer and connected tools | Private implementation; do not copy or treat claims as source evidence. |
| Slite Agent | No verified official implementation repository | First-party docs describe read-only connectors, drift detection, owner-routed diffs, Triage, Accept/Dismiss, permissions, and Activity Log | Proprietary product behavior; reimplement the workflow, not code or private data. |

The phrase “9+ company brains” therefore does **not** mean nine public codebases. GBrain, mem0, Letta Code, Graphiti, and Sylph are inspectable repositories. Pletor has a public integration plugin, while Pletor Brain, Gorgias Cortex, and Slite Agent are documented products/internal systems without verified public implementations.

## Patterns worth adopting

### Getting signals

- **GBrain:** every substantive inbound message can produce an asynchronous signal; search before create, preserve exact user wording where valuable, add entity backlinks, cite sources, and make capture observable.
- **mem0:** distinguish explicit storage (`infer=false`) from model extraction; do not pretend exact-text deduplication handles semantic contradiction. Extraction should carry observation time, source messages, and links to existing IDs.
- **Slite:** source connectors should be read-only and emit change events rather than silently mutating canonical knowledge.

### Remembering and retrieving

- **Letta:** keep a small always-visible hot layer separate from searchable archival memory and transient working state.
- **Graphiti:** preserve source episodes and temporal validity; use an append/expire model for changing facts instead of deleting the old fact. Its search recipes combine lexical, vector, graph proximity, and reranking signals.
- **mem0:** recent access can be a bounded search-time boost. Decay is not deletion/expiry; candidate overfetch is needed before reranking.
- **GBrain/Sylph:** human-readable files, explicit citations, domain scope, and role/owner boundaries make memory reviewable rather than opaque.

### Dreaming and pruning

- **GBrain:** nightly bounded jobs repair citations/links, enrich entities, consolidate truth, refresh embeddings, and surface stale pages. Jobs have retries, limits, and observable results.
- **Letta:** reflection is an explicit asynchronous worker triggered by activity/compaction, with state tracking and tests for successful completion; it must not block the interactive path.
- **Sylph:** self-improvement means diffing the generated draft against the human-approved output and promoting recurring lessons into versioned rules/examples—not silently rewriting policy.

### Speaking and searching safely

- **Slite:** produce a targeted diff, route it to an owner, and require Accept/Dismiss. Preserve source permissions and an activity log.
- **GBrain:** synthesis should cite every claim and say what the brain does not know. Retrieval quality is improved by graph/entity traversal, but search results still need scope and provenance.
- **Letta/Mnemosyne:** internal response guidance must be clearly separated from factual evidence and never be quoted as a user fact.

## What Mnemosyne already has

The current uncommitted implementation in this checkout already covers the strongest four text-memory seams:

1. Strict one-call turn extraction with bounded typed candidates, entities, confidence, source role, and verbatim evidence.
2. Evidence-backed `InteractionPolicy` memories kept out of factual recall and rendered in a separate internal-guidance channel.
3. Indexed entity anchors merged as a union signal into LibSQL keyword/vector/graph retrieval.
4. Bounded dual-channel context rendering under the existing shared token assembler, with abstention metadata and fenced-output scrubbing.

It also already has existing supersession/as-of retrieval, audit trails, archival/purge, graph traversal, hierarchy, and evolution infrastructure. This phase adds the bounded maintenance and durable proposal contracts in [`MEMORY_MAINTENANCE_APPROVALS.md`](MEMORY_MAINTENANCE_APPROVALS.md) without adding a second graph database, a duplicate memory taxonomy, audio dependencies, or a bulk copy of another project's prompts.

## Implemented follow-up hardening

The next safe improvements from the comparison are intentionally small and local:

- make namespace-scoped metadata-keyed turn learning idempotent so retrying a failed extraction does not create duplicate raw turns or derived memories;
- use bounded last-access recency in hybrid ranking, while keeping expiry/archival separate from search bias;
- make malformed policy rows fail loudly instead of silently disappearing from recall;
- match multi-word policy anchors on token boundaries, avoiding substring false positives such as `go` matching `golf`;
- preserve merged policy anchors and keep them indexed;
- make purge atomic across audit/provenance/entity/embedding metadata;
- keep read-only legacy projections working and restore StandardSQLite embedding storage, updates, vector recall, and consolidation candidates;
- make purge atomic and complete even when foreign-key enforcement is disabled, with accurate inline-embedding reporting;
- serialize metadata-keyed extraction claims across manager instances with owner-token fencing and durable empty-result completion, and bound CLI graph traversal depth;
- preserve graph traversal depth in ranking;
- add focused tests for each contract;
- persist bounded maintenance reports and owner-routed memory proposals, with explicit Accept/Dismiss/Apply transitions and base-revision conflict protection.

These are clean-room implementations of behavior, not copied source. The existing SQL supersession model remains the historical projection; Graphiti's bitemporal edge schema is a future extension only if fact-level interval queries become a separately approved requirement.

## Priority matrix

| Priority | Adoption | Why |
|---|---|---|
| P0 | Idempotent turn/retry identity and semantic conflict tests | Prevents the most damaging failure mode: duplicate or contradictory learned state after retries. |
| P0 | Provenance + human approval boundary | Already partly implemented; keep source quote, actor, scope, and audit history authoritative. |
| P1 | Access-aware bounded recency and score explanations | Mem0's useful ranking idea without conflating decay with deletion. |
| P1 | Maintenance protocol: stale/link/citation reports and bounded async jobs | Brings GBrain/Letta's “dream” discipline to existing evolution workers. |
| P1 | Proposal/diff/owner workflow for shared truth | Safely borrows Slite/Sylph without assuming their private internals. |
| P2 | Rich bitemporal fact intervals | Add only if current `created_at` + supersession/as-of behavior cannot satisfy real queries; avoid a schema expansion by default. |

## Licensing rule

Use repository architecture and public behavior as design input. When code or substantial expressive Markdown is copied, preserve the source license, copyright, NOTICE files, and modification markings; keep third-party Apache-2.0 material distinct from Mnemosyne's MIT code. Never copy private company data, credentials, branding, or claims as if independently verified.
