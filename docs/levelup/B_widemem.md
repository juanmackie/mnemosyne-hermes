# B. `remete618/widemem-ai` as a local-first, fully offline memory layer

Workstream B evaluation for `mnemosyne-hermes`. Environment: Windows, CPU-only
(`torch 2.14.1+cpu`, `torch.cuda.is_available() == False`, `device_count 0`),
Python 3.11.15, uv 0.11.28. All scratch evidence lives under
`.levelup-work/B-widemem/` (`logs/`, `scratch/`, `data/`, `hf/`, `repo/`).
Nothing outside this doc was written into the repo.

## Verdict

**widemem-ai is not a drop-in local-first memory layer for this repo, and the
"fully offline" premise is only half true.** The *read* path (search, confidence,
history, export, count, delete) and one *write* path (`import_json`) are genuinely
offline once `all-MiniLM-L6-v2` is cached — proven with every socket constructor
patched to raise. But `add()` **and `pin()` are LLM-gated**: both route through
`LLMExtractor` and fail with `ExtractionError ... Failed to connect to Ollama`
when no LLM is reachable. The brief's claim that `pin()` "stores a fact directly
... with no LLM call" is **false for 2.0.2** and is corrected in §0.

The best use of this project is **algorithm donor, not dependency**: port the
deterministic conflict-resolution guards (§4.2) and the per-model
confidence/trust-verdict pattern (§3) into the engine/provider. Vendoring it as a
backend would add a 905 MB dependency tree, a second store, and a second LLM
runtime, and would still leave the write path unusable on this machine.

## 0. Corrected premises

The brief (and `task-2` description) contains stale or wrong premises. Measurement
replaces them:

| Brief premise | Measured fact |
| --- | --- |
| "`pin()` stores a fact directly (importance 9.0 default) with no LLM call." | **False.** `widemem/core/memory.py:496-534` calls `self.pipeline.process(...)` (line 512), which calls `self.extractor.extract(text)` (`core/pipeline.py:95`), which calls `self.llm.generate_json(...)` (`extraction/llm_extractor.py:37`). Offline this raises `ExtractionError` — see §2.3. Only `import_json` (`core/memory.py:678-758`) writes without an LLM. |
| "The brief's Rust premise" | Engineering here is Python: the product is a vendored Python provider (`integrations/hermes-provider/`, provider id `mnemosyne`). Portability is a Python seam question, not a Rust question. |
| "fully offline" with `[local]` | The package is local-first (Ollama + MiniLM + FAISS), **not LLM-free**. Every default write path needs a local Ollama `llama3.1:8b`. |
| `widemem.mcp_server` ships in `[local]` | False. `[local]` = faiss-cpu, ollama, sentence-transformers, urllib3. `mcp` is the separate `[mcp]` extra. `import widemem.mcp_server` → `ModuleNotFoundError: No module named 'mcp'`. The HTTP server needs `[server]` (fastapi): `ModuleNotFoundError: No module named 'fastapi'`. |
| Repo HEAD == PyPI 2.0.2 | Repo HEAD `ddae795` (2026-10-09) still declares `version = "2.0.1"` and differs from the 2.0.2 wheel in `core/memory.py`, `core/types.py`, `mcp_server.py`. The files that carry this report's algorithm claims (`conflict/batch_resolver.py`, `retrieval/uncertainty.py`, `core/pipeline.py`, `hierarchy/manager.py`) are **byte-identical** between HEAD and the installed wheel (normalized for CRLF). Citations below point at HEAD paths; the algorithm text applies to the wheel. |

## 1. Install and versions

```powershell
uv venv --clear --python 3.11 .levelup-work/B-widemem/.venv
uv pip install --python .levelup-work/B-widemem/.venv/Scripts/python.exe "widemem-ai[local]"
```

Install log (`.levelup-work/B-widemem/logs/install.log`):

