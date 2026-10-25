# Mnemosyne level-up — recommendation

Date: 2026-10-18. Decision brief for adding temporal-graph, consolidation/forgetting/
importance, local-first operation and a defensible embedding model to
`mnemosyne-hermes`.

## Verdict

**Adopt none of the three as a runtime memory backend. The engine is the backend.**
The pinned `mnemosyne-memory 3.15.1` already ships a temporal graph, typed
forgetting, consolidation, 0–1 importance and keyless local embeddings, and the
offline proof passes 7/7 with zero network attempts
([0_baseline_and_premises.md](./0_baseline_and_premises.md),
[D_offline_smoke.md](./D_offline_smoke.md)). Swapping in a second store would
regress the contracts this repo exists to keep (single store, single worker,
capture containment, fail-loud, `hermes backup`).

What is left to integrate is small and surgical:

| Role | Candidate | What to take | Why |
|---|---|---|---|
| **Primary integration target** | `remete618/widemem-ai` | **Algorithm donor**: the ~120-LOC deterministic conflict-resolver guards and the per-model confidence/trust-verdict layer | Only candidate whose delta maps to a real engine gap and is pure Python, dependency-free, and portable without a second store |
| **Runner-up** | `zjunlp/MemBase` | **Measurement layer**: the embedding-evaluation protocol, kept as a dev-only bench gate | It is a benchmark, not a memory layer; it is the instrument that settled the embedding decision and should keep settling it |
| **Do not adopt** | `Ardha-Eco-System/synapse` | Donor-only at most (pattern completion, RIF) | FalkorDB + Graphiti + an LLM per batch + default-on telemetry, against a keyless single-store provider; its "9 algorithms wired" claim does not hold in code |

### Candidate metadata (verified against the GitHub API, 2026-10-18)

| | synapse | widemem-ai | MemBase |
|---|---|---|---|
| Repo | `Ardha-Eco-System/synapse` (brief said "ardhaecosystem" — redirects) | `remete618/widemem-ai` | `zjunlp/MemBase` |
| Stars | **76** | **49** | **50** |
| Last push | 2026-10-14 | 2026-10-17 | 2026-09-03 |
| License | MIT | Apache-2.0 | MIT |
| Language | Python | Python | Python (>=3.12) |
| `has_issues` | true | true | true |
| Self-host story | FalkorDB **server** (Docker/Linux) + `graphiti-core` + OpenAI-compatible LLM; Graphiti telemetry **on by default** (needs `GRAPHITI_TELEMETRY_ENABLED=false`); compose publishes 6379 unauthenticated | Fully local stack: Ollama `llama3.1:8b` + sentence-transformers MiniLM + FAISS + SQLite; read path offline-proven, **write path LLM-gated**; 905 MB dependency tree | Benchmark framework only: loads LoCoMo/LongMemEval; stage 3 needs an LLM/judge |
| Verdict | **Reject as backend**, donor-only | **Primary — algorithm donor** | **Runner-up — measurement layer** |

## Why this is the answer, not "pick a memory layer"

The brief's framing assumes the provider lacks temporal KG, consolidation/forgetting
and local-first operation. The tree says otherwise:

- **Temporal KG:** `TripleStore` with `valid_from`/`valid_until`/`as_of`/`end`, plus
  `EpisodicGraph` (`gists`, `facts`, `graph_edges`) and `temporal_parser`
  (`core/triples.py:113`, `core/episodic_graph.py:87`, `core/temporal_parser.py:357`).
- **Consolidation / forgetting / importance:** `VeracityConsolidator` +
  `consolidate_to_episodic`; typed Weibull decay over **21** memory types
  (`DEFAULT_HALFLIFE_HOURS=168`); importance is a first-class **0–1 float** blended
  into recall (`core/weibull.py`, `core/veracity_consolidation.py:270`,
  `core/beam.py:5859`).
