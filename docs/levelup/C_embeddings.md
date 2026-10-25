# Workstream C — Embedding comparison: BAAI/bge-small-en-v1.5 (384d) vs nomic-embed-text-v1.5 (768d)

**Decision to settle:** keep the engine's current default `BAAI/bge-small-en-v1.5` (384-dim) or reindex to
`nomic-ai/nomic-embed-text-v1.5` (768-dim).
**Verdict: RECOMMEND reindex to nomic-embed-text-v1.5**, measured on CPU-only over 120 paired LoCoMo queries.
Machine-readable record: [`C_embeddings.json`](C_embeddings.json).

Measured 2026-10-18 on this workstation (Windows 11, 8 logical cores, 15.7 GB RAM, RTX 3060 Laptop 6 GB
present but **intentionally not used**). Numbers below are measured, not estimated; anything that could not
be measured is in [Not measured](#not-measured).

---

## 1. Headline numbers

Same 120 questions, same 2,080 memory units, same retrieval contract. `recall@k = |gold ∩ top-k| / |gold|`;
gold = LoCoMo evidence turn ids. All metrics are means over the 120 queries; CI = paired bootstrap over
queries (10,000 resamples) for the nomic−bge difference.

| metric | BAAI/bge-small-en-v1.5 (384d) | nomic-embed-text-v1.5 (768d) | delta | 95% CI |
|---|---:|---:|---:|---:|
| recall@1 | 0.2640 | **0.3447** | +0.0808 | [+0.0099, +0.1556] |
| recall@5 | 0.5076 | **0.6071** | +0.0995 | [+0.0355, +0.1651] |
| recall@10 | 0.5713 | **0.6810** | +0.1097 | [+0.0507, +0.1730] |
| MRR | 0.4380 | **0.5446** | +0.1067 | [+0.0418, +0.1741] |
| nDCG@10 | 0.4462 | **0.5499** | +0.1038 | [+0.0527, +0.1564] |

Every delta is positive, every 95% CI excludes zero, and the direction is consistent across recall@1/5/10,
MRR and nDCG@10. The smallest claimed gain (recall@1, +8.1 pts, CI low +1.0 pt) is the weakest; recall@10
(+11.0 pts, CI low +5.1 pts) and MRR (+10.7 pts, CI low +4.2 pts) are the strongest.

Per question type (small n for some categories — see caveats):

| LoCoMo category | n | bge recall@10 | nomic recall@10 | bge MRR | nomic MRR |
|---|---:|---:|---:|---:|---:|
| multi-hop | 43 | 0.4549 | **0.5400** | 0.3809 | **0.5016** |
| temporal | 64 | 0.6797 | **0.8125** | 0.5244 | **0.6266** |
| open-domain | 11 | 0.5000 | 0.5000 | 0.2379 | **0.2668** |
| single-hop | 2 | 0.0000 | **0.5000** | 0.0000 | **0.3750** |
| adversarial | 0 | — | — | — | — |

## 2. Cost numbers (the same run)

| | bge-small | nomic | factor |
|---|---:|---:|---:|
| embedding dim | 384 | 768 | 2.0x |
| model weights (safetensors) | 133.5 MB | 546.9 MB | 4.1x |
| model load (warm HF cache, CPU) | 3.77 s | 7.90 s | 2.1x |
| index build, 2,080 units (CPU, batch 32) | 57.3 s (36.3 units/s) | 208.5 s (10.0 units/s) | 3.6x |
| index footprint, float32 (2,080 units) | 3.05 MB (1,536 B/unit) | 6.09 MB (3,072 B/unit) | 2.0x |
| query latency p50 (encode + cosine) | 31.7 ms | 68.5 ms | 2.2x |
| query latency p95 | 93.1 ms | 81.4 ms | noisy — see caveats |
| peak RSS (sentence-transformers path) | 742.8 MB | 1,665.9 MB | 2.2x |
| context tokens per query, top-10 units | 545.9 | 586.5 | 1.07x |
| max input length | 512 | 8,192 | — |

Context tokens are `tiktoken cl100k_base` encoded lengths of the top-10 retrieved units (mean over the 120
queries). The tokenizer and the memory units are identical for both models; nomic's total is slightly higher
(586.5 vs 545.9) only because its top-10 sets happen to contain somewhat longer units — not because of a
model tokenizer. The token budget itself is model-neutral.

Latency/RSS caveat: quality metrics are deterministic given the fixed query set (repeat runs give identical
scores). Timing numbers were taken while other agent jobs (node, pytest) shared the 8 logical cores; the
bge-small run overlapped more contention than the nomic run, which is why its p95 is higher than nomic's
despite a lower p50. Treat the p50 ratio (≈2.2x) as the signal and absolute latency as machine-load
dependent; re-measure on an idle machine before publishing latency claims.

## 3. Methodology

### 3.1 What MemBase is, and what runs without an LLM

- Repo: <https://github.com/zjunlp/MemBase>, MIT, `Python >= 3.12` required, ~5.6 MB working tree,
  HEAD commit `a9ba9495d48541d3e68588ff097294cc7a1201bc` (2026-09-03).
- Three-stage CLI: `memory_construction.py` → `memory_search.py` → `memory_evaluation.py`. Default stage-3
  metrics are token-F1, BLEU and LLM-as-a-Judge (README).
- Datasets: LongMemEval, LoCoMo, RealMem, MobileMem.
- **Runnable without an LLM:** stage 1 (memory construction) and stage 2 (retrieval) for the `NaiveRAG`
  and `Long-Context` layers. `NaiveRAGConfig` only uses its `llm_model` field as a tokenizer name; with the
  defaults (`max_tokens=None`, `deferred=False`) construction embeds and stores message units without any
  generation call.
- **Not runnable without a model:** stage 3 (answer generation + judge/BLEU/F1). No paid API key and no
  Ollama/local LLM exist on this machine → marked BLOCKED below; no scores were synthesized.

### 3.2 Why this is a harness, not the official three-stage runner

The official stage-2 `SearchRunner` was not used for scoring because it cannot report gold-evidence recall:
its output is `{"retrieved_memories": [MemoryEntry(content, formatted_content, metadata)]}` where metadata
contains only the memory-unit key/score — the source message ids are gone. MemBase's own `NaiveRAG` config
also batches messages into 1,200-token deferred chunks, so unit↔turn mapping would be ambiguous even if the
ids were carried. Stage 3 additionally requires an LLM.

This workstream therefore uses a clearly-labelled minimal harness that **reuses MemBase's official dataset
loader and retrieval contract**:

- `membase.datasets.locomo.LoCoMo.read_raw_data()` (official loader, official message/session/QA models;
  LoCoMo question dates and evidence ids are parsed by MemBase, evidence ids become `U<sample>:<dia_id>`).
- **Memory unit:** exactly the string `NaiveRAGLayer.add_message` stores, one unit per message:
  `f"Speaker {name} (role: {role}) says: {content}\nTimestamp: {iso}"`.
  This is the layer's own defaults (`num_overlap_msgs=0`, `max_tokens=None`, `deferred=False`) under which
  `_buffer_and_get_doc` returns the new message alone.
- **Retrieval contract:** dense cosine top-10 over all units of the same trajectory (MemBase indexes per
  user; LangGraph `InMemoryStore`'s vector index is cosine over normalized vectors). Gold = the trajectory's
  `qa.metadata["evidence"]` message ids.
- Deviation, stated plainly: embeddings are produced with `sentence-transformers` directly (not through
  LangChain `init_embeddings`) so that each model's own required task prefixes can be applied; ids are kept
  so recall can be scored. It is **not** the official MemBase runner and makes no claim to be.

### 3.3 Data subset and protocol

- Data: `locomo10.json` (snap-research/locomo, 2,805,274 bytes, sha256
  `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`), downloaded to
  `.levelup-work/C-membase/data/`. Full dataset: 10 conversations, 272 sessions, 5,882 messages, 1,986 QA
  pairs via MemBase's loader (matches raw JSON).
- Subset (deterministic): first 4 conversations in loader order; first 30 valid QA pairs per conversation
  (QA whose evidence resolves to a message in that conversation) → **120 queries**, 2,080 memory units
  (419/369/663/629 units per conversation), 198 gold evidence turns (avg 1.65 per query). 120/120 queries
  had resolvable gold.
- Each model was measured on the identical query set, unit set and per-query ordering.
- **Prefixes (per each model's own model card, i.e. how it is meant to be used):**
  - bge-small-en-v1.5: query prefix `Represent this sentence for searching relevant passages: `, no doc prefix.
  - nomic-embed-text-v1.5: doc prefix `search_document: `, query prefix `search_query: `.
- Tokenizer: `tiktoken cl100k_base` (importable). Warm-up: first 5 queries excluded from latency stats.
  Model files were pre-downloaded, so model-load time is a warm-cache load.
- CPU only; `torch.set_num_threads` left at default (8); batch size 32 for indexing, 1 for queries.

### 3.4 Environment

| | |
|---|---|
| Python | 3.12.13 (uv-managed; MemBase requires >=3.12) |
| torch / sentence-transformers | 2.14.1+cpu / 5.2.3 |
| transformers | **4.56.2 (pinned)** — nomic's remote code calls `get_extended_attention_mask`, removed in transformers 5.x; 5.20.1 fails with `AttributeError`. Both models were re-run under the same pinned 4.56.2 stack. |
| numpy / tokenizer | 2.5.4 / tiktoken cl100k_base |
| CPU / RAM | 8 logical cores / 15.7 GB |
| GPU | RTX 3060 Laptop 6 GB, unused |

## 4. Decision and engine implications

**RECOMMEND reindex to `nomic-ai/nomic-embed-text-v1.5`.** The measured gain is large for an embedding change
(+11.0 pts recall@10, +10.7 pts MRR, 95% CI excludes zero), while the cost stays inside the engine's CPU-only
envelope: ~3.6x slower one-off reindex, exactly 2x index bytes per unit, ~2x query latency and ~2x model RSS
in this (sentence-transformers) path.

Effect size vs noise: the 95% CI half-width is ~6 pts on recall@10, so the lower bound of the gain is still
+5.1 pts; the direction is identical on five metrics. The only observed "noise" is (a) query-subset sampling,
which the paired bootstrap CI quantifies, and (b) machine load, which affects latency only, not quality.

What a rollout requires (from the engine source, `mnemosyne 3.15.x`):

1. `MNEMOSYNE_EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5`.
2. `MNEMOSYNE_EMBEDDING_DIM=768` — **mandatory**. The engine's dimension table has no nomic entry and falls
   back to 384; a 384/768 mismatch would corrupt similarity search. (Verified at `core/embeddings.py:186`.)
3. `MNEMOSYNE_EMBEDDING_QUERY_PREFIX='search_query: '` and
   `MNEMOSYNE_EMBEDDING_DOC_PREFIX='search_document: '` — the engine applies no built-in nomic prefixes
   (`_get_prefix`, `core/embeddings.py:79`); without them the numbers above do not transfer.
4. **Full reindex** (384 → 768 is a hard dim change; `mnemosyne reindex` recreates the sqlite-vec tables):

```bash
MNEMOSYNE_EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5 \
MNEMOSYNE_EMBEDDING_DIM=768 \
MNEMOSYNE_EMBEDDING_QUERY_PREFIX='search_query: ' \
MNEMOSYNE_EMBEDDING_DOC_PREFIX='search_document: ' \
mnemosyne reindex --yes
```

`mnemosyne reindex` takes a backup by default and prints the active model/dim; `--dry-run` shows the plan
first. On PowerShell the same command is:

```powershell
$env:MNEMOSYNE_EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
$env:MNEMOSYNE_EMBEDDING_DIM = "768"
$env:MNEMOSYNE_EMBEDDING_QUERY_PREFIX = "search_query: "
$env:MNEMOSYNE_EMBEDDING_DOC_PREFIX = "search_document: "
mnemosyne reindex --yes
```

To roll back, reindex the other way:

```bash
MNEMOSYNE_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 MNEMOSYNE_EMBEDDING_DIM=384 mnemosyne reindex --yes
```

Engine backend note: the engine embeds through **fastembed 0.8.0 (ONNX)**, not sentence-transformers.
`fastembed.TextEmbedding.list_supported_models()` lists both `BAAI/bge-small-en-v1.5` (384d, 0.067 GB) and
`nomic-ai/nomic-embed-text-v1.5` (768d, 0.52 GB), so no new dependency is required. ONNX-converted weights
can differ slightly from the PyTorch weights measured here, so a staging reindex + retrieval A/B is advised
before switching a large production DB; that engine-native measurement was not performed (see below).

## 5. Not measured

| item | status | why / what would unblock |
|---|---|---|
| token-F1, BLEU, ROUGE, BERTScore, LLM-as-a-Judge | **BLOCKED** | Stage 3 needs a generative QA model + judge. No paid API key and no Ollama/local LLM on this machine; scores were not synthesized. Unblock: start an OpenAI-compatible local chat server and run `memory_evaluation.py` with `--judge-model <local model>` (or provide a judge API key), after stages 1–2. |
| GPU footprint / latency | not measured | CPU-only is the honest, AMD-compatible engine scenario; the RTX 3060 was deliberately not used. Unblock: rerun the harness with `--device cuda`. |
| Quadro P4000 / RTX 3090 numbers | not measured | not this machine. Unblock: run the same harness there. |
| official MemBase 3-stage end-to-end numbers | partially blocked | Stage 2 drops memory-unit ids, so gold-evidence recall cannot be computed from its output; stage 3 needs an LLM. Unblock: carry ids through stage 2 (or evaluate stage 3 only). |
| engine-native fastembed/ONNX quality | not measured (support verified only) | The benchmark path is sentence-transformers as specified in the brief; fastembed 0.8.0's model registry contains both models but ONNX weights may differ. Unblock: run the same harness against `mnemosyne.core.embeddings` on this subset, or a `mnemosyne reindex --dry-run` + retrieval A/B on a real DB. |
| nomic without task prefixes / bge without instruction | not measured | Each model was used per its own model card. Since the engine only applies prefixes when the env vars are set, an unconfigured rollout would not match these nomic numbers. Unblock: rerun with prefixes disabled for both. |
| LongMemEval / RealMem / MobileMem, adversarial questions, multi-subset variance | not measured | Timebox: one dataset, 4 conversations, 120 queries. The subset happened to contain no adversarial (category 5) and only 2 single-hop questions, so those per-category rows are anecdotal: temporal n=64, multi-hop n=43, open-domain n=11, single-hop n=2. Unblock: extend to LongMemEval and a larger LoCoMo slice; quality CIs would tighten similarly. |
| GPU/on-device server-class CPU numbers | not measured | see above. |

## 6. Caveats / risks in this measurement

1. **Path mismatch with the engine.** Measured with sentence-transformers + PyTorch on the models' original
   weights; the engine loads ONNX fastembed models. Direction is very likely preserved, exact magnitudes are
   not guaranteed. This is the top risk to the decision.
2. **Prefix configuration is part of the result.** Nomic's numbers are only valid with `search_query:` /
   `search_document:` prefixes; the engine needs explicit env configuration for those. An unconfigured
   rollout silently underperforms and is not covered by this data.
3. **Message-granularity retrieval.** One memory unit per LoCoMo message (NaiveRAG's own default unit
   granularity), not the 1,200-token deferred chunks used in MemBase's example configs. Evidence-turn recall
   is the right metric for this granularity; it does not directly predict QA accuracy with chunked memories.
4. **Single dataset, single run, deterministic scores.** No run-to-run quality variance exists, but the
   120-query subset is one draw; the bootstrap CIs quantify that sampling uncertainty, not dataset diversity.
5. **Latency/RSS measured under shared machine load** — p50 ratios are usable, absolute values are not
   publishable. Peak RSS is the sentence-transformers path and overstates what the fastembed engine would use.
6. **Tokenizer choice.** `cl100k_base` was used for both models' context-token counts; it is not each model's
   own tokenizer, so per-query token totals are comparative, not billing-exact.

## 7. Reproduction

All scratch work (not committed) is in `.levelup-work/C-membase/`: `harness.py`, `analyze.py`, `results/`,
`indexes/`, and the isolated `.venv`.

```powershell
# 1. Python 3.12 + isolated env (MemBase requires >=3.12)
uv python install 3.12
uv venv --python 3.12 .levelup-work/C-membase/.venv
uv pip install --python .levelup-work/C-membase/.venv/Scripts/python.exe `
  -r .levelup-work/C-membase/MemBase/envs/rag_requirements.txt psutil einops
uv pip install --python .levelup-work/C-membase/.venv/Scripts/python.exe "transformers==4.56.2"

# 2. MemBase + data
git clone --depth 1 https://github.com/zjunlp/MemBase .levelup-work/C-membase/MemBase
# locomo10.json from https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

# 3. Measure both models on the same 120 queries (HF_HOME pointed into the scratch dir)
$env:HF_HOME = "$PWD\.levelup-work\C-membase\hf-cache"
.\.levelup-work\C-membase\.venv\Scripts\python.exe .\levelup-work\C-membase\harness.py `
  --model bge-small --n-samples 4 --queries-per-sample 30 --out results\bge_small.json
.\.levelup-work\C-membase\.venv\Scripts\python.exe .\levelup-work\C-membase\harness.py `
  --model nomic --n-samples 4 --queries-per-sample 30 --out results\nomic.json

# 4. Paired bootstrap + JSON record
.\.levelup-work\C-membase\.venv\Scripts\python.exe .\levelup-work\C-membase\analyze.py
```

Sources: [MemBase](https://github.com/zjunlp/MemBase) (MIT; HEAD 2026-09-03),
[LoCoMo data](https://github.com/snap-research/locomo/blob/main/data/locomo10.json),
[LoCoMo paper](https://arxiv.org/abs/2402.17753), engine source
`mnemosyne 3.15.x` `core/embeddings.py` (default model line 70; dims 135–186; prefixes 79).
