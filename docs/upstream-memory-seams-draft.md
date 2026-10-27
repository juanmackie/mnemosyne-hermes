# Draft: narrow model seams for Mnemosyne

**Status:** local discussion draft only. Do not send without owner approval.

## Problem

Mnemosyne currently has no stable extension points for optional recall ranking, answerability, capture filtering, or fact resolution. Downstream providers either carry engine patches or duplicate orchestration. The goal is to let the engine own lifecycle and storage while optional, versioned components provide these decisions.

## Proposed interfaces

- `recall_rerankers`: reorder an existing bounded candidate list; must not perform storage writes.
- `recall_verdict`: return confidence band, `answerable`, `requires_review`, and a machine-readable reason. `NONE` remains distinct from storage failure.
- `capture_filters`: inspect candidate text before any write and return allow/redact/refuse with a reason. Raw rejected text must not be persisted.
- `write_resolvers`: compare a proposed fact with existing facts and return a relation label; deterministic temporal, provenance, and subject guards remain engine-owned.
- `model_registry`: resolve a configured artifact by id and compatibility key, validate its digest, and load lazily. Missing configuration disables the feature; a configured digest mismatch fails loudly.

## Required invariants

1. Defaults preserve current behavior until a caller opts in.
2. Components receive typed domain values, not database connections or provider globals.
3. Storage errors propagate and never become empty results.
4. The engine remains responsible for authorization, transaction order, persistence, and hard safety constraints.
5. Lite remains stdlib-only; the seam must not impose a model runtime dependency on it.
6. Every seam has a deterministic no-op implementation and contract tests, including malformed output and exceptions.

## Questions for upstream

- Where should registry configuration live, and what compatibility key is stable across patch releases?
- Should verdicts and capture decisions use shared enums in the engine package?
- Can hooks be synchronous and bounded, with explicit per-hook time budgets?
- Which interfaces are public API versus experimental until one release cycle proves compatibility?

## Local prototype boundary

Phase 0 does not alter the provider, engine, runtime behavior, or public API. It only records this proposal. No upstream issue or pull request has been submitted.
