# Embedding Candidate Evaluation: Q vs EmbeddingGemma-300M vs fp32 Nomic

**Date**: 2026-08-25
**Branch**: `feature/embedding-candidates-q-gemma` (this repository)
**Baseline reference**: fp32 `nomic-embed-text-v1.5`, dev MRR **0.947368**
(measured at `91350c4`, autoresearch run 82)

## Question

Should either `NomicEmbedTextV15Q` (quantized nomic v1.5, ~4x smaller
download) or `EmbeddingGemma300M` (Google on-device SOTA class, MTEB ~69.7
at 300M params) replace the fp32 nomic default?

## Method

Fixed protocol, no tuning to datasets:

1. Added fastembed 5.2.0 mappings (`nomic-embed-text-v1.5-q`,
   `embedding-gemma-300m`) and a `MNEMOSYNE_EMBEDDING_MODEL` runtime
   override. The shipped default remains fp32 nomic.
2. Rebuilt the fixed corpus (`.auto/setup_data.py`) per candidate so ingest
   and query embeddings use the same model.
3. Evaluated each candidate on the dev split only
   (`.auto/eval_dev.jsonl`, 38 queries, flat mode).
4. The dev winner received exactly one held-out confirmation via
   `./.auto/measure.sh` (CLI + MCP held-out surfaces).

## Results

| Model | Dev MRR | Dev hit@1 | Dev hit@5 | Held-out MRR | Latency p95 | Download |
|---|---|---|---|---|---|---|
| fp32 nomic v1.5 (baseline) | 0.947368 | — | — | 0.933333 | ~3323 ms* | ~520 MB |
| `nomic-embed-text-v1.5-q` | 0.949561 | 0.9211 | 1.000 | not run | 1386 ms | ~35 MB (same repo, quantized ONNX) |
| `embedding-gemma-300m` | **0.953947** | 0.9211 | 1.000 | **0.958333** | 4475 ms | ~650 MB fp32 (onnx-community) |

\* Baseline latency from autoresearch run 82 (3322.808 ms); candidate
latencies measured in this evaluation.

Raw outputs: `/tmp/eval_dev_q.json`, `/tmp/eval_dev_gemma.json`,
`/tmp/measure_gemma_heldout.log` (session artifacts; key METRIC lines below).

```
METRIC hermes_phase1_gates=39
METRIC hermes_phase1_failures=0
METRIC recall_heldout_mrr=0.958333
METRIC recall_cli_heldout_mrr=0.958333
METRIC recall_mcp_heldout_mrr=0.958333
METRIC recall_dev_mrr=0.953947
METRIC recall_heldout_hit5=1.000000
METRIC recall_latency_p95_ms=4475.432
METRIC binary_size_mib=54.0714
```

## Decision

**Outcome: EmbeddingGemma300M wins on quality.**

- Dev: 0.953947 vs baseline 0.947368 (+0.0066).
- Held-out confirmation: 0.958333 vs baseline 0.933333 (+0.025), Hit@5
  perfect on both CLI and MCP surfaces, adoption gates 39/39.

The margin over the quantized Q variant is within noise on dev (0.9539 vs
0.9496 ≈ one query-rank swap across 38 queries), but Gemma is the only
candidate with a confirmed held-out gain over the fp32 baseline, so it wins
the quality criterion outright. Q's result establishes quality parity at a
~4x smaller download and ~3x lower p95 latency, which keeps it as the
footprint fallback if Gemma's costs prove prohibitive.

### Trade-offs recorded for the ship decision

- **Latency**: Gemma p95 is 4475 ms vs 3323 ms (baseline) and 1386 ms (Q) —
  roughly +35% over fp32 nomic per query.
- **Download**: ~650 MB fp32; the ~200 MB Q4 build requires fastembed ≥5.17,
  which is out of scope under the pinned 5.2.0 dependency.
- **Vector-space compatibility**: dimensions match (768) but spaces do not;
  adopting Gemma as default requires re-embedding any existing corpus.

## Scope notes

- This evaluation does not change the shipped default model; switching the
  default and re-embedding production corpora is explicitly follow-up work.
- fastembed stayed pinned at 5.2.0 throughout.
- Held-out sets were touched exactly once (dev winner only), preserving them
  for future evaluations.
