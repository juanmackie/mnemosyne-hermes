# Autoresearch: Hermes adapter + retrieval optimization

## Objective
Optimize the `mnemosyne-rust` adapter (reconstructed pure-Python adapter) and retrieval path for first-class Hermes integration. Focus on adapter contract reliability, retrieval quality, and graceful degradation without reintroducing Rust.

## Metrics
- **Primary**: `realquery_heldout_mrr` (unitless, higher is better) — retrieval ranking quality.
- **Secondary**: adapter test pass rate, build/install time, contract verification pass/fail.

## How to Run
`.auto/measure.sh` runs `.auto/evaluate_membench.py` (or `.auto/evaluate.py`) against the adapter/retrieval pipeline.

## Files in Scope
- `integrations/hermes-memory-provider/` (adapter source, tests)
- `docs/HERMES_INTEGRATION.md`, `README.md`
- `.auto/` session files only (no external DB changes)

## Off Limits
- No Rust source changes (`src/` retired).
- No new dependencies (adapter is stdlib-only).
- Do not modify `.auto/evaluate_membench.py` or `.auto/evaluate.py` contracts.

## Constraints
- Adapter contracts (`provider_id`, `namespace`, `DB`, `checkpoint`) must be preserved.
- No `maturin` / `PyO3` reintroduction.
- Tests should pass or degrade gracefully.

## What's Been Tried
- Adapter source reconstructed from `docs/plans/item_02_python_baseline.md` contracts.
- Pure Python adapter installed (`pip install -e integrations/hermes-memory-provider/`).
- Entry point verified (`mnemosyne-rust`).
- Some adapter tests still fail due to environment-level module caching (installed editable vs in-repo import). Needs resolution.
