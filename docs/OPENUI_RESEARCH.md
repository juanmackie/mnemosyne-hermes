# OpenUI review and proposed improvement plan

**Reviewed:** 2026-10-09 (Australia/Brisbane).  
**Status:** proposal only; no product implementation.  
**Review team:** three `gpt-6-luna` agents with high reasoning, covering the official website/documentation, upstream source, and local architecture.  
**OpenUI revision:** [`c4c0c90575facf824d3d8a64daa11947acc8a668`](https://github.com/thesysdev/openui/tree/c4c0c90575facf824d3d8a64daa11947acc8a668).  
**Local revision:** `5a1eb57cd844a2c58c5a5c5c330df04fffaca306`.

## Recommendation

Borrow OpenUI's schema-driven contracts, compact presentation, and diagnostic workflow. The best first implementation is accurate lite MCP output schemas and public contract coverage, followed by an opt-in bounded search/prefetch projection. Improve provider onboarding and diagnostic discoverability alongside that work. Keep direct OpenUI package adoption conditional on a concrete browser interface use case.

This recommendation follows the repository's product boundary: the engine-backed Hermes provider is the product; lite is a separate standard-library SQLite CLI/MCP surface. OpenUI addresses model-generated interfaces, rather than memory storage or retrieval. Its framework runtimes would add a new stack without improving our current provider interface by themselves ([OpenUI overview](https://www.openui.com/docs/openui-lang/overview), [local architecture](../README.md#the-real-architecture), [repository contract](../AGENTS.md)).

## Findings and evidence

| OpenUI pattern | What exists here | Useful transfer |
| --- | --- | --- |
| Component definitions feed prompt signatures and JSON schemas ([source](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/packages/lang-core/src/library.ts)). | Lite advertises strict input schemas and validates calls from the same table; it returns structured results without advertising output schemas ([tools](../src/mnemosyne_lite/tools.py#L13), [MCP](../src/mnemosyne_lite/mcp.py#L80)). | Add accurate output contracts, without another schema framework or tool registration. |
| Constrained composition and focused validation/round-trip tests ([validation tests](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/packages/lang-core/src/parser/__tests__/materialize.validation.test.ts), [serialization tests](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/packages/lang-core/src/parser/__tests__/serialize.test.ts)). | Lite rejects extra arguments and invalid types/ranges; MCP tests cover round trips, unknown tools, and missing/foreign stores ([dispatch](../src/mnemosyne_lite/tools.py#L76), [tests](../tests/test_lite_mcp.py#L44)). | Extend public boundary coverage for every tool, aliases, skip variants, and invalid writes. |
| Compact structured presentation ([language overview](https://www.openui.com/docs/openui-lang/overview)). | Lite already has cited IDs, namespaces, importance, snippets, and marked truncation; the card cap does not cap raw result content ([cards](../src/mnemosyne_lite/cards.py#L87), [results](../src/mnemosyne_lite/tools.py#L155)). | Bound an optional result projection, rather than inventing a memory DSL. |
| Diagnostics connect a failure to evidence ([reliability monitoring](https://www.openui.com/docs/reliability)). | Provider diagnostics already expose sanitized error classes, counts, size, truncation, timing, and cache behavior; the evaluation already produces HTML reports ([configuration](HERMES_CONFIGURATION.md#automatic-context-controls), [evaluation](../bench/HERMES_PREFETCH_RESULTS.md)). | Document symptom-to-command paths and reuse existing reports. |
| Separate introductory, build, and production paths ([documentation](https://www.openui.com/docs)); examples have explicit integration ownership ([examples](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/examples/README.md)). | README separates provider/lite and setup already has a runbook. | Add a small path selector and keep examples focused and independently runnable. |

The provider already applies much of the relevant discipline: four default tools with wildcard opt-in, configuration-aware prompt instructions, an 8,000-character aggregate prefetch ceiling, privacy-conscious diagnostics, and a synthetic quality/latency evaluation. These are shipped features, not proposed additions ([tool filtering](../integrations/hermes-provider/hermes_memory_provider/__init__.py#L1974), [prompt](../integrations/hermes-provider/hermes_memory_provider/__init__.py#L2391), [configuration](HERMES_CONFIGURATION.md#automatic-context-controls)). Generating more prompt instructions from schemas should be considered only if a drift audit finds a gap; tool definitions already consume model context.

### Measured local payload gap

A scratch check against the current checkout created ten synthetic memories in a temporary lite store. Each row contained a unique prefix and about 99,000 filler characters. It called the public MCP server with `mnemosyne_memory_search`, `max_results: 10`, and `max_chars: 500`.

| Measurement | Observed value |
| --- | ---: |
| Returned rows | 10 |
| Card `text` length | 7,282 characters |
| Serialized JSON text length | 999,943 characters |
| Complete JSON-RPC response | 2,000,365 UTF-8 bytes |
| Tool error | `false` |

The check used no live memories, model, or network service. It demonstrates a payload-size gap, not a measured tokenizer, latency, or cost benefit. Timestamps and IDs can make repeated byte counts differ slightly.

The cause is visible in code: `max_chars` caps card bodies, while `results` retain full rows. MCP serializes that complete result into both JSON text and `structuredContent` ([result assembly](../src/mnemosyne_lite/tools.py#L172), [transport](../src/mnemosyne_lite/mcp.py#L93)). That duplication is intentional compatibility behavior: MCP recommends serialized JSON text alongside structured content. Preserve both forms and apply any compact projection to the same object before serialization ([MCP structured content and output schemas](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#structured-content)).

### Upstream limits

OpenUI's reported token reduction does not establish a Mnemosyne improvement. Its seven-scenario benchmark generates one OpenUI sample per scenario, projects the parsed tree into comparison formats, counts saved outputs with the GPT-5 encoder, and estimates decode time at 60 tokens/second. It is a representation comparison, not measured memory recall quality or end-to-end latency ([methodology](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/benchmarks/README.md)).

Component constraints also do not replace application authorization. OpenUI's mutation dispatcher hands registered mutation tool names/arguments to its tool provider; the application must still validate and authorize them ([dispatcher](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/packages/lang-core/src/runtime/queryManager.ts)). Do not interpret a renderable component or action as permission to mutate memory.

## Proposed implementation sequence

### 0. Establish compatibility fixtures

**Scope:** public lite MCP requests and a small synthetic payload fixture. Document the existing full-response behavior, protocol variants, input constraints, aliases, and captured/skipped `sync_turn` shapes before changing output contracts. Record how prefetch fallback reports counts; do not introduce claims of exact totals that the current implementation cannot support.

**Paths:** `tests/test_lite_mcp.py`, `tests/test_lite_describe_cards.py`, `MCP_SERVER.md`; a focused measurement helper under `bench/` only if needed for reproducibility.

**Acceptance:** fixtures cover all four tools, both aliases, empty results, captured/skipped turns, storage failure, invalid required/type/range/extra arguments, and a large valid row. Invalid writes and skipped turns leave rows unchanged; foreign-store refusal stays byte-identical. Measure full wire bytes and card characters independently. Use independent expected outcomes, rather than treating generated schemas as the sole oracle.

**Effort/risk:** small; test/documentation work with no runtime change.

### 1. Advertise accurate lite output schemas

**Scope:** add per-tool output definitions beside the existing input definitions and advertise `outputSchema` where supported by the negotiated MCP protocol. Preserve all existing response fields, tool names, aliases, and error behavior. Cover both `sync_turn` success variants with a discriminated schema. Keep tool failures as `isError: true`; they must not masquerade as valid empty successes.

**Paths:** `src/mnemosyne_lite/tools.py`, `src/mnemosyne_lite/mcp.py`, the MCP tests and documentation. Prefer standard JSON Schema data and existing Python tools; no runtime dependency on OpenUI, Zod, or a general schema framework is needed.

**Acceptance:** every successful public response conforms to its advertised schema; historical protocol clients retain their compatible behavior. The JSON text decodes to the same object as `structuredContent`. Error cases remain clearly distinct. Unknown and optional existing row fields are handled honestly rather than removed merely to fit a schema. The four tools remain the only advertised lite tools.

**Effort/risk:** small to medium; primarily client compatibility. MCP requires results to conform once an output schema is advertised, so all variants must be characterized first.

### 2. Add an opt-in compact retrieval projection

**Scope:** extend the existing search/prefetch input contract with an explicit compact mode and aggregate budget. Exact argument names and budget default should be chosen after phase 0 measurements. Keep full-fidelity output as the default. Preserve IDs, namespace, importance, ranked order, useful excerpts, and explicit truncation/omission metadata in compact mode. Do not replace JSON with OpenUI Lang.

Apply the projection identically to JSON text and `structuredContent`. The aggregate limit must include metadata, snippets, card text, JSON escaping, both transport copies, and the envelope. Reserve overhead or omit additional rows deterministically when necessary; a per-row character cap alone is insufficient. Bound context/namespace fields too. Reject an unusably small budget clearly.

**Paths:** lite `tools.py`, `cards.py`, `mcp.py`, MCP/card regressions, `MCP_SERVER.md`, and client examples. Extend the phase 1 output schemas to describe compact variants. Keep storage schema v3 unchanged.

**Acceptance:** a fixture of many maximum-size memories meets the documented wire-size ceiling in compact mode, including non-ASCII and heavily escaped content. Default responses keep their full content and existing shape. Both transport representations agree. Missing stores, corrupt stores, and input failures still fail loudly. IDs and snippets remain tied to the returned rows; no fabricated matches.

Lite has no shipped get-by-ID MCP tool. Document CLI full JSON recall as the full-fidelity path available today; do not promise a clickable full-detail action or silently add a fifth tool. Provider inspection/export proposals in the existing `MEMORYREPO_RESEARCH.md` are separate, unimplemented work.

**Effort/risk:** medium; output semantics and clipping must be explicit. No token-saving percentage should be promised until measured on the target client. A client may still include both representations in model context.

### 3. Improve provider onboarding and diagnostic navigation

**Scope:** add a concise README entry selector for Hermes provider, standalone lite, and contributors. Connect common symptoms to existing evidence: install/activation failures → `hermes mnemosyne doctor --no-fix` and `hermes memory status`; oversized or missing automatic context → existing configuration/opt-in recall diagnostics; synthetic behavior comparisons → existing evaluation reports. Explain that lite `max_chars` currently caps cards, not full JSON rows.

**Paths:** `README.md`, `docs/AGENT_SETUP.md`, `docs/HERMES_CONFIGURATION.md`, `TROUBLESHOOTING.md`, `MCP_SERVER.md`, and `examples/README.md` if its catalog needs refinement.

**Acceptance:** each path has one clear install, verify, and next-step sequence; example prerequisites distinguish provider versus lite and run without a new cloud key. Preserve the canonical Hermes tool table and existing troubleshooting details. Documentation uses aggregate/sanitized diagnostics rather than encouraging users to publish memory contents.

**Effort/risk:** small; useful provider-facing improvement, suitable to ship independently of phases 1–2.

### 4. Optional interface experiment, only after selecting a use case

A read-only memory inspector could help humans browse cited results or understand failures. Direct OpenUI adoption becomes relevant if an existing Hermes/browser client needs dynamic component composition. Define that client and the task first; a static report or deterministic card interface may already suffice.

If pursued, start with a developer example against sanitized fixtures: a small allowlisted component library for memory cards, counts, and diagnostic states. Keep data access behind the existing supported interfaces and preserve their visibility rules. Rendering must treat memory text as untrusted data. Start without mutations, generated executable HTML/JavaScript, cloud telemetry, or an additional model key. OpenUI is MIT licensed; preserve notices if code is reused ([license](https://github.com/thesysdev/openui/blob/c4c0c90575facf824d3d8a64daa11947acc8a668/LICENSE)).

**Acceptance:** demonstrate a specific task benefit over the existing CLI/report, then establish a browser harness before offering a supported UI. Do not read engine SQLite tables directly or register a second Hermes provider. A separate supported UI would require a deliberate product-scope decision; it is not included in the recommended first implementation.

**Effort/risk:** large/unknown relative to the current Python surfaces; deferred.

## Verification and boundaries

For approved runtime work, run affected lite/MCP/card/storage regressions, `./test-all.sh --skip-llm`, `bash scripts/checks.sh`, and the required pre-commit checks. Add any new gate to the `scripts/checks.sh` registry. Avoid changing provider internals for the first slice; if a later change touches the vendored snapshot, include its `# LOCAL PATCH:` marker, `PATCHES.md` entry, and manifest hashes/bytes/lines, then run the provider gates and strict engine lane.

Preserve provider id/registration, four default Hermes tools, all lite names/aliases/skip semantics, engine pin, DB precedence, schema v3, storage refusal ordering, per-thread connections/cache, aligned CLI tables, JSON compatibility, and provider/lite installation separation. Keep memory keyless and optional LLM work inherited through Hermes' own proxy.

Do not adopt the OpenUI cloud gateway, hosted analytics, scaffolded model credentials, its DSL/parser/streaming protocol, or a new UI runtime as a prerequisite for memory. Those additions are outside the demonstrated gap. No retrieval accuracy, production latency, tokenizer cost, live gateway behavior, or UI usability improvement was established by this review.

**Recommended first scope:** phase 0 plus phase 1, with phase 3 documentation improvements. Phase 2 follows once those contracts and baseline measurements are in place. This document is the reviewable proposal; implementation has not begun.
