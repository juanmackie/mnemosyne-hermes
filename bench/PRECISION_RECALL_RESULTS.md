# PrecisionMemBench recall-cutoff replay

This report records an isolated, keyless BEAM characterization against the public PrecisionMemBench single-turn fixtures. It does not reproduce the reported deployment run: the supplied finding describes the deployed Mnemosyne MCP/stdio retrieval adapter with MiniLM embeddings enabled, while this scratch run called `BeamMemory.recall()` directly with embeddings disabled. The run is useful for checking cutoff behavior and its recall cost, not for choosing or advertising a production default.

## Reproduction inputs

The fixture source is [`tenurehq/precisionMemBench`](https://github.com/tenurehq/precisionMemBench), checked out at commit `61a712a1754e7d27b6dee42da32a2c214b003ece`. Inputs used were `fixtures/retrieval.cases.json` (SHA-256 `6ab5a626356f4a6e947be8e2e9a95af9ead7cfd58237e4aae6f02ae490db0ba`) and `fixtures/beliefs.seed.json` (SHA-256 `d6ae1c270faba0e8bf2bc0eb4527cd008d481c451a9cd557b5ab85f918fe33f3`).

The benchmark README defines 77 single-turn cases: 43 active retrieval, 25 structural, and 9 trivially empty. The supplied finding's baseline of 32/77 total passes and 12/43 active passes means 45 total failures but 31 active-case failures. Its phrase "45 active-case failures" conflates the total and active denominators. The "41/45 over-return" count may describe assertion failures, but it cannot be a count of distinct failing active cases under those denominators.

From a PowerShell checkout of the pinned fixture repo, the scratch replay command is:

```powershell
$suite = 'C:\path\to\precisionMemBench'
$repo = 'C:\path\to\mnemosyne-hermes'
$python = 'C:\path\to\scratch-venv\Scripts\python.exe'
$env:PYTHONPATH = "$repo\integrations\hermes-provider"
& $python "$repo\bench\precision_recall_eval.py" `
  --cases "$suite\fixtures\retrieval.cases.json" `
  --beliefs "$suite\fixtures\beliefs.seed.json" `
  --engine --cutoffs 0,0.25,0.5,0.6,0.7,1 `
  --output "$env:TEMP\mnemosyne-precision-eval-report.json"
```

The runner creates a fresh temporary database and homes directory for each invocation. It clears per-row recall counters before each cutoff, fixes seeded timestamps, disables embeddings and background consolidation, and does not read or write a live Hermes database. It imports the pinned engine from the scratch environment and the provider adapter for initialization, then calls BEAM directly; it does not test MCP serialization or provider-side retrieval parity.

## Observed results

Measured on 2026-10-13 with `mnemosyne-memory` 3.15.1 and the audited engine
patches in this branch. The final patched `core/beam.py` SHA-256 is
`4ef04dc556cf017b3ba16d066db8c6bcd40bb82d975fc93b1ffa5bc98561973d`.
The final sweep ran twice and produced identical metrics. Those repetitions
check this fixed fixture's determinism; they are not independent evidence of
performance or generalization.

The local collector concatenates canonical names, aliases, content and
`why_it_matters` into each stored memory and uses belief confidence as engine
importance. It synthesizes pinned facts, open-question lists and persona text
from the fixtures. Consequently, structural passes do not measure engine
capabilities. Domain filters and relation edges are not implemented in this
collector, and its behavior is not evidence about the original adapter.

Each variant, including cutoff `0`, is scored from the actual engine rows returned with that cutoff enabled and requests each case's `maxBeliefs` from BEAM. For the separate plain-score replay diagnostic, the runner records up to all 35 seeded candidates before applying benchmark user/pinned filtering and result capacity; this prevents foreign-user or pinned rows from being hidden when calculating a threshold. The report's `engineCutoffCheck` compares that offline replay with BEAM's actual gated/top-k output and is not used as a substitute for the actual variant rows.

| Relative score floor | Total passes | Active passes | Mean active precision | Mean active recall | Missing expected beliefs |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.00 | 32/77 | 12/43 | 0.501 | 0.826 | 8 |
| 0.25 | 32/77 | 12/43 | 0.504 | 0.826 | 8 |
| 0.50 | 33/77 | 12/43 | 0.519 | 0.802 | 10 |
| 0.60 | 38/77 | 17/43 | 0.624 | 0.776 | 15 |
| 0.70 | 39/77 | 18/43 | 0.649 | 0.700 | 23 |
| 1.00 | 38/77 | 17/43 | 0.605 | 0.476 | 41 |

In this keyword-only run, raising the ratio improved exact passes through 0.70 while increasing missing required beliefs from 8 at 0 to 23. That tradeoff is visible and reproducible, but it does not establish the best threshold for the deployed MiniLM/MCP path. The 0.70 value is not recommended as a default. The score floor remains opt-in and disabled by default pending the complete deployed-path retune.

The runner also accepts previously recorded raw ranked rows (`--rankings`) so candidate cutoffs can be replayed without rerunning retrieval. Each case row must contain every seeded candidate that can influence the maximum-score threshold, including foreign-user and pinned rows. The scorer applies the score ratio before the benchmark adapter's synthesized user/pinned filtering and output capacity. It validates exact case-ID parity and reports per-case expected, missing, removed, and extraneous IDs.

## Findings disposition

| Item | Disposition |
| --- | --- |
| P0 relevance floor | Implemented as the opt-in relative score floor `MNEMOSYNE_RECALL_SCORE_FLOOR`, default `0`. This replay measures recall loss, but full deployed MiniLM/MCP score retuning remains pending. |
| P1 scope-aware matching | The public cases use domain tags, while engine `scope` controls session/global visibility. The domain filtering and keyword-only reproductions are documented in [PRECISION_SCOPE_ANALYSIS.md](PRECISION_SCOPE_ANALYSIS.md); no vector-winner authorization rule is proposed. |
| P2 long-query normalization | A long-query keyword-only case is reproduced in [PRECISION_SCOPE_ANALYSIS.md](PRECISION_SCOPE_ANALYSIS.md). The observed behavior points to candidate admission and does not yet support a separate query-length normalization change. |
| P3 watch list | Small-sample observations and limits are recorded in [PRECISION_SCOPE_ANALYSIS.md](PRECISION_SCOPE_ANALYSIS.md); aggregate claims are not attributed to the engine without original adapter traces. |
| P4 Qdrant `filters` signature | Verified against current upstream source; a review-ready draft is in [UPSTREAM_PRECISION_BENCHMARK_ISSUE.md](../docs/UPSTREAM_PRECISION_BENCHMARK_ISSUE.md). |

The synthetic/isolated scores in this report must not be presented as the deployed benchmark score or as a production improvement. The original adapter and its case-level result artifact were not available in the public fixture checkout.

## Repository verification

Final verification on 2026-10-13 used a temporary copy of the pinned engine:

- `test-all.sh --skip-llm --require-engine`: provider contract gates passed and
  all 296 pytest tests passed.
- `scripts/checks.sh`: all registered gates passed, including the new precision,
  query-noise and scorer checks.
- `pre-commit run --all-files`: Ruff, formatting, mypy and ShellCheck passed.
  A separate Ruff and mypy run included the new untracked Python files.
- `scripts/smoke-hermes-onboarding.sh`: skipped on Windows because it requires
  real symlinks. No live gateway acceptance is claimed.

The pre-existing source-reference assertion was updated to include the internal
message-ID namespace required by P32. Existing formatting and lint failures in
the provider evaluation test and tracked task scorer were also corrected.
Installed Hermes sources, configuration and live stores were not modified.
