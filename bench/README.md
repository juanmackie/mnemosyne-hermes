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

## `memory_writer_eval.py` — memory-writer quality

This is a separate evaluation seam. `hermes_prefetch_eval.py` seeds memories and
measures injected context; `precision_recall_eval.py` measures retrieval
cutoffs. Neither isolates what a writer chooses to store. This evaluator scores
writer artifacts produced from the same synthetic traces with the retrieval,
answer and decoding settings held fixed.

The fixture (`memory_writer_cases.json`, format
`mnemosyne-memory-writer-cases/v1`) contains 24 authored synthetic cases:
12 personal and 12 coding, split 16 development / 8 held-out. Every case has a
trace, optional context, optional seed memories, independently labeled required
and forbidden propositions, evidence anchors, a no-op expectation, and a recall
query. No external benchmark data, real transcript, credential or live store is
used. The research rationale and licensing limits are in
[`docs/HY_MEMORY_RESEARCH.md`](../docs/HY_MEMORY_RESEARCH.md).

### Artifact contract

A candidate artifact is JSON (format
`mnemosyne-memory-writer-artifact/v1`):

```json
{
  "format": "mnemosyne-memory-writer-artifact/v1",
  "writer": {"name": "example", "model": "example-model", "prompt_version": "v1"},
  "cases": [
    {
      "id": "personal-dev-01",
      "memories": [
        {
          "content": "The user takes coffee with oat milk and 2 shots of espresso.",
          "kind": "preference",
          "subject": "user",
          "subject_name": null,
          "scope": "session",
          "valid_until": null,
          "evidence": ["u1"]
        }
      ],
      "operations": [{"op": "add", "memory": 0}]
    }
  ]
}
```

`kind` is one of `fact`, `preference`, `identity`, `plan`, `experience`,
`decision`, `blocker`. `subject` is `user`, `assistant`, `repo` or `other`.
`scope` is `session` or `global`. `evidence` lists trace message IDs; a
memory that cites context or an unknown ID fails. `operations` must reference
every memory exactly once. `add` creates a new memory; `update` and `supersede`
require a seed ID in `target`. A case with `expect_no_memories` must have no
memories or operations. A candidate with no cases is valid but fails every
required case; malformed shapes, unknown keys, oversized content, bad dates,
duplicate case IDs and invalid operation indexes are rejected.

### Scoring and limits

The scorer is deliberately lexical and deterministic. It checks required and
forbidden propositions, subject/kind ownership, evidence anchors, duplicate
claims, expected seed-preserving or mutation behavior, no-op behavior, and
content bounds. It does **not** prove semantic entailment: a paraphrase can be
missed, and a sentence can contain a required token while drawing a wrong
conclusion. Wrong-conclusion controls and held-out cases are included; review
disputed cases manually. `required_claim_recall` is a label-retention measure,
not an answer-quality score. Character counts are not billed tokens, and no
latency claim is made.

### Run

```bash
python3 tests/test_memory_writer_eval.py   # evaluator self-check, no pytest needed
python3 bench/memory_writer_eval.py \
  --cases bench/memory_writer_cases.json \
  --baseline /path/to/baseline.json \
  --candidate /path/to/candidate.json \
  --output /path/to/new-report.json \
  --require-no-regression --require-pass
```

The report is a new JSON file; the tool refuses to overwrite an input, an
existing report, or a SQLite store path. Exit codes: `0` report written and
requested gates passed; `1` quality or regression gate failed; `2` invalid
input/artifact/environment or unsafe output. `--require-no-regression` fails if
the candidate loses a required claim or case pass that the baseline had.

`oracle_artifact()` is an instrument check built from the fixture labels. It is
used by the tests and must never be presented as model-quality evidence.

### Optional provider replay

`--replay-provider` applies the candidate operations to fresh temporary stores
through the existing public provider handlers. It requires the audited pinned
engine and refuses live DB/home selectors; it sets throwaway `HERMES_HOME` and
`MNEMOSYNE_DATA_DIR`, disables embeddings, and never calls a model or network.
It configures the fixture's declared `replay_tools` through the provider's
`memory.mnemosyne.tools` resolution path, so a tool the provider actually
cannot expose fails the case instead of being assumed available. The report
records the `mnemosyne-memory` distribution version and the provider's pinned
engine requirement. It checks persisted content and recall reachability.
Corrections are replayed as in-place updates, so replay does **not** assert
historical-chain semantics. If the engine is unavailable, replay fails loudly;
it does not silently skip.

### Collecting real writer artifacts

Artifact creation happens outside this script. Use an existing, explicitly
selected Hermes model environment; do not add a credential, paid endpoint,
model download or live-store connection. Record the model, prompt version,
decoding settings, fixture SHA-256, and the empty starting-bank state. Produce
baseline and candidate artifacts from the same traces. Compare development and
held-out results by domain, retain failures in the denominator, and repeat
paired runs before promoting a prompt or engine change. A writer-only result
does not establish retrieval or production improvement.

## Scope

- `src/mnemosyne_lite/storage.py` owns recall semantics and the storage path.
- This harness reports memo-hit and SQL-miss paths separately; do not compare a
  new result against an older generic `search_*` number without checking which
  path it represents.
- Contract-frozen: namespace semantics, DB path, tool names, `integrations/`.
