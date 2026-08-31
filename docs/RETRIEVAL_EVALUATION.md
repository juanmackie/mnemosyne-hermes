# Retrieval diagnostics and evaluation

Retrieval diagnostics are enabled by default. Each hybrid recall writes a local
`retrieval_traces` row containing candidate counts, effective fusion weights,
fallback reasons, result IDs, and a SHA-256 query hash. MCP and CLI JSON recall
responses also include the in-memory `explain_trace`. Set
`MNEMOSYNE_RECALL_DIAGNOSTICS=0` only when diagnostics must be disabled.

Queries are rewritten before FTS matching. Camel-case, dotted/version, and
letter/digit boundaries are split, and only a small explicit synonym list is
expanded. FTS terms persisted in diagnostics are hashed; raw query text is not
stored by the evaluation tables.

Use signals from `mnemosyne.used` harvest bounded golden items. The storage API
`run_retrieval_evaluation(max_samples)` evaluates the latest trace for each
namespace-scoped golden query and reports `precision_at_5` and
`phrasing_miss_rate`. Results are gated to once per configured interval
(default seven days). After the configured minimum sample count (default 20),
keyword/vector/graph weights are updated by a capped, normalized adjustment and
persisted in `retrieval_adaptive_weights`.

The following local settings live in `retrieval_evaluation_config`:

- `enabled`
- `diagnostics_enabled`
- `weekly_interval_seconds`
- `max_samples`
- `min_samples_for_adaptation`
- `fallback_alert_rate`

All evaluation data stays in the local database and no separate network calls
are made.
