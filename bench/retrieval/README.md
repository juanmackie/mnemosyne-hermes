# Retrieval fixture replay

This harness replays the retrieval fixtures from the Rust era against the
pinned Python engine.

The fixtures come from `b548943:.auto/`. The original runner (`evaluate.py` and
`setup_data.py`, both removed in `032c848`) called the retired Rust binary
`target/release/mnemosyne`. The Rust tree is gone, so this harness keeps the
data and the metric and replaces only the call layer with the engine's public
`Mnemosyne` facade.

## Files

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `corpus.jsonl` | 35765 | `19b36cce896ade29efb267d254924d6c7c975f19fe5bda7de6e1ecb4979a0481` |
| `eval_dev.jsonl` | 3743 | `cd9f16dd94eb8e46b783dfe5faa1ff4df5771fa8a600ad85476b26514a7e2de0` |
| `eval_heldout_a.jsonl` | 3731 | `8d3388647a008b45e041896234360a8a10ce3cdf7e3062516934099997654518` |
| `eval_heldout_b.jsonl` | 3659 | `ac6d52d2e83463df6d203bb6391d69d2ae22b44921b8a4943b3bf751af940831` |

The four JSONL files are byte-identical to `b548943:.auto/`. To compare one:

```bash
git show b548943:.auto/corpus.jsonl | cmp - bench/retrieval/corpus.jsonl
```

`corpus.jsonl` has 177 rows. Nine rows carry `supersedes`, which names the
older row they replace. Each query set has 27 queries with a `category` and a
list of `relevant` substrings. The dev split is `correction` 6, `already_told`
5, `structured_current` 5, `entity_exact` 5, `multi_constraint` 6.

## Method

`run_eval.py` replays the original order:

1. Ingest `corpus.jsonl` into a throwaway engine database. `importance` is
   divided by 10 because the engine expects 0.0-1.0.
2. Apply the `supersedes` pairs through `Mnemosyne.invalidate`.
3. Backdate `created_at` and `timestamp` by the row's `age_days`.
4. Copy the database for each query, then call `recall(query, top_k=5)`. The
   copy stops recall counters from one query changing the next ranking.
5. Rank a result when a `relevant` substring occurs in its `id`, `summary` or
   `content`. Report Hit@1, Hit@5, MRR, empty-result count and per-category MRR.

Embeddings are off (`MNEMOSYNE_EMBEDDINGS_OFF=1`), so this measures the lexical
path and needs no API key. The engine keeps its connections in a module
thread-local and has no `close()`, so teardown tracks `sqlite3.connect` and
resets that local. Without it, Windows keeps the database locked.

## Run

```bash
# one split, with per-query detail
python bench/retrieval/run_eval.py --dataset bench/retrieval/eval_dev.jsonl --details

# all three splits, record the result
python bench/retrieval/run_eval.py --all --json-out bench/retrieval/result.json
```

To measure a candidate engine without touching the installed one, put its
package root first on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/engine-root python bench/retrieval/run_eval.py --all
```

## Limits

- The corpus is synthetic and small: 177 rows, 5-6 queries per category. One
  rank flip moves a category MRR by up to 0.2. The two heldout splits are the
  guard against tuning on dev alone.
- The run covers working memory only. It does not consolidate, so it never
  reaches episodic paths.
- Embeddings are off. The dense path is not measured.
- The score is ranking quality on these fixtures. It is not a production
  quality claim.

`RESULTS.md` records the baseline and the shipped change. `baseline.json` and
`candidate.json` are the raw reports behind its tables. Their latency fields
come from the recording machine; the ranking claim uses MRR only.
