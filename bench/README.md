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
- `memo_hit_search_p50_ms`, `memo_hit_search_p99_ms` — repeated calls served by
  the per-thread recall memo, across all six shapes
- `sql_miss_search_p50_ms`, `sql_miss_search_p99_ms` — the same shapes with
  that query's memo entry evicted before every timed call; this includes SQL,
  row decoding, result copies and normal buffered access accounting
- `memo_hit_q0_p50_ms` … `memo_hit_q5_p99_ms` and
  `sql_miss_q0_p50_ms` … `sql_miss_q5_p99_ms` — per-shape medians and tails
- `search_p50_ms`, `search_p99_ms`, `q0_p50_ms` … `q5_p99_ms` — retained
  aliases for memo-hit metrics so existing consumers keep working

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

Measured on the dev host (Windows, Python 3.11). Median of four consecutive
invocations against the already-built corpus:

| path | p50 | p99 |
| --- | --- | --- |
| memo hits, all shapes | 0.0052 ms | 0.1882 ms |
| forced SQL misses, all shapes | 0.2938 ms | 3.6348 ms |
| SQL miss, q0 common + namespace | 1.8605 ms | 4.2326 ms |
| SQL miss, q5 wide 50 results | 2.4082 ms | 3.8425 ms |

All four runs reported `assert_ok=1`. These are local measurements; SQLite and
filesystem cache state affect them. The gap between memo hits and SQL misses is
why the harness reports the paths separately. The original `search_*` values
measured memo hits only, despite their generic names.

The widest query shape has the highest SQL-miss median. Its BM25 ranking and
deterministic tie-break still require sorting the matching rows before the
`LIMIT`; the benchmark makes that query cost visible separately from memo hits.

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
recall memo, which only holds rows a thread actually asked for. The old
0.0058 ms p50 was a memo-hit result, not a measurement of FTS search latency.

Ranking is no longer `importance DESC, created_at DESC`: it is `bm25`, then
that chain as the tie-break, then `id` so a `LIMIT` is deterministic.

## Scope

- `src/mnemosyne_lite/storage.py` owns recall semantics and the storage path.
- This harness reports memo-hit and SQL-miss paths separately; do not compare a
  new result against an older generic `search_*` number without checking which
  path it represents.
- Contract-frozen: namespace semantics, DB path, tool names, `integrations/`.
