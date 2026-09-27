# Benchmarks

Two things live here: the recall-latency benchmark, and the experiment loop that
was run against it. The loop's ledgers are not kept in the repo (git history
has them); only the reproducible harness and this record are.

## `measure.sh` — recall latency

`PythonMemoryStorage.recall()` on a deterministic 3000-row corpus, over six
query shapes:

| # | query | shape |
| --- | --- | --- |
| 0 | `memory` | common term + namespace filter |
| 1 | `xylophone` | rare term, no namespace |
| 2 | `midnight lantern protocol` | multi-word phrase |
| 3 | `zzzqqqnomatch` | full-scan miss |
| 4 | `project` | common + importance floor |
| 5 | `memory` | wide, `max_results=50` |

`bench/data/bench_corpus.db` is persistent and rebuilt only when missing, stale,
or its index set does not match the code under test, so insert-phase I/O jitter
never pollutes the timings. It prints `METRIC name=value` lines and exits
non-zero if recall semantics change:

- `assert_ok` — 7 hits for `xylophone`, 2 for the phrase, 0 for the miss
- `search_p50_ms`, `search_p99_ms` — median / tail over all shapes (60 reps each)
- `q0_p50_ms` … `q5_p99_ms` — per-shape median and tail

```bash
bash bench/measure.sh
MEASURE_PYTHON=/path/to/python bash bench/measure.sh   # pin the interpreter
```

It needs only the stdlib plus `src/`; no engine, no Hermes, no network.

## Noise

Single-invocation noise at the microsecond scale measured ±8–20% on p50. A
single run is not a result. `autoresearch.sh` runs the harness `AR_RUNS` times
(default 3, forced odd) and reports the median per metric, and takes two more
samples (median-of-5) when the median lands within 8% of `AR_BASELINE` so noise
cannot flip a keep/discard decision.

```bash
bash bench/autoresearch.sh
AR_RUNS=5 AR_BASELINE=0.0051 bash bench/autoresearch.sh
```

## `run.sh` — experiment ledger

One iteration: measure, append to `bench/log.jsonl`, print the delta against the
best kept run and the measured noise floor. Nothing is logged on a dry run.

```bash
./bench/run.sh                                       # dry run, no logging
./bench/run.sh keep "candidate idea"
./bench/run.sh discard "candidate idea" '{"why": "..."}'
```

A run whose measurement failed cannot be logged as `keep`: the harness exit code
is captured before anything else runs. `bench/last_measure.txt` keeps the raw
harness output of the last iteration.

## Recorded result

The retired loop's last state, from git history (commit `64a2c9e`, "best
0.0051ms p50 (-50%)"), against a starting point of 0.0102 ms:

| metric | value |
| --- | --- |
| `search_p50_ms` | 0.0051 |
| change vs start | −50% |

That number was measured with the layered caches the loop added (id-tuple
batching, in-place row patching, per-thread counters, two caches). It is **not**
re-measured here: this doc records what the loop claimed at the time, not a
current measurement. Anything the loop bought by adding a cache is a claim to
re-earn, and a cache is also where the cross-thread staleness bug came from.

## Scope

- Editable: `src/mnemosyne_lite/storage.py` — the recall path.
- Not the target: this harness. Editing the instrument to improve the number is
  not a result.
- Contract-frozen: namespace semantics, DB path, tool names, `integrations/`.
