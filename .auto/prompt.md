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
- **Primary**: `recall_heldout_mrr` (unitless, higher is better) — mean flat
  MRR across the two fixed held-out retrieval sets.
- **Secondary**: `recall_dev_mrr`, held-out Hit@5, hierarchical held-out MRR,
  p95 recall latency, `hermes_phase1_gates`, and binary size in MiB.
- **Current clean candidate**: held-out flat MRR 0.860648, dev MRR 0.871491,
  held-out Hit@5 0.972222, hierarchical held-out MRR 0.832870, and 39 adoption
  gates. These are measurements, not labels to optimize against.
- **Quality guardrails**: the existing `.auto/eval_dev.jsonl`,
  `.auto/eval_heldout_a.jsonl`, and `.auto/eval_heldout_b.jsonl` are fixed
  datasets. Do not tune production code or labels to their questions.

## How to Run
`./.auto/measure.sh` builds the release binary, runs the public adoption smoke
suite, rebuilds the fixed corpus through the public CLI, and evaluates flat and
hierarchical recall on dev plus both held-out sets. It emits structured
`METRIC` lines. The smoke tests and evaluator never mutate the source datasets
or their relevance labels.

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
1. Held-out flat MRR is the optimization target; the adoption smoke test remains
   a hard compatibility gate. Never improve either by weakening a test.
2. When retrieval or storage code changes, run dev and both held-out evaluations
   through `./.auto/measure.sh`, compare generalization, and monitor Hit@5,
   hierarchical MRR, and latency. Do not add query-specific rules.
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
- The recall segment reinitialized the primary metric to held-out flat MRR.
  BM25-ranked FTS5 results replaced rowid-order keyword results, and the hybrid
  scorer now preserves normalized keyword relevance. This raised held-out MRR
  from 0.271898 to 0.662037.
- Deterministic hash vectors are now excluded from offline CLI ranking because
  they are API-compatible but not semantic; model-backed local and remote
  vector paths remain enabled. Held-out MRR rose to 0.815278 and p95 latency
  improved.
- Generic function-word filtering before FTS5 improved held-out MRR to
  0.860648 and dev MRR to 0.871491. Negations and temporal qualifiers remain
  searchable; all-stopword queries fall back to their original terms.
- The default release remains keyless/local-first and all 39 adoption gates
  pass. Do not trade away that contract or add fixed-dataset query strings.
- Deferred ideas are in `.auto/ideas.md`; feature-gating work is historical
  context, not the current recall target.