```
Resolved 50 packages in 2.98s
Prepared 30 packages in 2m 03s
Installed 50 packages in 27.18s
 + faiss-cpu==1.15.1
 + huggingface-hub==1.33.0
 + numpy==2.4.6
 + ollama==0.6.3
 + pydantic==2.14.1
 + scikit-learn==1.9.1
 + scipy==1.17.1
 + sentence-transformers==6.1.0
 + torch==2.14.1
 + transformers==5.20.1
 + urllib3==2.8.0
 + widemem-ai==2.0.2
```

| Component | Resolved version | Source |
| --- | --- | --- |
| `widemem-ai` | **2.0.2** | `uv pip freeze` |
| `faiss-cpu` | 1.15.1 | `uv pip freeze` |
| `sentence-transformers` | 6.1.0 | `uv pip freeze` |
| `transformers` | 5.20.1 | `uv pip freeze` |
| `torch` | 2.14.1 (**+cpu**) | `import torch; torch.__version__` |
| `numpy` | 2.4.6 | `uv pip freeze` |
| `pydantic` | 2.14.1 | `uv pip freeze` |
| `ollama` (client) | 0.6.3 | `uv pip freeze` |
| `huggingface-hub` | 1.33.0 | `uv pip freeze` |
| Embedding model | `all-MiniLM-L6-v2`, 384 dim | `[init] embedder model = all-MiniLM-L6-v2`, `dimensions = 384` |
| HF model cache | **87.4 MB** in `.levelup-work/B-widemem/hf/hub` | directory size after phase 1 |
| Venv footprint | **905 MB / 26,994 files** (torch alone 477.1 MB) | `Get-ChildItem .venv -Recurse` |
| Python | 3.11.15 | `.venv\Scripts\python.exe -VV` |

Test tooling was added on top for §4.4/§5: `pytest==9.1.1`, `pytest-asyncio==1.4.0`.

## 2. What is actually offline-capable (the central finding)

### 2.1 Method

Two runs, same corpus, same dedicated HF cache:

1. **Phase 1 (online, once):** `HF_HOME=.levelup-work/B-widemem/hf` — downloads and
   caches `all-MiniLM-L6-v2` (87.4 MB), then exercises the LLM-free surface.
2. **Phase 2 (offline proof):** `HF_HOME=<same dir> HF_HUB_OFFLINE=1
   TRANSFORMERS_OFFLINE=1`, and — before `widemem`/`torch`/`httpx` are imported —
   the script replaces `socket.socket.connect`, `socket.socket.connect_ex`,
   `socket.create_connection` and `socket.getaddrinfo` with functions that raise.
   Any outbound attempt fails loudly and is counted.

Phase-2 command:

```powershell
$env:HF_HOME           = "~\mnemosyne-hermes\.levelup-work\B-widemem\hf"
$env:HF_HUB_OFFLINE    = "1"
$env:TRANSFORMERS_OFFLINE = "1"
.\.levelup-work\B-widemem\.venv\Scripts\python.exe -u .levelup-work\B-widemem\scratch\phase2_offline.py
```

### 2.2 Raw offline output (quoted, `.levelup-work/B-widemem/logs/phase2.log`)

```
[proof] socket.socket.connect, connect_ex, create_connection, getaddrinfo all raise
[env] HF_HOME = ~\.levelup-work\B-widemem\hf
[env] HF_HUB_OFFLINE = 1
[env] TRANSFORMERS_OFFLINE = 1
[init] WideMemory constructed offline in 13.34s
[init] embedder = SentenceTransformerEmbedder model = all-MiniLM-L6-v2 dimensions = 384
[init] vector_store = FAISSVectorStore
[init] llm = OllamaLLM
[write] import_json imported=9 in 0.60s
[count] total = 9 alice = 5 bob = 4
...
[get] alice-1 = Lives in Berlin
[history] alice-1 entries = 1 -> [('add', 'Lives in Berlin')]
[summarize] facts = 9 summaries returned = 0 (threshold is 10)
[summarize] alice-only scoped call returned 0 (5 facts < 10)
[export] memories=9 bytes=2916 -> data/phase2-export.json
[entities] backfill_entities updated=9 in 31.8ms
[delete] after delete: total = 8 bob = 3 history entries = 3
...
[proof] outbound attempts denied during whole run: 9
  - socket.create_connection: (None,)
  ...
[phase2] done
```

