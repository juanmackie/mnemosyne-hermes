# Metrics and grading

## Decision this eval supports

Measure **warm repeated-query recall latency** for `PythonMemoryStorage.recall()` on a fixed, synthetic 3,000-row corpus, while preserving retrieval correctness. This follows `bench/README.md`'s existing latency scope. It does not measure cold FTS query latency, end-to-end MCP/Hermes latency, semantic relevance, or production traffic.

## Metrics

- `latency_ms` (report metric, lower is better): elapsed wall time for one `recall()` call after the same query has been warmed ten times. Rows contain one observation per `(case, rep)`; the report aggregates per-case means. `summary.json` also records pooled p50/p95/p99 and per-case p50/p99.
- `median_trial_p50_ms` (decision metric, lower is better): median of the workload-wide p50 values from five independent Python processes. `summary.json` records each run's p50 and their spread. Use this for the accepted 20% action bar.
- `correct` (hard guardrail, binary): programmatic checks for expected result count, requested content tokens, namespace, and minimum importance. A case passes only if every check passes. Each result row records this as a top-level field; `summary.json` contains the aggregate count and rate. No answer style or ranking preference is graded.

The corpus and queries are deterministic; model and token usage are unavailable and omitted. Timing observations are host- and process-specific. Reps characterize repeated-call timing noise, not independent production cases. Treat the six query shapes as a fixed workload, not a random sample of user traffic.

## Ground truth and coverage

The six queries and workload shapes are retained from `bench/measure.sh`. The expected counts for planted positive and negative cases come from its seed-42 fixture generation and its existing assertions (7 xylophone hits, 2 phrase hits, 0 miss hits, 10 filtered common-term results, 50 wide results). The importance-filter case checks predicates rather than a hard-coded random count. Ground truth is fixture-derived and enforced by the runner; the application receives only each query and filters.

This suite has two common-term cases, one rare-term case, one phrase case, one negative case, and one filtered case. It has no real user queries. The runner performs no retries; any exception is recorded in `errors.jsonl` and is not silently converted to a quality failure.

## Baseline and no-change controls

All four independent invocations used the same source, six cases, and 60 reps per case. The headline p50 values below were recomputed from each raw `results.jsonl`; every one of the 1,440 rows passed the correctness checks.

| Invocation | Correct | Warm p50 (ms) | Warm p99 (ms) |
| --- | ---: | ---: | ---: |
| baseline | 360/360 | 0.03030 | 0.49631 |
| no-change control 1 | 360/360 | 0.03055 | 0.65485 |
| no-change control 2 | 360/360 | 0.02380 | 0.28698 |
| no-change control 3 | 360/360 | 0.02865 | 0.43526 |

The baseline-to-control p50 shifts were +0.8%, -21.5%, and -5.4%. Across these four runs, the sample standard deviation of run p50 was 0.00313 ms; a rough normal 95% single-run noise band is ±21.7% of the mean. This estimate is based on only four process runs, but a no-change run already crossed the accepted 20% action bar. Therefore this setup cannot reliably distinguish a 20% p50 gain from run noise yet. The p99 is also unstable (0.287–0.655 ms) and should remain diagnostic, not a decision target.

For a climb, compare the median of several independent process runs per variant, or use a quieter pinned host. Keep the 20% p50 bar only if the expanded no-change controls put the noise floor below it. The HTML viewer reports per-case means for the per-call metric; the exact workload-wide and process-level p50/p99 and correctness totals are in each variant's `summary.json`.

## Independent-process repeat calibration

The repeat runner records five fresh processes per variant, with 60 measured calls per case in each process. This produces 1,800 rows per variant and reports `median_trial_p50_ms`, the median of five run-level p50 values.

| Same-code group | Run p50 values (ms) | Median (ms) | Range (ms) | Correct |
| --- | --- | ---: | ---: | ---: |
| Baseline, five fresh runs | 0.02000, 0.01930, 0.02480, 0.01910, 0.02380 | 0.02000 | 0.01910-0.02480 | 1800/1800 |
| No-change control, five fresh runs | 0.02770, 0.02510, 0.03090, 0.04020, 0.02650 | 0.02775 | 0.02505-0.04020 | 1800/1800 |

These two groups contain identical code and cases, but the second group's median is 38.8% slower. The groups ran sequentially, so this exposes host/time drift rather than a code effect. Five repetitions alone do not fix it. A single 20% p50 decision is not trustworthy on this host; comparisons need time-blocked paired runs (candidate and control interleaved) or a quieter pinned machine. The action bar remains 20%, but do not use it until the paired no-change differences stay inside a narrower band.

The aggregated baseline is in `baseline/results.jsonl` and `baseline/summary.json`; raw process runs live in `baseline/runs/trial-NN/`. The single-pass baseline and no-change control values are recorded above; regenerable per-process outputs are kept local.

## Paired no-change calibration

A frozen source snapshot and the working tree have identical SHA-256 source hashes. The runner compared them in 15 counterbalanced time blocks (60 calls per case per process; 10,800 recorded call rows total), and every row passed correctness. Pair p50 differences ranged from −20.1% to +47.0%; their median was −0.81%. The mean absolute difference was +0.00125 ms with a paired 95% t interval of [−0.00084, +0.00334] ms (half-width 0.00209 ms). Against the 0.020 ms five-process baseline median, that half-width is about 10.5%, below the agreed 20% action bar. The paired protocol can therefore resolve a 20% effect at this observed scale, though the wide individual-pair range means use the full 15-pair comparison and require the candidate effect to persist across pairs. The paired p50 values and calculation are in `baseline/evidence/paired/calibration-15pair-20261002/summary.json`; reproducible per-call outputs are kept local and ignored by Git.

The five-pair pilot's summary is retained separately and was less precise (95% half-width about 0.00559 ms). Do not use its median alone to decide a change. The HTML viewer reports the baseline workload; paired comparison details live in these metrics and the JSON summaries.
