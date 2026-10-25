# Level-up baseline: premise audit and engine capability evidence

Status: Lead recon, 2026-10-18. Every claim below is tied to a file, a command, or a
command's output. Where the 2026-08-24 brief and this tree disagree, the tree wins
and the disagreement is recorded rather than smoothed over.

Machine used for this audit: Windows 11 workstation, Python 3.11.15 (Hermes venv),
`mnemosyne-memory` **3.15.1** installed, Hermes **v0.18.2**. No Docker, no Ollama,
no TrueNAS access. NVIDIA RTX 3060 Laptop 6 GB present. See
[environment](./D_offline_smoke.md) for what that permits and forbids.

---

## 1. Premise audit — the brief versus this repository

| # | Brief claim (2026-08-24 / task prompt) | Verified reality | Evidence |
|---|---|---|---|
| P1 | "mnemosyne-hermes, a **Rust** memory backend" | The Rust product was retired at the 3.0.0 pivot. This repo ships a Python Hermes provider plus a SQLite lite surface. No `Cargo.toml` exists in the tree. | `AGENTS.md` "The Rust product that used to live here is retired"; `CHANGELOG.md` "3.0.0 is the pivot release"; `Get-ChildItem -Recurse -Filter Cargo.toml` → none |
| P2 | "built for the **Claude Code MCP contract**, not Hermes' native tool surface (`mnemosyne.recall`, `mnemosyne.remember`, `mnemosyne.persona`, `mnemosyne.canonical`, `mnemosyne.triple`)" | False as stated. The provider **is** the vendored `hermes_memory_provider` from `mnemosyne-memory 3.15.1` and exposes a 40-tool native surface, including `mnemosyne_triple_add/query/end`, `mnemosyne_persona_promote/demote/list/reinforce`, `mnemosyne_remember_canonical/recall_canonical/forget_canonical`. Four core tools are exposed by default; the other 36 are opt-in. The real nuance is naming (`mnemosyne_remember`, not `mnemosyne.remember`) and default exposure, not architecture. | `docs/HERMES_INTEGRATION.md#canonical-tool-names-and-default-exposure` (table, lines 141–182); `integrations/hermes-provider/README.md`; `plugin.yaml` (`name: mnemosyne`) |
| P3 | "Data model mismatch: importance is stored as **0–1 floats**" | Direction inverted. The **live engine** stores importance as a 0.0–1.0 float; the **lite store** stores it as an INTEGER with `CHECK(importance >= 0 AND importance <= 10)` and `min_importance` is an int. The provider is engine-backed, so the provider side already matches the live stack. There is a real scale split — but the 0–10 side is the lite surface, not the product provider. | engine: `mnemosyne/core/beam.py:3262` "importance: 0.0-1.0 relevance score"; lite: `src/mnemosyne_lite/storage.py:115` and `:1033`/`:1049` |
| P4 | "`ci.yml` has **conflict markers** on main" | No `ci.yml` exists. Workflows are `.github/workflows/python-ci.yml` and `upstream-drift.yml`. Zero conflict markers in the worktree **and** on `origin/main`. Local `main` is 0 ahead / 1 behind `origin/main` (fast-forward available). | `git grep -n -E '^(<<<<<<< |>>>>>>> )' -- .` → none; same against `origin/main` → none; `git ls-tree -r --name-only origin/main -- .github`; `git rev-list --left-right --count HEAD...origin/main` → `0 1` |
| P5 | "**Capture containment + personal-context lanes** hardened 2026-09-13 (don't regress)" | The literal strings and that date are not in this tree. The *behaviour* exists and is documented. Treat the constraint as: preserve automatic turn capture limited to user turns, context skipping, regex ignore patterns, raw tool/delegation capture remaining opt-out, the 8,000-character prefetch ceiling, and the single background consolidation worker. | `docs/HERMES_CONFIGURATION.md` lines 40–64 (`default_scope`, `sync_roles`, `skip_contexts`, `ignore_patterns`, `MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE=false`, `MNEMOSYNE_PREFETCH_TOTAL_CHARS=8000`); `PATCHES.md` P23; `integrations/hermes-provider/README.md#background-consolidation` |
| P6 | "Embedding reindex decision open: nomic-v1.5 vs bge-small, **no numbers yet**" | Partly stale. The engine default is already `BAAI/bge-small-en-v1.5` (384-dim) and `mnemosyne reindex` rebuilds the vector tables for the active model. An in-repo upstream record reports **98.9% Recall@All@5 on LongMemEval (April 2026) using bge-small-en-v1.5**. What is genuinely missing is a like-for-like nomic-v1.5 measurement on this corpus, not any measurement at all. | `mnemosyne/core/embeddings.py:70`; `mnemosyne reindex --help` output ("re-embed ... with 'BAAI/bge-small-en-v1.5' (dim 384)"); `integrations/hermes-provider/UPSTREAM_METADATA.txt:183`; `docs/archive/RUST_ARCHIVE_REF.md:40` "No nomic switch applied" |
| P7 | "live Python stack **mnemosyne-memory 3.15.1**" | Confirmed. `mnemosyne.__version__ == 3.15.1`; engine pin is `>=3.15.1,<3.16`. | `pip show mnemosyne-memory` → 3.15.1; `AGENTS.md` engine pin |
| P8 | "cron model: pin only after a real MODEL_OK verification call" | Not verifiable from this box (no `hermes cron` state inspected). Recorded as an operational rule, not tested. | — |