**Interpretation.** No outbound call succeeded: the only 9 attempts in the whole
run were denied by the patched constructors, and all 9 are the Ollama retry path
from the deliberately-failing `pin()`/`add()` block in §2.3. Everything above them
— model load from the local HF cache, embedding, FAISS insert/search, SQLite
history, summarization no-op, export, backfill, delete — ran with sockets
unavailable. `import_json` is therefore a real offline *write* path, and the whole
read path is offline.

### 2.3 LLM-gated paths: exact failures (BLOCKED)

From the phase-2 log:

```
[BLOCKED] pin() offline attempt:
  widemem.core.exceptions.ExtractionError: Fact extraction failed: Failed after 3 retries: Failed to connect to Ollama. Please check that Ollama is downloaded, running and accessible. https://ollama.com/download
[BLOCKED] add() offline attempt:
  widemem.core.exceptions.ExtractionError: Fact extraction failed: Failed after 3 retries: Failed to connect to Ollama. Please check that Ollama is downloaded, running and accessible. https://ollama.com/download
[BLOCKED] resolver.resolve() single LLM call attempt:
Conflict resolver LLM call failed (Failed after 3 retries: Failed to connect to Ollama. ...), falling back to ADD with dedup
  resolver SUCCEEDED
```

Phase 1 (network allowed, Ollama still absent) produces the same failure, so this
is *Ollama missing*, not *network blocked*:

```
[LLM-gated] pin() attempt:
  widemem.core.exceptions.ExtractionError: Fact extraction failed: Failed after 3 retries: Failed to connect to Ollama. ...
```

Machine check:

```
=== ollama command ===            (no output: not on PATH)
=== port 11434 ===               TcpTestSucceeded : False
ollama.exe (LocalAppData/Program Files) -> False
=== docker ===                   (no output: not installed)
```

Why this matters: `pin()` reads like a bypass ("no extraction") but line 512 calls
`pipeline.process()`, which always runs the extractor. The only LLM-free write
APIs are `import_json`, `delete`, and `backfill_entities` — i.e. you can *restore*
a store offline, but you cannot *learn* anything offline.

**Operational consequence:** a widemem deployment is not "$0/offline" in this
repo's sense; it needs a local Ollama server with `llama3.1:8b` (model download
size not measured — Ollama is absent on this box) plus a ~87 MB embedding model,
on top of the ~905 MB Python stack.

## 3. Measured behaviour: search, confidence, latency

Both phases were executed; numbers below are from `logs/phase1.log` and
`logs/phase2.log` and are identical across phases (cosine similarities match to
the last digit), which is itself useful evidence that offline caching changes
nothing about ranking. 9 facts, 2 users, `top_k=5`, CPU-only, 6 timed iterations
per query after one warmup (first iteration dropped).

### 3.1 Confidence semantics from source

`widemem/retrieval/uncertainty.py:18-43`:

| Embedding model | high | moderate | low |
| --- | --- | --- | --- |
| `all-MiniLM-L6-v2` (`_MODEL_THRESHOLDS`) | 0.60 | **0.30** | 0.20 |
| anything else (OpenAI default table) | 0.60 | **0.50** | 0.30 |

`assess_confidence()` (`uncertainty.py:80-101`) maps the top result's raw
similarity to `HIGH >= high`, `MODERATE >= moderate`, `LOW >= low`, else `NONE`.
Model matching is case/space-insensitive on the last path segment
(`_model_key`, lines 48-51), so `sentence-transformers/all-MiniLM-L6-v2` and
`ALL-MINILM-L6-V2` hit the MiniLM table while `all-MiniLM-L12-v2` does not
(test file `tests/test_confidence_thresholds.py:40-48`). `WIDEMEM_CONFIDENCE_HIGH/
MODERATE/LOW` env vars override per-model values (lines 58-60), and a non-monotonic
override logs one warning instead of failing (lines 62-69).

