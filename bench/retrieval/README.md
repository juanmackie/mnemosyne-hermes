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
| `eval_paraphrase.jsonl` | 5501 | `228700ba1b015843ccde08452ca9e1e46a31eb70f785603cdb417aeb88fdb659` |

The four `b548943` fixtures are byte-identical to `b548943:.auto/`. To compare
one:

```bash
git show b548943:.auto/corpus.jsonl | cmp - bench/retrieval/corpus.jsonl
```

`eval_paraphrase.jsonl` is new and has no `b548943` counterpart; see
[The paraphrase split](#the-paraphrase-split).

`corpus.jsonl` has 177 rows. Nine rows carry `supersedes`, which names the
older row they replace. Each query row has a `query`, a `category` and a list
of `relevant` substrings; a result counts as the target when one of those
substrings occurs in its `id`, `summary` or `content`. The dev split is
`correction` 6, `already_told` 5, `structured_current` 5, `entity_exact` 5,
`multi_constraint` 6. The paraphrase split is `paraphrase_config` 10,
`paraphrase_entity` 8, `paraphrase_task` 8, `paraphrase_preference` 6 and
`unanswerable` 5.

## Method

`run_eval.py` replays the original order:

1. Ingest `corpus.jsonl` into a throwaway engine database. `importance` is
   divided by 10 because the engine expects 0.0-1.0.
2. Apply the `supersedes` pairs through `Mnemosyne.invalidate`.
3. Backdate `created_at` and `timestamp` by the row's `age_days`.
4. Copy the database for each query, then call `recall(query, top_k=5)`. The
   copy stops recall counters from one query changing the next ranking.
5. Rank a result when a `relevant` substring occurs in its `id`, `summary` or
   `content`. Report Hit@1, Hit@5, MRR, empty-result count, per-category MRR,
   `latency_p50_ms`/`latency_p95_ms` and, at the top level of `--json-out`,
   `peak_rss_mb` (stdlib `resource.getrusage`; the field is `null` on Windows,
   which has no `resource` module) plus the `embeddings`/`embedding_model`
   A/B arm that produced the file.

Embeddings are off (`MNEMOSYNE_EMBEDDINGS_OFF=1`), so this measures the lexical
path and needs no API key. The engine keeps its connections in a module
thread-local and has no `close()`, so teardown tracks `sqlite3.connect` and
resets that local. Without it, Windows keeps the database locked.

## Run

```bash
# one split, with per-query detail
python bench/retrieval/run_eval.py --split dev --details

# the paraphrase split
python bench/retrieval/run_eval.py --split paraphrase

# an explicit fixture path
python bench/retrieval/run_eval.py --dataset bench/retrieval/eval_dev.jsonl --details

# every split (dev, heldout_a, heldout_b, paraphrase), record the result
python bench/retrieval/run_eval.py --all --json-out bench/retrieval/result.json
```

`--split` takes `dev`, `heldout_a`, `heldout_b` or `paraphrase` and may be
repeated. With no selector the harness replays `dev`, so the lexical baseline
is unchanged. `--all` now covers all four splits; the reports for `dev`,
`heldout_a` and `heldout_b` are the same as before.

To measure a candidate engine without touching the installed one, put its
package root first on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/engine-root python bench/retrieval/run_eval.py --all
```

## The paraphrase split

`eval_paraphrase.jsonl` exists because the other three splits cannot tell an
embedding model upgrade from a keyword baseline. Their queries share most of
their words with the answer row: mean query-token coverage of the target row is
0.46-0.48, so BM25/FTS5 alone already finds the row and a dense re-rank has
nothing left to add. That is why the 2026-10-26 embedding A/B in `RESULTS.md`
reported identical MRR for `bge-small`, `bge-base` and the lexical baseline.

The new split asks each fact a second time in different words. Mean coverage
drops to 0.23, and every query keeps exactly one anchor token that also occurs
in other rows, so the keyword index admits the row without being able to
identify it. A keyword-only run should therefore fall well below a dense run;
the gap, not the absolute MRR, is the signal. `tests/test_retrieval_eval.py`
gates the split on schema, ground truth, unique anchor and that coverage.

Five `unanswerable` queries (salary, rent, degree, blood group, airline
programme) carry an empty `relevant` list. They have no correct row, so their
hit@1 and MRR are 0 by construction; the number to read for them is `empty`,
which counts queries that return nothing at all.

This machine has no engine installed, so the split is data only here. The
section below gives the command, not a recorded result.

### Offline A/B across embedding models

The engine picks its model and dimension from the environment, and the harness
turns the dense path on with `MNEMOSYNE_EVAL_EMBEDDINGS=on`. Every arm seeds and
queries its own throwaway database, so the arms cannot contaminate each other
and no reindex is needed.

```bash
# arm 1: lexical baseline (embeddings off, the default)
python bench/retrieval/run_eval.py --split paraphrase --json-out bench/retrieval/ab-lexical.json

# arms 2-4: one embedding model each
MNEMOSYNE_EVAL_EMBEDDINGS=on MNEMOSYNE_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
MNEMOSYNE_EMBEDDING_DIM=384 \
  python bench/retrieval/run_eval.py --split paraphrase --json-out bench/retrieval/ab-bge-small.json

MNEMOSYNE_EVAL_EMBEDDINGS=on MNEMOSYNE_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5 \
MNEMOSYNE_EMBEDDING_DIM=768 \
  python bench/retrieval/run_eval.py --split paraphrase --json-out bench/retrieval/ab-bge-base.json

# nomic needs its task prefixes, otherwise its numbers do not transfer
MNEMOSYNE_EVAL_EMBEDDINGS=on MNEMOSYNE_EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5 \
MNEMOSYNE_EMBEDDING_DIM=768 \
MNEMOSYNE_EMBEDDING_QUERY_PREFIX='search_query: ' \
MNEMOSYNE_EMBEDDING_DOC_PREFIX='search_document: ' \
  python bench/retrieval/run_eval.py --split paraphrase --json-out bench/retrieval/ab-nomic.json

# compare the arms: MRR and p95 latency per run, peak RSS per process
python - <<'PY'
import glob, json
for path in sorted(glob.glob("bench/retrieval/ab-*.json")):
    report = json.load(open(path))
    para = report["reports"]["paraphrase"]
    print(f"{path:38s} model={report['embedding_model']} "
          f"mrr={para['mrr']:.4f} hit1={para['hit1']:.4f} empty={para['empty']} "
          f"p95={para['latency_p95_ms']:.1f}ms rss={report['peak_rss_mb']}MB")
PY
```

This is offline only after each model's ONNX weights are in the local fastembed
cache: the first run per model downloads them, and a machine with no network
fails that arm loudly. The dimensions above are the models' own; `3.15.x` has
no nomic entry in its dimension table, so `MNEMOSYNE_EMBEDDING_DIM` is
mandatory there (see [docs/levelup/C_embeddings.md](../../docs/levelup/C_embeddings.md)).
Read MRR and `empty` per arm. Latency and RSS are recorded, but the fixture is
177 rows, so they are smoke numbers, not a capacity claim.

## Limits

- The corpus is synthetic and small: 177 rows, 4-10 queries per category. One
  rank flip moves a category MRR by up to 0.2. The two heldout splits are the
  guard against tuning on dev alone.
- The run covers working memory only. It does not consolidate, so it never
  reaches episodic paths.
- Embeddings are off unless `MNEMOSYNE_EVAL_EMBEDDINGS=on`. The default run
  measures the lexical path only.
- The paraphrase split's ground truth is one row per query, checked by
  `tests/test_retrieval_eval.py`. It does not record a second acceptable row,
  so a semantically reasonable neighbour counts as a miss.
- The score is ranking quality on these fixtures. It is not a production
  quality claim.

`RESULTS.md` records the baseline and the shipped change. `baseline.json` and
`candidate.json` are the raw reports behind its tables. Their latency fields
come from the recording machine; the ranking claim uses MRR only.