- **Local-first:** fastembed ONNX + SQLite, no key, no server, no API key
  (`core/embeddings.py:70`); proven offline under a socket guard.
- **Native tool surface already exists:** 40 provider tools including
  `mnemosyne_triple_*`, `mnemosyne_persona_*`, `mnemosyne_canonical_*`
  (`docs/HERMES_INTEGRATION.md#canonical-tool-names-and-default-exposure`).

The brief's load-bearing premises were therefore stale, and the workstreams
corrected them with evidence rather than deferring to them:

| Brief said | Reality |
|---|---|
| Rust backend / Claude Code MCP contract | Python provider, provider id `mnemosyne`, full native Hermes surface; Rust retired at 3.0.0 |
| importance stored 0–1 floats is a mismatch | The **engine** is 0–1 float (matches the live stack); the separate **lite** store is 0–10 int |
| `ci.yml` has conflict markers on main | No `ci.yml`; zero conflict markers in the worktree or on `origin/main` |
| capture containment hardened 2026-09-13 | No such date/term; the behaviours exist and are enumerated as constraints to preserve (`default_scope`, `sync_roles`, `skip_contexts`, raw-capture opt-in, 8,000-char ceiling, single worker) |
| `pin()` is LLM-free (workstream B premise) | False in widemem 2.0.2 — `pin()` routes through the extractor (`core/memory.py:512`) |
| synapse has 9 wired hippocampus algorithms | 3 affect live calls; 4 are compute-only/report-only; 2 have no call site |

## Embedding reindex decision — **APPROVED (staged)**

`nomic-embed-text-v1.5` (768-dim) beats `bge-small-en-v1.5` (384-dim) on the same
120 paired LoCoMo queries and 2,080 memory units, with paired-bootstrap 95% CIs
that exclude zero ([C_embeddings.md](./C_embeddings.md)):

| metric | bge-small | nomic | delta (95% CI) |
|---|---:|---:|---:|
| recall@10 | 0.571 | **0.681** | +0.110 (+0.051 … +0.173) |
| MRR | 0.438 | **0.545** | +0.107 (+0.042 … +0.174) |
| nDCG@10 | 0.446 | **0.550** | +0.104 (+0.053 … +0.156) |

Cost on this CPU box: reindex 57 s → 209 s (3.6×), index 3.05 → 6.09 MB (2.0×),
query p50 31.7 → 68.5 ms, RSS 743 → 1,666 MB. **Approval is conditional**, because
the measurement used sentence-transformers/PyTorch while the engine loads ONNX
fastembed, and nomic's prefixes must be configured explicitly:

```bash
MNEMOSYNE_EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5 \
MNEMOSYNE_EMBEDDING_DIM=768 \
MNEMOSYNE_EMBEDDING_QUERY_PREFIX='search_query: ' \
MNEMOSYNE_EMBEDDING_DOC_PREFIX='search_document: ' \
mnemosyne reindex --yes        # dry-run first; backup is taken by default
```

Ship only after a staging reindex + engine-native retrieval A/B on the the host.
Rollback is the symmetric reindex to `BAAI/bge-small-en-v1.5` / `..._DIM=384`.
Do not mix dimensions in one store; `..._DIM=768` is mandatory because the engine's
dimension table has no nomic entry.

**Validated by the consumer.** The production agent was consulted read-only over
its own API and independently ranked the gaps: **confidence/abstention #1,
temporal validity #2, consolidation #3**, with multi-hop graph work *deferred*
because its graph holds only 13 edges. That confirms the donor choice and added a
temporal slice to the plan. See [E_agent_consultation.md](./E_agent_consultation.md).

## Evidence pack