The source is unusually honest about the limit (`uncertainty.py:31-42`): MiniLM
"cannot separate 'answer stored' from 'something about this person stored': the
name in the query dominates the score", precision drops from 0.771 to 0.745 when
`moderate` is lowered to 0.30, and hard negatives reach MODERATE at 12/12.

### 3.2 Measured confidence (phase 2, identical to phase 1)

| Kind | Query | Top-1 memory | raw similarity | Confidence |
| --- | --- | --- | --- | --- |
| clear | Where does alice live? | Lives in Berlin | `0.34023553133010864` | **MODERATE** |
| clear | What is alice allergic to? | Allergic to penicillin | `0.36659300327301025` | **MODERATE** |
| clear | What is the dog named Rufus a pet of? | Has a dog named Rufus | `0.9150575399398804` | **HIGH** |
| ambiguous | What does alice do for a living? | Works as a data engineer at Contoso | `0.21088159084320068` | **LOW** |
| ambiguous | Where does she live? | Lives in Berlin | `0.3944632112979889` | **MODERATE** |
| hard negative | Where does alice work out? | Lives in Berlin | `0.15459157526493073` | **NONE** |
| unrelated | What is the capital of Mongolia? | Runs five kilometres every morning | `0.16149011254310608` | **NONE** |
| unrelated | How do I bake sourdough bread? | **Works at a bakery** | `0.4357951879501343` | **MODERATE (false positive)** |

Two calibrated observations:

- A correct, subject-bearing query lands at **MODERATE**, not HIGH (0.34, 0.37).
  HIGH only appears when the query lexically restates the stored text
  ("dog named Rufus" → 0.915). This is exactly the "subjectless answer" behaviour
  the MiniLM table was built for, and it means a consumer must treat MODERATE as
  answerable — which `explain=True` does (next section).
- The sourdough query is a **live false positive**: "bakery" pulls "Works at a
  bakery" to 0.4358, above the 0.30 moderate line, so an unrelated question is
  reported MODERATE. This is the source's documented precision trade-off, now
  reproduced on this box. Any adoption must not treat MODERATE as ground truth.

### 3.3 Trust verdict (`explain=True`)

```
[explain] Where does alice live? -> {"answerable": true, "confidence": 0.34,
  "confidence_level": "moderate", "requires_review": false,
  "reason": "Moderate-confidence match from stored memory; answerable with normal caution."}
```

The pattern is the transferable asset: *ranked results plus a machine-readable
trust verdict* (`answerable`, `confidence_level`, `requires_review`, `reason`),
returned by the same `search()` call. This repo's provider surface currently has
no such verdict (no `confidence`/`answerable` hits in provider docs).

### 3.4 Latency (CPU, 9 facts, top_k=5)

| Phase | per-query median range | worst observed |
| --- | --- | --- |
| phase 1 (online) | `1.6` – `4.7` ms | 12.3 ms |
| phase 2 (offline, sockets blocked) | `1.7` – `4.2` ms | 10.9 ms |

Raw lines, e.g. phase 2: `latency_ms(min/median/max)=1.9/3.6/10.9` for
"Where does alice live?", `...=1.7/1.7/1.8` best case. Includes embedding +
FAISS search + scoring. Single-query, 9-fact store: this says nothing about
10⁴–10⁶-fact performance (not measured — no scale fixture was built).

## 4. Conflict-resolution semantics (source + tests)

### 4.1 The algorithm (from `widemem/conflict/batch_resolver.py`)

`MemoryPipeline.process()` (`core/pipeline.py:86-129`) does, in order:

1. `facts = extractor.extract(text)` — **LLM** turns free text into atomic facts.
2. `_find_existing()` — embed each fact, top-5 FAISS neighbours per fact, filtered
   by `user_id`/`agent_id`; each fact keeps its own "linked" candidate list.
3. `resolver.resolve(facts, existing, linked)` — **one batch LLM call**
   (`batch_resolver.py:96`) proposing an action per fact:
   `add | update | delete | none` with `target_id`, `importance`,
   `update_kind`, `contradicts`.
