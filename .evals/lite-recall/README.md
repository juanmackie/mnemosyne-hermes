# Lite recall eval

Run from the repository root with Python 3.11+ and SQLite FTS5:

```powershell
python .evals/lite-recall/runner.py --pilot
python .evals/lite-recall/replicate.py --variant baseline --trials 5 --reps 60 --archive-existing
python .evals/lite-recall/paired.py --left-source .evals/lite-recall/baseline/source --right-source src --left-label baseline-snapshot --right-label candidate --pairs 15 --reps 60 --run-id candidate-check
# Then run the installed build-eval report script on .evals/lite-recall
```

The pilot writes three representative cases with one measured call each to `pilot/`. The repeat runner launches five independent Python processes, each measuring six cases by 60 calls, and aggregates 1,800 rows under `baseline/`. Each process keeps resumable rows, traces, summary, and errors under `baseline/runs/trial-NN/`. Completed `(case, rep)` pairs are skipped on rerun.

Cases, grading, and the measured call use `src/mnemosyne_lite/storage.py`'s real `PythonMemoryStorage.recall()` entry point. Each process builds a fresh deterministic 3,000-row synthetic corpus in a temporary SQLite database under the flow directory, warms each query ten times, then times repeated calls. The report's per-call `latency_ms` metric is descriptive; the decision metric is the median of the five process-level p50 values. `correct` is a hard correctness guardrail.

For a change comparison, `paired.py` runs the frozen baseline source and candidate in counterbalanced order within 15 time blocks. It writes raw process runs and a paired-difference summary under `baseline/evidence/paired/<run-id>/`. Keep the same pair count and reps for baseline and candidate comparisons; the 15-pair no-change calibration is recorded in `metrics.md`.

Review the literal inputs and expected outcomes in `cases.md` before accepting this suite as a trusted benchmark. See `metrics.md` for metric definitions and coverage limits.
