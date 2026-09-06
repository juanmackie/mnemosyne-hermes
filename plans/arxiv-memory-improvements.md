# Steal from arXiv: what's actually missing in Mnemosyne

## Context

Question: what can we take/use/copy/implement from arxiv to improve the memory system.
Most of the famous agent-memory papers (Mem0, MemoryBank, Reflexion, Generative Agents)
describe mechanisms Mnemosyne **already has**, in better or equivalent form:

| Paper idea | Already in Mnemosyne |
|---|---|
| MemoryBank Ebbinghaus forgetting curve (2305.10250) | `src/utils/hotness.rs` — sigmoid(log1p(uses)) × exp decay, half-life |
| Mem0 ADD/UPDATE/DELETE consolidation (2504.19413) | `src/evolution/consolidation.rs` — vector >0.90 + keyword check + LLM merge/supersede |
| Reflexion verbal reinforcement (2303.11366) | outcome-aware reasoning memories (`src/reasoning.rs`) |
| Generative Agents recency×importance×relevance (2304.03442) | hotness × semantic-similarity blend + `src/evolution/importance.rs` |
| Sleep-time consolidation (2504.13171) | `src/evolution/scheduler.rs` jobs |

So: **not much wholesale** — but two papers describe things we genuinely don't do.

## Worth taking (2 items)

### 1. HippoRAG — Personalized PageRank retrieval over the link graph (arxiv 2405.14831)

We have a weighted link graph with traversal-tracked strengths (`src/evolution/links.rs`)
but retrieval ranks by vector similarity + hotness only. Multi-hop recall ("the thing
linked to the thing I asked about") is dead weight.

Steal the retrieval mechanism only (skip the NPMI/OpenIE KG construction — our
links already exist and are LLM-curated, cheaper and better than auto-extracted triples):
- Seed nodes = top-k retrieved memories for the query
- Run Personalized PageRank seeded at those nodes over the memory-link graph
- Rank results by PPR score, blend with similarity

Small, self-contained: a PPR power-iteration over an adjacency query in
`LibsqlStorage`. ~150 lines + tests. Biggest quality win per line.

### 2. A-MEM — prospective memory evolution (arxiv 2502.12110)

Our consolidation only runs as a **periodic batch job**. A-MEM's trick: at *insert*
time, find the k nearest existing memories and have the LLM (a) propose links between
new and old, (b) optionally update attributes/summaries of the old ones. The graph
gets denser and fresher immediately instead of waiting for the scheduler.

We have all the pieces: nearest-neighbor query, `memory_link.rs`, `LlmService`,
link decay that already prunes bad auto-links (this de-risks junk links — they rot
themselves). Steal: a fast-path post-insert hook that does (a) always, (b) rarely
behind a config flag (Haiku call per insert; gate on cost).

## Not taking

- Letta/MemGPT core-memory blocks — agent-prompt architecture, not storage; conflicts with Claude Code owning the prompt.
- HippoRAG's NPMI knowledge-graph construction — replaces good LLM links with noisy auto-triples.
- Mem0's hosted product / vector-DB plumbing — already have LibSQL + FTS5 + quantized vectors.
- Survey taxonomies (2605.06716, 2512.13564) — reading list, not code.

## Files to modify

- `src/storage/libsql/…` — adjacency query (`links BETWEEN memory_ids`, with strengths)
- `src/services/…` (retrieval/ranking path) — PPR blend step
- `src/artifacts/memory_link.rs` — post-insert link-proposal hook
- `src/evolution/config.rs` / config — flags: `retrieval.ppr`, `evolution.on_insert`
- `Cargo.toml` — nothing new; power iteration is 10 lines of stdlib math

## Reuse

- `src/utils/hotness.rs` — keep as similarity blend, PPR is a third signal
- `src/evolution/links.rs` — traversal tracking doubles as PPR edge weights
- `src/evolution/consolidation.rs` — LLM-prompt style for the insert-time linker
- existing kNN vector query used by consolidation

## Steps

- [ ] Add storage query: weighted adjacency for a set of node ids (depth ≤ 2 cap)
- [ ] Implement PPR (damped power iteration, ~20 iters, stdlib only) + unit test on a toy graph
- [ ] Blend PPR score into retrieval ranking behind config flag; measure vs. baseline
- [ ] Add post-insert evolution hook: kNN → Haiku "link or not + why" → write links (user_created=false)
- [ ] Test: insert-similar-memories scenario produces cross-links without scheduler run
- [ ] Bench: confirm PPR step keeps p95 < 200ms target

## Verification

- Unit tests for PPR (known toy graph, seed-adjacent nodes rank highest)
- Existing `cargo test` suite green; new retrieval test with multi-hop fixture
- Manual: `mnemosyne search` on a seeded graph shows 2-hop memories ranked up
- Latency: existing benches show PPR blend < 10ms at 10k memories