4. `_execute_actions()` (`pipeline.py:190-316`) applies the validated actions,
   with content-hash dedup, a scope guard (`_in_scope`, lines 318-346), history
   logging, and `created_at` preservation on updates.

The prompt contract (`conflict/prompts.py`) is explicit that ADD keeps details,
UPDATE means "entirely obsolete" (contradiction or full restatement), DELETE
requires invalidation, NONE must name the covering memory, and contradictions must
quote the contradicted text verbatim.

### 4.2 Deterministic guards — the valuable part

The LLM only *proposes*. Every proposal is re-validated in Python, and the
validators are conservative in one direction: **never lose a new fact, never
destroy old content without proof.**

| Guard | Code | Behaviour |
| --- | --- | --- |
| Unknown/malformed action, wrong `fact_index`, duplicate index | `batch_resolver.py:227-258` | dropped or coerced to ADD; missing fact indices are appended as ADD |
| UPDATE/DELETE with no target (flat mode) | `:271-274` | coerced to ADD |
| NONE that does not name a covering memory | `:326-335` | coerced to ADD, or to NONE only if a content-hash duplicate is found |
| UPDATE on a target outside the linked group | `:337-349` | falls back to the first linked candidate, or ADD |
| DELETE with a hallucinated target | `:350-355` | deletes nothing; ADD unless an exact duplicate exists |
| In-place UPDATE that would lose detail | `:276-294` + `_may_overwrite` `:359-378` | allowed only if new text **supersedes** old (`_supersedes`: old informative tokens ⊆ new tokens, `:30-36`), or a contradiction against an **atomic** old memory (≤ 6 informative tokens, `:39-43`) whose quoted text appears verbatim and covers ≥ 80% of it (`_quote_invalidates`, `:46-61`). Otherwise ADD — both facts are kept and ranking decides. |
| Silent drops in `_execute_actions` | `pipeline.py:204-210`, `:304-314` | NONE without target → ADD; justified NONE is logged to history so the skipped fact is traceable |

This is stricter than most "mem0-style" resolvers and is exactly the kind of
invariant that is cheap to transplant (pure Python, no new dependencies).

### 4.3 Failure behaviour observed offline

When the resolver's LLM call raises (`OSError`, `ConnectionError`, `TimeoutError`,
`RuntimeError`, `ProviderError`), `_fallback_add_with_dedup()`
(`batch_resolver.py:391-411`) logs and returns **ADD for every fact not already
stored** (content-hash dedup):
`Conflict resolver LLM call failed (...), falling back to ADD with dedup`, then the
resolution returns actions (observed in `logs/phase2.log`). So writes never crash
because the LLM is down — but **no conflict is resolved either**: offline, the
store can only grow. `pin()`/`add()` still fail earlier, at extraction, so this
fallback only matters for callers that inject their own extractor.

### 4.4 Test evidence, run against the installed 2.0.2 wheel

`tests/test_consolidation.py` uses a deterministic `CountingLLM` fake and a
`MockEmbedder` — **no Ollama, no network**. All five resolver tests pass:

| Test (file:line) | What it proves | Result |
| --- | --- | --- |
| `test_resolver_uses_linked_candidates_and_prompt_contract` (`tests/test_consolidation.py:96-143`) | one LLM call; ADD/UPDATE/DELETE/NONE mapped 1:1; target UUID mapping; prompt carries linked ids | PASSED |
| `test_resolver_invalid_update_degrades_to_none_when_unchanged` (`:146-166`) | bogus UPDATE target + identical content → NONE, keeps single row | PASSED |
| `test_resolver_invalid_delete_degrades_to_none` (`:169-189`) | bogus DELETE target → deletes nothing, dedups to NONE | PASSED |
| `test_resolver_valid_update_noops_when_content_matches` (`:192-212`) | update on identical content → NONE | PASSED |
| `test_pipeline_is_idempotent_for_same_input` (`:215-239`) | second identical add() stores nothing, `count()==1`, one LLM call | FAILED — teardown only (see §5) |
| `test_config_defaults_consolidation_off` (`:242-243`) | consolidation opt-in | PASSED |

