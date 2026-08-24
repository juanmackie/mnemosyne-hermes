# Mnemosyne Hermes Goto-Choice Plan

## Goal

Make the Rust fork the lowest-friction, local-first memory provider for Hermes
and other personal-agent runtimes, without sacrificing the public MCP contract
or retrieval quality. A random Hermes user should be able to install a release,
select the provider, keep existing memories, and verify the first recall without
compiling Rust or configuring a cloud API.

## Review of the Hermes-agent comments

| Comment | Finding at `e7476a9` | Decision |
|---|---|---|
| Distribution is the blocker | Confirmed. The installer builds from source and there is no release workflow or published binary surface. | Phase 1 release artifacts, checksums, and a curl installer. Keep source install as an explicit fallback. |
| MCP is not Hermes-native | Partly confirmed. The server is JSON-RPC/MCP-compatible, but exposes dotted names such as `mnemosyne.remember`, documents `serve`, and does not expose the underscore names used by Hermes provider tools. | Phase 1 add a compatibility surface with `mnemosyne_remember`/`mnemosyne_recall` and a `mcp` command alias. Preserve dotted names for existing clients. Add truthful equivalents for the remaining provider surfaces as they land. |
| No migration path | Confirmed. There is no `import` command for the Python `mnemosyne-memory` SQLite layout. | Phase 1 add an idempotent, read-only-source importer for working/episodic/legacy memories plus structured facts/triples where present. Never mutate the source database. |
| Enterprise features are always present | Confirmed in dependency structure. ICS/TUI, orchestration, Iroh/Ractor, and RPC dependencies are not all behind default-off feature gates. | Phase 2 feature-gate or split binaries. Do not delete ICS before measuring build size and confirming a migration path. |
| Recall quality is unproven | Partly confirmed. The fork has a fixed dev/held-out retrieval harness, but no published comparison against the Python Hermes provider on the same workloads. | Phase 3 run a documented, held-out comparison. Retrieval labels and production code remain benchmark-independent. |
| Docs target the wrong audience | Confirmed. `docs/HERMES_INTEGRATION.md` exists but still leads with `cargo build` and does not describe `hermes config set memory.provider`. | Phase 2 make it the canonical front door after the install/import path is real. |
| Cut ICS/tree-sitter now | Not yet justified. It may be maintenance drag, but deleting 7,500 lines before measuring default-build cost creates avoidable compatibility risk. | First gate it and publish a minimal default; consider removal only after usage and build-size evidence. |

## Phases and exit criteria

### Phase 0 — Baseline and guardrails

- Pin the upstream fork at a clean branch and record the review above.
- Add a repeatable adoption smoke harness that exercises the public binary,
  MCP discovery, offline operation, migration, and release/documentation
  surfaces. It must not alter benchmark labels or call a remote model.
- Keep the existing retrieval harness and held-out sets as a quality guardrail.

### Phase 1 — Works (critical path)

1. **Release distribution**
   - GitHub Release workflow for Linux x86_64, Linux aarch64, macOS x86_64,
     and macOS arm64.
   - Versioned archives, SHA-256 checksums, and a curl installer with explicit
     version/platform overrides and a source-build escape hatch.
   - Smoke-test the installed binary and make the release asset naming contract
     part of CI.
2. **Hermes-native MCP compatibility**
   - Accept `mnemosyne mcp` as well as the existing `mnemosyne serve`.
   - Advertise and dispatch underscore aliases (`mnemosyne_remember`,
     `mnemosyne_recall`, `mnemosyne_forget`, `mnemosyne_stats`, and related
     operations) while retaining dotted names for compatibility.
   - Document the exact `~/.hermes/config.yaml` provider/MCP configuration and
     keep stdout valid JSON-RPC with logs on stderr.
3. **Import and migration**
   - `mnemosyne import --from <path> [--namespace ...] [--dry-run]`.
   - Read Python `mnemosyne-memory` tables defensively by table/column presence,
     including `working_memory`, `episodic_memory`, legacy `memories`, facts,
     canonical facts, and triples where available.
   - Use deterministic target IDs and source identifiers so reruns do not
     duplicate memories; preserve source metadata and report counts/skips.
   - Open the source read-only and never delete or rewrite it.

**Phase 1 gate:** a clean machine with a downloaded binary can follow the
Hermes-first guide, configure Hermes, import a fixture SQLite database, call the
native remember/recall tools without a cloud key, and verify the imported fact.

### Phase 2 — Adoptable

- Make the minimal memory daemon the default feature set; move RPC, P2P,
  orchestration, dashboard/TUI, and ICS behind opt-in features or binaries.
- Remove build-time dependencies from the default path where possible and publish
  binary size/build time as secondary metrics.
- Rewrite README and `docs/HERMES_INTEGRATION.md` around install → configure →
  import → verify, then add concise guides for other MCP clients.
- Add compatibility tests for provider config, tool schemas, importer fixtures,
  corrupted/partial source databases, and offline/keyless behavior.

### Phase 3 — Goto choice

- Publish a reproducible comparison against the Python Hermes stack using the
  same corpus, queries, embedding settings, and hardware notes.
- Report Hit@1/Hit@5/MRR, abstention behavior, import fidelity, cold/warm latency,
  memory footprint, and binary size. Keep dev/held-out splits separate and do
  not tune production code to held-out questions.
- Add persona/canonical/triples APIs only with explicit semantics and migration
  tests; do not claim feature parity from a name-only adapter.
- Collect real-user install failures and prioritize fixes by time-to-first-recall.

## Autoresearch acceptance policy

The primary metric is the number of independent Phase 1 adoption gates passed by
`./.auto/measure.sh`, not a benchmark score. Retrieval `dev_mrr` and both held-out
MRRs remain secondary guardrails when retrieval code changes. A candidate is kept
only when it improves the primary metric, passes correctness checks, and does not
regress the existing public CLI/MCP behavior.
