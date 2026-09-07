# Mnemosyne Retrieval Benchmarks

Quality evaluation harness for memory retrieval, structured after
OpenViking's benchmark layout (one directory per suite, reproducible scripts).

## Retrieval suite (`locomo_eval.py`)

Measures **Hit@k** and **MRR** for `mnemosyne recall`, comparing flat hybrid
search against `--hierarchical` topic-tree reranking.

### Dataset format

JSONL, one item per line:

```json
{"query": "how do we handle caching?", "relevant_ids": ["uuid-or-summary", ...]}
```

### Running

Requires a Mnemosyne binary on `PATH` and a DB populated with the memories the
queries are written against. Two dataset shapes are supported:

```bash
# Hand-authored QA dataset (locomo_eval.py format: `relevant_ids`):
#   {"query": "...", "relevant_ids": ["uuid-or-summary", ...]}
# Ingest memories first, then:
python3 benchmark/retrieval/locomo_eval.py \
    --dataset benchmark/retrieval/my_set.jsonl \
    --db /path/to/mnemosyne.db \
    -k 5 \
    --mode compare

# Reproducible in-repo real-query datasets (tracked in git):
#   .auto/eval_dev.jsonl
#   .auto/eval_heldout_a.jsonl
#   .auto/eval_heldout_b.jsonl
# These query the fixed corpus in .auto/corpus.jsonl. Regenerate their DB with
#   python3 .auto/setup_data.py   # prints .auto/data/template.db
# then run a dataset against that DB (see .auto/measure.sh for the full flow).
# The .auto sets use `relevant`/`category` keys and are consumed by
# .auto/evaluate.py, not locomo_eval; locomo_eval's `relevant_ids` shape is the
# generic hand-authored variant above.
```

`--mode compare` runs both flat and hierarchical retrieval and prints both
rows so improvements from topic-tree reranking are directly visible.

### Reporting results

Every reported metric must identify its exact provenance so it can be
reproduced or compared against a later revision:

- **Binary revision**: git commit (run `git rev-parse --short HEAD`).
- **Provider/model**: build feature set and embedding model, e.g.
  `release --features local-embeddings`, `MNEMOSYNE_EMBEDDING_MODEL=` value.
  keyless-default (no `local-embeddings`) uses hash fallback embeddings and
  is not comparable to model-backed numbers.
- **Dataset revision**: the dataset file and its git revision; note whether it
  was an in-repo `.auto/eval_*.jsonl` set or a local file.
- **Config**: `-k`, `--mode`, `--namespace`, `--db`, and any recall flags.

See `.auto/measure.sh` and `benchmark/retrieval/locomo_eval.py` for the
canonical repro steps and the CLI-timeout/error-handling behavior.

### Notes

- Uses only the public CLI, so it works against any Mnemosyne deployment.
- `relevant_ids` accept either memory UUIDs or summary-text prefixes for
  hand-authored datasets.
- The name honors the LoCoMo long-conversation memory benchmark; any QA
  dataset in the format above works.
