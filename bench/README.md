# Benchmarks

This directory contains the reproducible recall-latency harness and its record.

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

The same run also reports cold-store metrics from three fresh databases, each
with 5000 writes:

- `cold_init_p50_ms` — median time to create a new store and its schema
- `write_5000_p50_ms` — median time to insert 5000 rows
- `store_5000_bytes` — median database file size after closing the store

```bash
bash bench/measure.sh
MEASURE_PYTHON=/path/to/python bash bench/measure.sh   # pin the interpreter
```

It needs only the stdlib plus `src/`; no engine, no Hermes, no network.

Cold metrics use a fresh database for each sample, so schema initialization and
write timings do not inherit the persistent recall corpus. These are local
measurements, not a guarantee about storage hardware or filesystem cache state.

## Recorded result

Measured on the dev host (Windows, Python 3.11) with `AR_RUNS=5`, after recall
moved to FTS5 (see below). Median of five invocations:

| metric | value |
| --- | --- |
| `search_p50_ms` | 0.0058 |
| `search_p99_ms` | 0.444 |
| `assert_ok` | 1 |

Two single invocations during the same session gave p50 0.0053 and 0.0083 — a
56% spread, which is why the number above is a median and why a single run is
not a result. The p99 tail is dominated by shape 5 (wide, `max_results=50`),
where BM25 has to rank every match before the `LIMIT`.

### What changed, and what the old number meant

The retired experiment loop recorded `search_p50_ms` 0.0051 (commit `64a2c9e`,
"best 0.0051ms p50 (-50%)") against a starting point of 0.0102 ms. It got there
by layering id-tuple batching, in-place row patching, per-thread counters and
**two** caches — one of which cached a full copy of every memory's text per
thread, so memory grew with the corpus times the thread count, and one of which
was shared across threads and produced a cross-thread staleness bug.

The current implementation replaces the substring scan with an FTS5 index and
BM25 ranking. That removes the text snapshot entirely (the index is external
content, so the corpus is not duplicated) and leaves one cache: the per-thread
recall memo, which only holds rows a thread actually asked for. The p50 is
within the noise band of the old claim; the honest reading is "about the same
p50, less memory, real relevance ranking, one fewer cache", not a speed-up.

Ranking is no longer `importance DESC, created_at DESC`: it is `bm25`, then
that chain as the tie-break, then `id` so a `LIMIT` is deterministic.

## Scope

- Editable: `src/mnemosyne_lite/storage.py` — the recall path.
- Not the target: this harness. Editing the instrument to improve the number is
  not a result.
- Contract-frozen: namespace semantics, DB path, tool names, `integrations/`.