**BLOCKED:** live batch conflict resolution through a real model — every branch
above that depends on the *model's choice* (contradiction vs ADD, which text it
quotes) is exercised only against the fake. `add()`/`pin()` end-to-end is BLOCKED
(no Ollama).

## 5. LLM-independent test suite run

Repo tests were copied byte-identically (sha256-verified) to
`.levelup-work/B-widemem/scratch/pkgtests/` and run there so `import widemem`
resolves to the installed wheel, not the repo source:

```
widemem 2.0.2
C:\...\.levelup-work\B-widemem\.venv\Lib\site-packages\widemem\__init__.py
...
======================= 98 passed, 8 skipped, 2 failed in 18.14s =======================
```

Files: `test_consolidation.py`, `test_confidence_thresholds.py`,
`test_local_defaults.py`, `test_frustration_confidence.py`,
`test_store_count_and_payload.py`, plus `conftest.py` and
`tests/fixtures/confidence_minilm.json`. Offline env vars were set for the run
(`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`); notably
`test_fixture_scores_match_the_live_model` (`test_confidence_thresholds.py:241-267`)
**PASSED** — it re-encodes the 62-query calibration fixture with the real model on
CPU and asserts every stored similarity matches within 0.01. So the repo's MiniLM
confidence calibration is reproducible on this machine, offline.

Both failures are environmental, not logic:

- `test_consolidation.py::test_pipeline_is_idempotent_for_same_input` —
  `PermissionError: [WinError 32] ... tmptvgg3vux\history.db` raised in
  `tempfile.TemporaryDirectory` cleanup, i.e. **after** the assertions (the
  traceback starts at `tempfile.py:947: in cleanup`). Windows cannot delete the
  SQLite file while `HistoryStore` still holds it; the test needs
  `close()`-before-cleanup discipline on Windows. The failure is teardown, not the
  idempotency contract.
- `test_local_defaults.py::test_explicit_openai_is_honoured` —
  `ProviderError: Install openai: pip install "widemem-ai[openai]"` (the `[local]`
  extra does not include `openai`; expected).

8 skips are the qdrant and pgvector tests, skipped by `pytest.importorskip(...)`
(`tests/test_store_count_and_payload.py:245`) because those optional extras are
not installed.

## 6. Integration assessment

### 6.1 What this repo's provider contract requires

From `AGENTS.md` and `integrations/hermes-provider/`:

- One provider id `mnemosyne`, one registration, one CLI command; no second entry
  point. `MnemosyneMemoryProvider(HermesPersonaPromptMixin, MemoryProvider)` lives
  in a 5,545-line `__init__.py` and implements the full Hermes in-process
  interface: `initialize()` (`:2301`), `prefetch()` (`:2641`), turn sync, tool
  schemas, config schema, audit, `recall_status()`.
- **Single background consolidation worker** per provider (auto-sleep), which owns
  its own SQLite connection; session-end reuses it with a bounded wait.
- **Single store**, with DB path precedence `memory.mnemosyne.db_path` >
  `MNEMOSYNE_DB_PATH` > engine default (`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` >
  `~/.hermes`).
- **Keyless / $0 / offline**, LLM work inherited from the active Hermes model via
  Hermes' local proxy — no second provider key, no second model runtime.
- Capture containment: session/author/channel ownership is provider-managed and
  audited; forget/update/invalidate write audit evidence.
- CPU/AMD compatibility (no CUDA assumption).

### 6.2 What widemem actually is

- An in-process **library**: `WideMemory` owning its own FAISS index +
  its own SQLite history DB + its own `~/.widemem` data dir (defaults:
  `history_db_path="~/.widemem/history.db"`, `extractions_db_path`,
  `WIDEMEM_DATA_PATH` for the MCP server).
- Its own extractor + resolver + **Ollama LLM default** (`llama3.1:8b`) and its own
  embedding stack (`sentence-transformers` + `torch`, 905 MB).