| Doc | Contents |
|---|---|
| [0_baseline_and_premises.md](./0_baseline_and_premises.md) | Premise audit, engine capability table, environment feasibility matrix |
| [A_synapse.md](./A_synapse.md) | Provider-contract audit, 9-algorithm mapping with file:line and wiring status, host runbook, conflicts |
| [B_widemem.md](./B_widemem.md) | Install/versions, socket-guarded offline proof, measured confidence, resolver guards, BLOCKED items |
| [C_embeddings.md](./C_embeddings.md) / [C_embeddings.json](./C_embeddings.json) | Benchmark methodology, numbers, 95% CIs, engine reindex path |
| [D_offline_smoke.md](./D_offline_smoke.md) | Executed offline proof (7/7 PASS); live provider state and the passing memory round-trip |
| [E_agent_consultation.md](./E_agent_consultation.md) | Live agent's ranked gaps, critique of the plan, 5 acceptance tests, production ops findings |
| [LIVE_CHAT.md](./LIVE_CHAT.md) | The working live channel: transports, raw response shapes, pitfalls, usage |
| [SPIKE_PLAN.md](./SPIKE_PLAN.md) | Sprint 0 prerequisite + 3 sprints, milestones, rollback criteria, acceptance gates |

## Blockers, flagged plainly

1. **No the host shell / TrueNAS API access.** Synapse was not stood up (no Docker on
   the dev box); the host runbook is written but **unexecuted**. All GPU and
   Quadro P4000 / RTX 3090 numbers are **not measured**.
2. **No Ollama or paid API here.** widemem `add()`/`pin()` and batch conflict
   resolution, and MemBase stage 3 (F1/BLEU/LLM-judge), are **BLOCKED** with the
   exact failing commands; no scores were synthesized.
3. **Live gateway — verified end-to-end.** The deployed gateway (Hermes **v0.22.1**)
   runs the provider `mnemosyne` **active/ready** with the expected pin, DB path,
   `default_scope=session`, and the canonical four tools. A real agent-turn
   memory round-trip passed on **both** transports: remember `zephyr-quokka-42`
   in one session, then recall it from a fresh session via OpenAI
   `/v1/chat/completions` + `X-Hermes-Session-Key` **and** REST
   `/api/sessions` + `/chat`. Details and raw shapes:
   [D_offline_smoke.md](./D_offline_smoke.md) §6. Remaining nit: the dashboard's
   `/api/config` reports a different `API_SERVER_KEY` than the gateway's actual
   `.env` value — a config-view inconsistency, not a runtime failure.
4. **The workspace ships a different scored objective.** `task/PROBLEM.md` +
   `task/score.py` (and `.dream-rsi/task.json`) define a write-throughput RSI task
   on `src/mnemosyne_lite/storage.py`, 30-minute budget. It was **not** touched;
   this brief is a different task. Say so if it was meant to run instead.
5. **Repo state:** local `main` is 1 commit behind `origin/main`
   (`d29625c Add memory-writer evaluation harness (#23)`), fast-forward available,
   not applied.

## Next actions (in order)

1. **Approve the reordered sprint plan** in [SPIKE_PLAN.md](./SPIKE_PLAN.md):
   Sprint 0 (writer serialization + telemetry) → Sprint 1 (confidence +
   abstention, flag-off, kill switch) → Sprint 2 (temporal validity + resolver
   guards) → Sprint 3 (engine-native embedding gate + acceptance suite).
2. **Production ops items needing owner approval** (from the live consult, not
   code): remove the orphan MCP server process (PID 91185, old gateway);
   `hygiene_clean` the **30 `secret_assignment` findings after a backup** and add a
   capture-time secret denylist; consolidate the **535-row** backlog; calibrate
   `MNEMOSYNE_RECALL_SCORE_FLOOR` before enabling it.
3. **Embedding reindex**, staged: engine-native A/B on the real corpus first
   (bge-small retained as rollback), then production reindex only if it wins.
4. Optional upstream reports (both repos have `has_issues: true`, no PATCH needed):
   engine cp1252 legacy-config warning; synapse's install trap (documented install
   never creates the discoverable plugin directory).
