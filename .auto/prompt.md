# Autoresearch: Hermes goto-choice adoption

## Objective
Make this Rust fork the lowest-friction local-first memory provider for Hermes
and other personal-agent runtimes. Optimize the real adoption path: a clean
user downloads a release, configures Hermes, imports an existing Python
`mnemosyne-memory` SQLite database without losing data, and completes a local
remember/recall smoke test without a cloud key. Preserve compatibility with
existing dotted MCP names and do not overfit retrieval datasets.

The implementation sequence is documented in `docs/HERMES_GOTO_PLAN.md`.
Phase 1 is the current critical path: release distribution, Hermes-native MCP
aliases/command compatibility, and an idempotent importer. Phase 2 gates
enterprise features and rewrites docs. Phase 3 publishes a fair retrieval
comparison against the Python stack.

## Metrics
- **Primary**: `hermes_phase1_gates` (count, higher is better) — independent
  black-box Phase 1 gates passed by `scripts/test-hermes-adoption.sh`.
- **Secondary**: `hermes_phase1_failures`, binary size in MiB, and any retrieval
  quality metrics emitted by a separately run retrieval evaluation.
- **Quality guardrails**: the existing `.auto/eval_dev.jsonl`,
  `.auto/eval_heldout_a.jsonl`, and `.auto/eval_heldout_b.jsonl` are fixed
  datasets. Do not tune production code or labels to their questions.

## How to Run
`./.auto/measure.sh` builds the release binary and runs the public adoption
smoke tests. It emits structured `METRIC` lines. The smoke tests use only the
public CLI/MCP surface and never mutate the source database used for imports.

## Files in Scope
- `src/main.rs`, `src/cli/*` — public command aliases and import CLI.
- `src/mcp/*` — MCP tool schemas, aliases, and dispatch.
- `src/storage/*`, `migrations/*` — durable import/storage support.
- `scripts/install.sh`, `scripts/install/*`, `.github/workflows/release.yml` —
  release assets and installation.
- `scripts/test-hermes-adoption.sh` and `.auto/*` — measurement correctness.
- `README.md`, `docs/HERMES_INTEGRATION.md`,
  `docs/HERMES_GOTO_PLAN.md`, `examples/hermes/*` — audience-facing setup.
- `Cargo.toml` only when needed to make an explicit feature or compatibility
  change; avoid unrelated dependency churn.

## Off Limits
- Do not alter or delete fixed evaluation datasets or their labels.
- Do not add query strings, expected answers, dataset names, or benchmark IDs
  to production source code.
- Do not fake persona/canonical/triple parity: expose only behavior backed by
  tested semantics and document gaps honestly.
- Do not mutate or rewrite a source SQLite database during import.
- Do not add network calls to the adoption smoke score.
- Do not remove ICS/tree-sitter before measuring and documenting the default
  build impact; feature-gate first.

## Constraints
- `./.auto/checks.sh` must pass before a candidate is kept.
- Existing dotted MCP tools remain available to avoid breaking clients.
- MCP stdout must remain clean JSON-RPC; diagnostics go to stderr.
- Core memory operations must work without `ANTHROPIC_API_KEY` or a cloud LLM.
- Import must be deterministic and safe to rerun, report counts, and preserve
  enough source metadata to audit the conversion.
- Release artifacts must be checksummed and the installer must verify them.
- Prefer one coherent idea per iteration. Keep only real primary improvements;
  simpler equal-quality code wins.

## Honest evaluation protocol
1. The adoption smoke test is the primary metric; it is a behavior gate, not a
   benchmark score. Never improve it by weakening the test.
2. When retrieval or storage code changes, run the existing dev and both held-out
   evaluations separately and compare against the clean baseline recorded in the
   prior log (`dev_mrr` 0.4708; held-out average 0.4036 before the accepted graph
   fix, 0.6222 for the current retrieval candidate). Report generalization.
3. Run `./.auto/checks.sh` after a passing candidate. Revert candidates that
   regress public contracts, fail tests, mutate source data, or add unverified
   compatibility claims.
4. Record every experiment with the idea, metrics, decision, and durable lesson.

## What's Been Tried
- The repository at `mnemosyne-hermes` was already a clean clone of
  `https://github.com/juanmackie/mnemosyne-hermes`; `git pull --ff-only origin
  main` confirmed HEAD `e7476a9` was current before this branch was created.
- Existing retrieval autoresearch established a clean isolated harness and a
  graph-neighbor improvement. Those results remain historical quality context;
  this session optimizes adoption, not benchmark labels.
- Phase 0 baseline was `hermes_phase1_gates=1` with a 55.70 MiB release
  binary; release assets, installer, MCP aliases, importer, and Hermes docs
  were absent.
- Phase 1 now passes 12 independent gates: four-target release workflow,
  checksum-verifying installer, `mcp` command, dotted plus underscore aliases,
  persona/canonical/triples discovery and round-trip, idempotent Python SQLite
  import, keyless CLI memory, and the Hermes-first docs front door. Current
  release binary is ~55.84 MiB.
- Importer debugging found that libsql `Row` handles become an all-NULL view
  after cursor advancement; the importer now materializes `libsql::Value`
  values while iterating. It also had to resolve the target through the common
  `MNEMOSYNE_DB_PATH` helper so the write and verification stores match.
- A release integration-test link exceeded the per-iteration timeout, so the
  black-box public MCP round-trip is the fast backpressure gate; full
  integration validation is deferred to release CI.
- Next structurally different work is Phase 2 default feature-gating and
  binary-size/build-time measurement. Deferred ideas are in `.auto/ideas.md`.