- Optional surfaces: `widemem.mcp_server` (7 tools: `widemem_add`, `widemem_search`,
  `widemem_delete`, `widemem_count`, `widemem_pin`, `widemem_export`,
  `widemem_health`; needs `[mcp]`) and a FastAPI `widemem.server` (needs
  `[server]`). Neither is in `[local]`; both fail to import here. The CLI
  (`widemem.exe --help`) exposes only `bench`.
- No session/turn/prefetch/consolidation/audit concepts. MCP handlers catch every
  exception and return `{"error": "internal error"}` (`mcp_server.py:224-226`),
  which converts fail-loud into silent-500 — the opposite of this repo's
  "store broken must be distinguishable" contract.

### 6.3 The three options

**Option A — sidecar service (widemem MCP/HTTP server out-of-process).**
Adds an extra dependency (`[mcp]`/`[server]`), a supervised process, a second store
(`WIDEMEM_DATA_PATH`), a second LLM runtime, and still needs an in-process Hermes
bridge provider because Hermes providers are Python objects, not MCP clients. The
bridge would own no prefetch/turn-sync/consolidation semantics, and MCP error
masking would hide the exact offline failure we hit. **Reject.**

**Option B — vendored Python adapter (`WideMemory` inside the provider).**
Technically feasible (`WideMemory` is importable and side-effect-free at
construction; our offline run proves it). But: 905 MB/26,994 extra files into the
Hermes venv (`torch`, `transformers`, `sentence-transformers`, `scipy`, `faiss`),
a second embedding cache, a second SQLite+FAISS store breaking single-store/DB-path
precedence, write latency gated on a local Ollama model (a second LLM runtime, not
the inherited Hermes model), and `user_id`-only scoping (`pipeline.py:318-346`
compares `user_id`, then optional `agent_id`) with no author/channel/capture
containment. You would rewrite the ownership, audit, and worker seams and still
keep a worse write path. **Reject as a backend.**

**Option C — algorithm donor (recommended).**
Transplant the small, dependency-free, deterministic parts into the engine/provider:

1. **Resolver post-validation guards** (§4.2) as invariants on the engine's
   consolidation/claim step: supersession test, atomic-fact + verbatim-quote
   coverage rule, "NONE must name its covering memory", hash-dedup fallback on LLM
   failure, scope guard before any destructive write. ~120 lines of pure Python;
   `batch_resolver.py` and `pipeline.py` are the reference implementations and are
   byte-identical between repo HEAD and the 2.0.2 wheel.
2. **Confidence/trust-verdict layer** (§3.1–3.3): per-embedding-model threshold
   table + `RetrievalConfidence` levels + `answerable/requires_review/reason` from
   `explain=True`. This repo has no equivalent today. Caveat: thresholds are
   model-specific and MiniLM is measurably imprecise (0.4358 false positive above);
   the engine's own embedder must be calibrated with a labeled fixture like
   `tests/fixtures/confidence_minilm.json` rather than copying MiniLM's numbers.
3. **The calibration methodology** (fixture with per-query top similarity +
   answer-fact similarity + subjectless similarity, recall/precision floors,
   live-model re-encode check) as a test pattern.

**Do not transplant:** FAISS+SQLite store ownership, the LLM extractor, the MCP
server, or the `pin()`/`add()` write path.

### 6.4 Verdict and seam

The seam is **the resolver's action-validation boundary and the retrieval trust
verdict**, not the storage engine. widemem is a reference implementation of
"LLM proposes, deterministic code disposes" for memory writes, plus a calibrated
uncertainty layer. Both fit inside the engine without a new process, a new store,
or a new dependency. Everything that makes widemem a *product* (FAISS store, own
LLM default, own data dir, MCP server) collides with this repo's single-store,
single-worker, LLM-inherited, keyless contracts and should not be imported.

## 7. BLOCKED (explicit, with the failing command)