**Consequence for the workstreams.** P2 and P3 are the load-bearing corrections. If
P2 were true, the winning candidate would be "whatever fills the missing native
surface." It is false: the surface exists. If P3 were true, a schema migration would
be the first integration slice. It is inverted: the provider is already on the live
0–1 float scale, and the 0–10 store is the separate lite artifact. Both corrections
move weight away from "replace the provider" and toward "extend the engine/providers
where a measured gap remains."

---

## 2. What the engine already covers (so candidates are judged on delta, not overlap)

`mnemosyne-memory 3.15.1`, installed at
`%LOCALAPPDATA%\hermes\hermes-agent\venv\Lib\site-packages\mnemosyne`.

| Objective from the brief | Already in the engine | Evidence |
|---|---|---|
| **Temporal knowledge graph** | `TripleStore` with explicit single-current-truth semantics: `add(subject, predicate, object, valid_from, valid_until, supersede=...)`, `query(..., as_of=...)`, `end(...)`. Bitemporal by `valid_from`/`valid_until`; multi-valued predicates opt out of supersede. | `mnemosyne/core/triples.py:113` class docstring and `:142` `add` |
| | `EpisodicGraph` over SQLite: `gists` (episode summaries with participants/location/emotion/time_scope), `facts` (subject/predicate/object + timestamp + confidence), `graph_edges` (typed edges with weight), plus a **rule-based, zero-LLM** gist extractor. | `mnemosyne/core/episodic_graph.py:87`–`161`, `:165` `extract_gist` |
| | `temporal_parser`: `extract_temporal`, `extract_date_from_text`, `parse_nl_date`, `extract_temporal_span`. | `mnemosyne/core/temporal_parser.py:357`, `:393` |
| | Append-only `AnnotationStore` for multi-valued mentions, deliberately separate from the triple store. | `mnemosyne/core/triples.py:127` docstring |
| **Consolidation / forgetting / importance scoring** | Weibull forgetting per memory type: `weibull_decay_factor(age_hours, memory_type)` and `weibull_boost`, with **21** typed parameter sets (`DEFAULT_HALFLIFE_HOURS = 168.0`). | `mnemosyne/core/weibull.py` (`WEIBULL_PARAMS`, `weibull_decay_factor`) |
| | `VeracityConsolidator`, `aggregate_veracity`, `clamp_veracity`, `compute_fact_id`; auto-sleep consolidation to episodic memory. | `mnemosyne/core/veracity_consolidation.py:270`, `:445`; `beam.py:4417` `consolidate_to_episodic` |
| | Importance as a first-class 0–1 float blended into recall: `relevance * kw_share + importance * iw + relevance² * 0.08`, weight configurable. | `mnemosyne/core/beam.py:1418`, `:5859`, `:6424` |
| | Hygiene/noise scoring and persona promotion threshold. | `mnemosyne/core/hygiene.py:73`; `mnemosyne/core/persona.py` (`MIN_IMPORTANCE_FOR_PROMOTION`) |
| **Local-first / offline** | `fastembed` ONNX embeddings run locally; default `BAAI/bge-small-en-v1.5` (384-dim); `sqlite-vec` vector tables; keyless design. An OpenAI-compatible endpoint is **optional**. | `mnemosyne/core/embeddings.py:70`, `:217`, `EMBEDDING_DIM`; `sqlite_vec` import OK; `AGENTS.md` "Keyless by design" |
| | Local ONNX model is already cached on this machine: `...\hermes\cache\fastembed\models--qdrant--bge-small-en-v1.5-onnx-q\...\model_optimized.onnx` (66,465,124 bytes). | `os.walk` of `_FASTEMBED_CACHE_DIR` |
| **Persona / canonical / profile** | `PersonaExtractor`, persona triggers, markdown renderer; `CanonicalStore` with remember/recall/forget canonical + slot history. | `mnemosyne/core/persona.py`, `mnemosyne/core/canonical.py` |
| **Typed memory + decay-aware types** | `MemoryType` enum and `typed_memory.py`; Weibull parameters are keyed by those types. | `mnemosyne/core/beam.py` (`MemoryType`), `typed_memory.py` |