| Item | Status | Exact evidence |
| --- | --- | --- |
| Live `add()` extraction | **BLOCKED** | `WideMemory.add(...)` → `ExtractionError: Fact extraction failed: Failed after 3 retries: Failed to connect to Ollama.` Ollama absent (port `11434` `TcpTestSucceeded: False`, no binary) |
| Live `pin()` | **BLOCKED** | same error; `pin()` is not LLM-free (`core/memory.py:512`) |
| Live batch conflict resolution against a real model | **BLOCKED** | requires Ollama `llama3.1:8b`; resolver path only exercised with `CountingLLM` fake + the offline ADD-with-dedup fallback |
| `llama3.1:8b` model download/VRAM/latency | **NOT MEASURED** | Ollama not installed; no number invented |
| MCP server as a service | **NOT EVALUATED LIVE** | `import widemem.mcp_server` → `ModuleNotFoundError: No module named 'mcp'`; `[local]` lacks `[mcp]`. Tool surface read from source only |
| HTTP server | **NOT EVALUATED LIVE** | `import widemem.server` → `ModuleNotFoundError: No module named 'fastapi'`; `[local]` lacks `[server]` |
| Scale (10⁴–10⁶ facts) recall/latency | **NOT MEASURED** | 9-fact corpus only; no scale fixture built in the timebox |
| Windows test-suite teardown | **KNOWN FAILURE** | `test_pipeline_is_idempotent_for_same_input` → `PermissionError: [WinError 32] ... history.db` during tempdir cleanup |

## 8. Risks (ranked)

1. **The "offline" write path doesn't exist.** `pin()`/`add()` need a local Ollama
   model. Any plan that assumes "pin facts with no LLM" is wrong and should be
   re-scoped before it reaches a design doc. Only `import_json` is LLM-free.
2. **MiniLM confidence is a weak signal.** Measured false positive: unrelated
   "How do I bake sourdough bread?" → MODERATE at `0.4357951879501343`. The source
   itself documents precision 0.745 and 12/12 hard negatives at MODERATE.
   Treating MODERATE as "answerable" needs per-domain fixtures, not a copied table.
3. **Dependency and store duplication.** 905 MB / 26,994 files (torch 477.1 MB),
   plus an 87.4 MB model cache, plus a second FAISS+SQLite store and a second LLM
   runtime. Any adapter also inherits `user_id`-only scoping, not capture
   containment.
4. **Provenance drift.** PyPI 2.0.2 ≠ repo HEAD `ddae795` (still says 2.0.1);
   `core/memory.py`, `core/types.py`, `mcp_server.py` differ. The algorithm files
   are identical, but pin a commit/wheel hash before transplanting.
5. **Error masking.** The MCP server converts every failure into
   `{"error": "internal error"}`, which would hide exactly the failures this
   report depends on.
6. **Windows SQLite teardown** flakes in its own test suite (`WinError 32` on
   tempdir cleanup) — minor, but a signal about Windows hygiene in this codebase.

## 9. Reproduce

```powershell
# install (from repo root)
uv venv --clear --python 3.11 .levelup-work/B-widemem/.venv
uv pip install --python .levelup-work/B-widemem/.venv/Scripts/python.exe "widemem-ai[local]"

# phase 1: warm the dedicated cache (network allowed, one time)
$env:HF_HOME = "$PWD\.levelup-work\B-widemem\hf"
.\.levelup-work\B-widemem\.venv\Scripts\python.exe -u .levelup-work\B-widemem\scratch\phase1_online.py

# phase 2: offline proof (sockets patched to raise before widemem is imported)
$env:HF_HOME = "$PWD\.levelup-work\B-widemem\hf"; $env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
.\.levelup-work\B-widemem\.venv\Scripts\python.exe -u .levelup-work\B-widemem\scratch\phase2_offline.py

# package tests, LLM-independent subset, against the installed wheel
cd .levelup-work\B-widemem\scratch\pkgtests
..\..\.venv\Scripts\python.exe -m pytest -v -p no:cacheprovider
```

Artifacts: `logs/install.log`, `logs/pip-freeze.txt`, `logs/phase1.log`,
`logs/phase2.log`, `logs/pytest.log`, `logs/clone.log`; scripts in `scratch/`;
exports in `data/phase1-export.json`, `data/phase2-export.json`; cloned upstream at
`repo/` (HEAD `ddae795b753e12de0cdfb439a7231b3180fc6e3c`, 2026-10-09, shallow).