### Provider surface actually shipped

- Provider id `mnemosyne`, a byte-identical vendored snapshot of
  `hermes_memory_provider` from the pinned engine, plus audited local patches
  (`integrations/hermes-provider/README.md`, `PATCHES.md`).
- **40 tools** total; **4** default (`mnemosyne_remember`, `mnemosyne_recall`,
  `mnemosyne_stats`, `mnemosyne_forget`); `memory.mnemosyne.tools: ["*"]` opts into
  the rest, including triples, canonical profile, persona, graph traversal, sync,
  scratchpad, batch, pending approval and diagnostics.
- Hooks: `on_pre_compress`, `on_session_switch`, `on_delegation` (`plugin.yaml`).
- Bounded automatic context: 8,000-character ceiling, identity priority, truncation
  marking, raw tool/delegation capture opt-in only.
- One background consolidation worker per provider; session-end reuses it.

---

## 3. What is actually open (the real delta the candidates must fill)

1. **A like-for-like embedding measurement.** The engine ships bge-small and has a
   historical LongMemEval recall figure, but there is no in-tree A/B for
   `nomic-embed-text-v1.5`. A 384→768 switch also forces `mnemosyne reindex` over
   working + episodic vectors, so the decision must carry index cost, not just
   recall. Candidate C (MemBase) is the only one of the three that addresses this.
2. **Confidence-gated retrieval output.** The engine returns scores and has a
   relative score floor, but there is no HIGH/MODERATE/LOW/NONE abstention contract
   surfaced to the agent. Candidate B (widemem-ai) has that shape.
3. **Contradiction reconciliation taxonomy at the write path.** The engine has an
   `llm_conflict_detector` and veracity consolidation, but not widemem's
   ADD/UPDATE/DELETE/no-op batch decision with YMYL floors. Candidate B again.
4. **Multi-hop / graph reasoning.** Edges and a `mnemosyne_graph_query` tool exist;
   the gap is a *reasoning* layer over them (path queries, episode→fact
   consolidation), which is candidate A's (synapse) stated value.

---

## 4. Repo state deltas found during recon

- `origin/main` is **one commit ahead** of local `main`: `d29625c Add memory-writer
  evaluation harness (#23)` (adds `bench/memory_writer_eval.py`, fixture JSON,
  `docs/HY_MEMORY_RESEARCH.md`, tests, and a `checks.sh` gate). Fast-forward
  available; not applied by this work.
- The tree ships a **Dream-RSI write-throughput harness** that is *not* the
  objective in this brief: `task/PROBLEM.md` + `task/score.py` score
  `write_5000_p50_ms` for `src/mnemosyne_lite/storage.py`, with a 30-minute budget
  and three anti-cheat gates. `.dream-rsi/task.json` names it
  `mnemosyne_lite_write_throughput`; its active campaign is `complete`. It was left
  untouched. If the intent was to run that objective, say so explicitly — it is a
  different task from this evaluation.
- `docs/HY_MEMORY_RESEARCH.md` (on `origin/main`) already states the integration
  constraint this evaluation must respect: the provider has bounded turn capture,
  explicit remember/update/forget, approval staging, owner/scope checks, provenance,
  canonical history, and a single-flight consolidation worker — **"Do not add a
  second extractor, reconciler, store or worker."** That sentence is the bar any
  candidate integration has to clear.

---

## 5. Environment feasibility matrix

| Capability | This box | TrueNAS SCALE | Consequence |
|---|---|---|---|
| Docker / FalkorDB | **absent** | present (Custom Apps) | Synapse cannot be stood up here; live stand-up is a the host step. Source audit only. |
| Ollama / local LLM | **absent** | expected present | widemem `add()`/conflict resolution and MemBase Stage-3 QA are LLM-gated: blocked here, runnable on the host. |
| CUDA GPU | RTX 3060 Laptop 6 GB | Quadro P4000 / dual RTX 3090 | Do not bake CUDA into the decision; CPU-only results are the portable ones. |
| HF / PyPI network | available | network-independent runtime target | Model/dataset fetch is a build-time step; runtime must pass with `HF_HUB_OFFLINE=1` + socket blocked. |
| Engine + provider | installed, 3.15.1 | same pin | Offline smoke test of the shipped memory path is executable here. |
| the host access | **none stated** | owner-operated | Any host-only number in this pack is labelled "not measured". |

See [D_offline_smoke.md](./D_offline_smoke.md) for the executed offline proof.
