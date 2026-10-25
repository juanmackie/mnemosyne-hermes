# A. Synapse (Ardha-Eco-System/synapse) as a Hermes memory provider

Status: workstream A, 2026-10-18. Static source audit only — **no live stand-up was
possible on this machine** (no Docker, no Ollama, no TrueNAS access; confirmed
below). Every claim cites a file:line or a command output. Where the 2026-08-24
brief and the tree disagree, the tree wins.

Audit conditions (commands run on this workstation):

```text
python (Hermes venv) 3.11.15
git clone --depth 1 ... -> HEAD 64c8b14a6a610a7ce351bd01b70cef1748998803
docker  -> not found
ollama  -> not found
python -c "importlib.util.find_spec": graphiti_core None, falkordb None,
                                        mnemosyne present
PyPI graphiti-core 0.31.1 (license Apache-2.0)
PYTHONPATH=src pytest tests -q -> 126 passed, 1 skipped (FalkorDB fixture)
```

## Verdict

**ADAPT-AS-DONOR — do not ship it as the Hermes provider; harvest the
hippocampus-layer algorithms instead.**

One-line why: the repo is a legitimate, contract-compatible Hermes provider
(duck-typed ABC, two tools, working lifecycle) but it is a **second datastore + a
per-turn LLM extraction pipeline** (FalkorDB + Graphiti + OpenAI-compatible
endpoint) that competes with the single-store, keyless, $0 mnemosyne provider,
while most of its advertised "nine algorithms" are computed and discarded rather
than persisted — the genuinely novel parts are ~800 lines of pure-Python,
dependency-free scoring/consolidation modules that can be adapted onto engine
primitives without the stack.

## 0. What the repo actually is (stale brief premises corrected)

| Brief premise | Reality | Evidence |
| --- | --- | --- |
| "Rust memory backend" | No Rust in this repo. The shipped product is a Python Hermes memory provider; the root `__init__.py` is a 5-line loader shim around `src/synapse/provider.py`. | clone tree: no `Cargo.toml`/`*.rs`; `__init__.py` (repo root) imports `synapse.provider.register`; `pyproject.toml:1-3` (`name = "synapse-memory"`, hatchling wheel packages `src/synapse`) |
| "built for the Claude Code MCP contract" | No MCP server, no MCP transport. It is a Hermes `MemoryProvider` plugin (`register(ctx)` → `ctx.register_memory_provider`). | `src/synapse/provider.py:512-514`; `plugin.yaml:1-10`; `README.md:41` ("Ships as a Hermes Agent memory provider plugin") |
| "nine algorithms wired into the live runtime" | Only 4 of 9 affect a live call: pattern completion, reconsolidation, RIF, and (marginally) pattern separation. Consolidation, forgetting/pruning and schema extraction run only in a separate script and mutate nothing; salience and prediction-error output is discarded; cognitive map is never instantiated. See §3. | §3 table and `scripts/consolidate.py:79-141` |
| Provider is "the product" | Upstream's product claim is the *stack*: Graphiti episode ingestion into FalkorDB, with a thin hippocampus layer on top. | `pyproject.toml:17-19` (`graphiti-core[falkordb]>=0.29.2`); `provider.py:85-117` |

Clone: `git clone --depth 1 https://github.com/Ardha-Eco-System/synapse.git` →
HEAD `64c8b14a6a610a7ce351bd01b70cef1748998803` (commit date 2026-07-15). Owner
`Ardha-Eco-System` (the brief's `ardhaecosystem` redirects); 22 test modules, one
Docker compose file, one CI workflow.

## 1. Evidence table

| # | Claim | Evidence |
| --- | --- | --- |
| E1 | The provider implements the Hermes lifecycle by duck typing, not by subclassing `MemoryProvider`. Its docstring says "full MemoryProvider ABC implementation" but the class has no base. | `src/synapse/provider.py:31` (`class SynapseMemoryProvider:`), `:36-42` (docstring claim); Hermes ABC at `agent/memory_provider.py:43` |
| E2 | Required ABC surface is present with matching signatures: `name`, `is_available`, `initialize(session_id, **kwargs)`, `get_tool_schemas`, `handle_tool_call(tool_name, args, **kwargs)`. Optional hooks present: `system_prompt_block`, `prefetch`, `queue_prefetch`, `sync_turn`, `shutdown`, `on_session_end`, `on_memory_write`, `backup_paths`, `get_config_schema`, `save_config`. | `provider.py:60-62,66-72,74-157,163-208,210-225,276-369,402-469,473-509`; ABC list `agent/memory_provider.py:43-315` |
| E3 | Optional hooks absent: `on_turn_start`, `on_session_switch`, `on_pre_compress`, `on_delegation`. They are documented optional, so this is not a contract failure. | ABC `agent/memory_provider.py:157-315`; no such defs in `provider.py` |
| E4 | Loader compatibility is real: `register(ctx)` → `ctx.register_memory_provider(...)` matches the loader's `register()` path. The class-scan fallback (subclass of `MemoryProvider`) would *not* find it. | Hermes `plugins/memory/__init__.py:295-316`; `provider.py:512-514` |
| E5 | Discovery requires the provider to live in a directory: `$HERMES_HOME/plugins/<name>/__init__.py` (or bundled), with source containing `register_memory_provider`/`MemoryProvider`. Pip metadata alone is never scanned by the memory-provider loader. | Hermes `plugins/memory/__init__.py:74-87,90-121,124-139,183-205`; generic plugin manager skips `exclusive` plugins: `hermes_cli/plugins.py:1592-1611,1398-1405` |
| E6 | The repo's install docs say `pip install` + `hermes config set memory.provider synapse` and never create that plugin directory. The root shim exists precisely because the loader needs `__init__.py` at the plugin root, but nothing documents symlinking/copying the repo root. | `README.md:49-62`; `docs/user-guide.md:52-76`; root `__init__.py`; root `__init__.py` is not part of the wheel (`pyproject.toml:32-33`) |
| E7 | Activation is gated by `is_available()`; if it returns False the provider is silently skipped (`memory.provider: synapse` then means *no external memory at all*). Synapse requires **all three** of `SYNAPSE_FALKORDB_HOST`, `SYNAPSE_LLM_API_KEY`, `SYNAPSE_LLM_BASE_URL`. | Hermes `agent/agent_init.py:1362-1364,1412-1414`; `provider.py:66-72`; test pins the behaviour: `tests/test_provider.py:14-27` |
| E8 | Tool surface is 2 tools, `synapse_query` / `synapse_remember`, returned as static schemas before init. | `tools.py:32-77`; `provider.py:276-285`; test `tests/test_provider.py:82-90` |
| E9 | Facts saved via `synapse_remember` are cached **in process memory only**; the graph write is best-effort async. Brain-mode profile facts are read from that in-memory list, never queried from the graph at init. | `provider.py:58,177-195,371-400` |
| E10 | `save_config()` writes `$HERMES_HOME/synapse.json`; nothing ever reads that file — all config is env-only. The `hermes memory setup` wizard values are silently ignored. | `provider.py:473-505`; `config.py:42-56` (`from_env` only); repo grep: no `synapse.json` reader |
| E11 | Default recall is a substring `CONTAINS` filter, not BM25: no full-text index, no scoring/ranking, case-sensitive matching inherited from the backend. Module docstring calls it "BM25-only". | `retrieval.py:48-58`; docstring `retrieval.py:1-10`; `provider.py:297-299` |
| E12 | `at_time` search applies only the time filter — the text query is silently ignored, so results are "whatever was valid then". | `retrieval.py:116-120` (`temporal_filter_query(valid_at_max=at_time)`; query unused); `tools.py:90-98` |
| E13 | Graphiti telemetry is on by default upstream (anonymous UUID, version, arch, provider labels → PostHog) unless `GRAPHITI_TELEMETRY_ENABLED=false`. Synapse never sets it: 0 matches for `telemetry` in `src/`, `docs/`. | upstream doc: https://help.getzep.com/graphiti/other/telemetry ; grep count = 0 over `src/synapse/**.py` + `docs/**.md` |
| E14 | Dependency floor is unpinned upward: `graphiti-core[falkordb]>=0.29.2`, no upper bound; latest on PyPI is 0.31.1 at audit time. Provider code imports `OpenAIGenericClient` (`provider.py:90`), a recent Graphiti API — the lower bound may be stale (unverified without installing). | `pyproject.toml:17-19`; `python -c "Invoke-RestMethod https://pypi.org/pypi/graphiti-core/json"` → 0.31.1, `requires_python <4,>=3.10` |
| E15 | Data lives in FalkorDB (a server), not in `$HERMES_HOME`. `backup_paths()` returns `[]`, so `hermes backup` cannot capture memory. | `provider.py:507-509`; `falkor.py:19-39` |
| E16 | Store integrity is fail-silent: prefetch/ingest/remember swallow exceptions ("Background — fail silently", `logger.warning`). A broken store is indistinguishable from "no match". | `retrieval.py:97-98`; `provider.py:271-272,395-396,466-467`; repo contract `AGENTS.md` "Fail loud" |
| E17 | Concurrency: each ingest, each remember and each prefetch starts its own daemon thread; each episode opens a fresh `FalkorHelper`/client whose `close()` is a no-op. Nothing bounds this per session. | `provider.py:236-274,379-398,451-469`; `retrieval.py:79-100`; `provider.py:253-258`; `falkor.py:196-199` (`close(): pass`) |
| E18 | Recall path is write-time LLM heavy: Graphiti extraction + embeddings run on every flushed batch (default 5 turns). Batch/trivial-turn skipping is the only cost control. | `provider.py:98-117,144-157`; `encoding.py:19-60`; `config.py:26-36` |
| E19 | Tests are mostly pure unit tests that skip/avoid FalkorDB; CI runs them against a FalkorDB service container with `redis-cli PING` health check. Provider tests themselves are env/state tests. | `tests/conftest.py:11-28`; `.github/workflows/ci.yml` (service block) |
| E20 | Repo is MIT, no vendored third-party source: Graphiti/FalkorDB are imported, not copied. | `LICENSE:1-3`; `pyproject.toml:5`; imports `provider.py:85-90`, `falkor.py:31`; only one licence file in tree |

## 2. Provider-contract comparison and shim surface

### 2.1 Hermes side — no translation layer needed

Synapse's Python surface already matches the ABC closely enough for `register(ctx)`
loading. The shim is **installation/env plumbing**, not an adapter:

```text
$HERMES_HOME/plugins/synapse/
  __init__.py     # from synapse.provider import register   (repo root shim, copied)
  plugin.yaml     # name: synapse (metadata only; loader reads description)
# plus, into the Hermes venv:
#   pip/uv install "graphiti-core[falkordb]>=0.29.2" + the synapse wheel
# env (Hermes gateway process):
#   SYNAPSE_FALKORDB_HOST / _PORT / _PASSWORD
#   SYNAPSE_LLM_API_KEY   (dummy value is fine for Ollama — must be non-empty)
#   SYNAPSE_LLM_BASE_URL  / SYNAPSE_LLM_MODEL / SYNAPSE_EMBEDDING_MODEL
#   GRAPHITI_TELEMETRY_ENABLED=false
```

Caveats that make this more than a one-liner:
- the repo root shim imports `synapse`, so the `synapse` package must be installed
  **into the Hermes venv**, not the user's system Python (`__init__.py`;
  `pyproject.toml:32-33`);
- the docs' `pip install` + config flow never creates the plugin directory (E6),
  so `hermes memory status` will show no provider until it is added;
- `is_available` needs a non-empty API key even for a keyless local Ollama (E7);
- missing optional hooks mean no `on_session_switch` rotation, so a long-lived
  gateway session keeps its original `group_id`; no `on_pre_compress` checkpoint.

### 2.2 Engine side — what a donor adaptation must translate

If harvested into this repo, the translation is data-shape only. Hippocampus
modules consume plain `list[dict]` edges (`fact`, `from_node`, `to_node`,
`valid_at`, `invalid_at`) — exactly the shape the engine's `TripleStore`/
`EpisodicGraph` rows can produce:

| Synapse shape | Engine primitive | Translation |
| --- | --- | --- |
| edge dict `{from_node,to_node,fact,valid_at,invalid_at}` | `triples` table `{subject,predicate,object,valid_from,valid_until}` | lossy: Graphiti edge = free-text `fact` + two entities; engine triple needs a split predicate. Decide one: keep `fact` as `object` with predicate `"relates_to"`, or run a splitter. |
| `group_id` (single, from `agent_identity`) | `owner_id`/`bank`/profile scoping | one graph per profile is emulable by bank naming; user/channel scoping is not representable. |
| salience `0.0–1.0` | `importance REAL DEFAULT 0.5`, 0–1 float | same scale (no conversion). |
| in-memory recall count + activation window | persisted `recall_count`, `last_recalled` | engine is already better; persist instead of in-memory. |
| `synapse_query` tool name | canonical `mnemosyne_*` table | do not add a second tool name; fold behaviour into `recall`/`graph_query`. |

## 3. The nine hippocampus algorithms — file:line, behaviour, mapping

Status legend: **LIVE** = affects a provider call today; **COMPUTE-ONLY** = result
computed and thrown away (or script-only); **UNMAPPABLE** = no call site at all.

| # | Algorithm | File:line | Behaviour (one line) | Hermes/engine primitive | Status | Evidence of wiring or dead-end |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Salience scoring | `hippocampus/salience.py:18-107` (`score_entity:33`, `score_edge:73`) | Weighted 0–1 importance from recency/frequency/correction/emotional (entity) or recency/validity/recall/richness (edge). | `remember` (write-time importance) | COMPUTE-ONLY | `Hippocampus.on_episode_ingested:129-131` scores `new_edges`, but provider passes `new_edges=[]` (`provider.py:262-269`) and drops the return value (`provider.py:265-270`) |
| 2 | Forgetting curve | `hippocampus/forgetting.py:15-69` (`compute_strength:28`, `should_prune:58`) | Ebbinghaus `exp(-t/tau)` with salience/recall modulation, prune threshold. | `recall` (decay/rank), `remember` (importance) | COMPUTE-ONLY | only call site is `scripts/consolidate.py:125-129`, `--prune` reports candidates and writes nothing (`consolidate.py:108-138`, `:50-51`) |
| 3 | Consolidation engine | `hippocampus/consolidation.py:12-90` (`detect_contradictions:15`, `hebbian_strengthening:60`) | Flags superseded facts via `invalid_at` + keyword corrections; groups co-occurring entities for Hebbian boost. | `triple` (temporal end/supersede), `remember` | COMPUTE-ONLY | `Hippocampus.run_consolidation:260-273` called only by `scripts/consolidate.py:85`; provider never calls it; no graph mutation anywhere |
| 4 | Pattern completion (CA3) | `hippocampus/pattern_completion.py:31-146` (`complete:53`) | Substring-match seed edges, BFS ≤2 hops over active edges, return deduped subgraph facts. | `recall` | LIVE (PARTIAL) | wired at `provider.py:320-337`; seeds come from a **1-hop** `get_entity_neighborhood` (`falkor.py:156-194`), so effective depth is bounded by the fetched edge set |
| 5 | Reconsolidation | `hippocampus/reconsolidation.py:40-166` (`recall:75`, `tick:91`, `get_boost:135`) | Recall counter + N-turn activation window; recalled entities get a salience boost on new edges. | `recall` (spaced repetition) | LIVE (in-memory only) | `provider.py:222-223` ticks per turn; `provider.py:341-343` records recall; state cleared at session end (`hippocampus/__init__.py:306-310`, `provider.py:430-431`) |
| 6 | Prediction error | `hippocampus/prediction_error.py:30-215` (`detect:55`, `detect_contradictions:113`, `detect_surprise:176`) | Novelty fraction, keyword + same-pair contradiction, known-subject-new-object surprise. | `remember`/`triple` (contradiction handling) | COMPUTE-ONLY / DEAD | provider supplies `new_entities=[], new_edges=[]` (`provider.py:262-269`), so every detector returns empty; queued contradictions (`hippocampus/__init__.py:121-122`) are only drained by the uncalled script path (`:260-273`) |
| 7 | Schema extraction (CLS) | `hippocampus/schema_extraction.py:41-204` (`extract:67`) | Connected-component clustering over active edges → heuristic summary + density (docs claim it "creates schema nodes"; it returns dicts and writes nothing). | `persona` / `canonical` (generalised facts) | UNMAPPABLE as shipped | `Hippocampus.extract_schemas:275-277` called only from `scripts/consolidate.py:100-106`, report-only |
| 8 | Pattern separation (DG) | `hippocampus/pattern_separation.py:29-163` (`fingerprint:55`, `find_similar_pairs:132`) | Entity fingerprint = neighbour set; Jaccard similarity flags similar-but-distinct contexts. | `triple` (namespace separation) / `recall` (RIF) | LIVE only through RIF | episode path computed with `new_entities=[]` (`provider.py:266`) → empty; RIF path is live (`hippocampus/__init__.py:211-226`, `provider.py:346-358`) |
| 9 | Cognitive map | `hippocampus/cognitive_map.py:31-145` (`shortest_path:68`, `neighborhood:101`) | BFS shortest semantic path, N-hop neighbourhood, degree — graph navigation utilities. | `recall` / `triple` (`graph_query`) | UNMAPPABLE | class is exported (`hippocampus/__init__.py:26`) but never instantiated anywhere in `src/` or `scripts/` |
| + | Retrieval-induced forgetting (RIF) | `hippocampus/__init__.py:69-73,181-250`; `provider.py:346-358` | Jaccard-similar entities not recalled in the same query get a session-scoped ranking penalty that expires with the reconsolidation window. | `recall` (re-rank) | LIVE | the only hippocampus effect that visibly changes tool output |

Net: 3 distinct live behaviours (pattern completion, reconsolidation, RIF) plus
pattern separation acting only as RIF's similarity engine; 4 compute-only or
report-only (salience, forgetting, prediction error, consolidation); 2 with no
call site at all (schema extraction, cognitive map). This matches the code but not
`README.md:80-102`'s "all nine wired".

## 4. Dependencies, self-host audit, and the TrueNAS runbook

### 4.1 What must run

| Component | Required | Cost / licence | Evidence |
| --- | --- | --- | --- |
| FalkorDB server (Docker, Linux image) | **yes** — every read/write goes through it | $0 self-hosted; FalkorDB python client MIT (PyPI classifier) | `provider.py:87,92-97`; `retrieval.py:35-42`; `docker/docker-compose.yml:1-12` |
| Graphiti library (in-process, no server) | **yes** | Apache-2.0 (`license_expression` on PyPI); pulls `neo4j`, `openai`, `posthog` (telemetry), numpy, pydantic, tenacity | `pyproject.toml:17-19`; PyPI metadata for graphiti-core 0.31.1 |
| OpenAI-compatible LLM + embeddings endpoint | **yes** at init and on ingest (entity/edge extraction, embeddings, reranker) | cloud default = paid/SaaS (violates the $0 mandate); local Ollama/vLLM on the host = $0 | `provider.py:98-117`; `config.py:24-27` (defaults `gpt-4o-mini`, `text-embedding-3-small`); README claims local Ollama works (`README.md:227-238`) |
| Python ≥3.11 | yes | — | `pyproject.toml:6`; Hermes venv is 3.11.15 (verified) |
| Docker / Linux containers | yes, on whatever host runs FalkorDB | $0 | `docker-compose.yml:2-3` (`falkordb/falkordb:latest`, no `platform:` key, Linux-only image) |
| OS keyring / cloud key / Postgres / Neo4j server | no | — | tree imports only `falkordb` + `graphiti_core` |

### 4.2 Windows-hostile bits

- Docker Desktop/WSL2 is needed to run the FalkorDB Linux image; there is no
  Windows-native FalkorDB service, and Docker is not installed here
  (`docker: not found`). TrueNAS is the only plausible host on this network.
- The Python code itself is portable: no `os.fork`, `fcntl`, signals, `/proc`, or
  absolute Unix paths were found in `src/`.
- Installing the provider as a Hermes plugin needs a directory symlink or a copy;
  the repo ships no Windows installer. Use copy on Windows (this repo's own
  `install.ps1`/`--copy` path exists for the same reason,
  `docs/HERMES_INTEGRATION.md:42-48`).

### 4.3 TrueNAS runbook (to be executed on the the host host, not here)

I could not touch the the host from this workstation, so this is the exact sequence to
run there; treat it as unexecuted.

1. **TrueNAS → Apps → Discover/Custom App → "Install via YAML"** (SCALE 24.10+
   Docker-based Apps; older SCALE versions use the same fields in the form UI —
   the compose below is the canonical source). Container `falkordb`:
   - image `falkordb/falkordb:latest` — pin a digest or a release tag before
     relying on it; `latest` is unpinned in the repo and is a reproducibility risk;
   - port mapping `6379:6379` (LAN only — do **not** expose to the internet);
   - volume `falkordb-data` → `/var/lib/falkordb` (ixVolume or a dataset path);
   - restart policy `unless-stopped`;
   - health check: `redis-cli PING` (this is the same check upstream CI uses,
     `.github/workflows/ci.yml`).
   Compose equivalent, from `docker/docker-compose.yml:1-12`:
   ```yaml
   services:
     falkordb:
       image: falkordb/falkordb:latest
       container_name: synapse-falkordb
       ports: ["6379:6379"]
       volumes: ["falkordb-data:/var/lib/falkordb"]
       restart: unless-stopped
   volumes: {falkordb-data: {}}
   ```
2. **Ollama app** (TrueNAS community app or Linux VM): pull an extraction model
   and an embedding model, e.g. `qwen2.5:7b-instruct` and `nomic-embed-text`.
   Serve on `11434` on the LAN. This is what keeps the stack $0; with the repo
   defaults (`gpt-4o-mini` + `text-embedding-3-small`) you are off-mandate.
3. **Auth decision**: the shipped compose publishes 6379 with **no password** and
   no TLS. FalkorDB supports a password the Synapse client already passes
   (`config.py:20,48`; `provider.py:92-97`), but the compose file does not set
   one. Either enable server-side auth and set `SYNAPSE_FALKORDB_PASSWORD`, or
   firewall 6379 to the Hermes workstation only.
4. **On the Hermes workstation** (Windows), in the gateway process environment
   (Hermes reads its `.env`; see `docs/user-guide.md:67-77`):
   ```text
   SYNAPSE_FALKORDB_HOST=<host-lan-ip>
   SYNAPSE_FALKORDB_PORT=6379
   SYNAPSE_FALKORDB_PASSWORD=<if enabled>
   SYNAPSE_LLM_API_KEY=dummy-nonempty       # required by is_available even for Ollama
   SYNAPSE_LLM_BASE_URL=http://<host-lan-ip>:11434/v1
   SYNAPSE_LLM_MODEL=qwen2.5:7b-instruct
   SYNAPSE_EMBEDDING_MODEL=nomic-embed-text
   SYNAPSE_HALF_LIFE_DAYS=7
   GRAPHITI_TELEMETRY_ENABLED=false         # required by the no-SaaS mandate
   ```
5. **Install into the Hermes venv** (`...\hermes-agent\venv\Scripts\python.exe`):
   `pip install "graphiti-core[falkordb]>=0.29.2"` and the `synapse` package (or
   `pip install -e` the clone). Then create
   `$HERMES_HOME\plugins\synapse\__init__.py` containing
   `from synapse.provider import register` (the repo-root shim) plus the repo
   `plugin.yaml`; set `memory.provider: synapse` in `config.yaml`. Restart the
   gateway (Hermes caches provider modules; see
   `docs/HERMES_INTEGRATION.md:68-71`).
6. **Verify, in order**:
   - from Windows: `Test-NetConnection <host> -Port 6379` → TcpTestSucceeded;
   - `python -c "import falkordb; print(falkordb.FalkorDB(host='<host>', port=6379).connection.ping())"`
     (or `redis-cli -h <host> PING`) → `True`/`PONG`;
   - `curl http://<host>:11434/v1/models` returns the two models;
   - `hermes memory status` shows provider `synapse` available/active;
   - one Hermes turn with a durable fact, then a `synapse_query` for it;
   - graph inspection: run `python scripts/consolidate.py --group-id <profile>`
     (the script's JSON contains `edge_count`/`entity_count`, `consolidate.py:87-98`)
     — it is read-only, so it doubles as the health check;
   - telemetry check: confirm `GRAPHITI_TELEMETRY_ENABLED=false` is visible in the
     gateway environment, otherwise PostHog traffic violates the mandate (E13).
7. **Backups**: snapshot the FalkorDB dataset/volume on TrueNAS. `hermes backup`
   will not include it (`backup_paths() -> []`, E15).

## 5. Compliance

- Synapse itself: MIT (`LICENSE:1-3`, "Copyright (c) 2026 Ardha Universe";
  `pyproject.toml:5`). Note the README says "Ardha Studios" (`README.md:377`) —
  cosmetic inconsistency only.
- No vendored Graphiti or FalkorDB client source: imports only
  (`provider.py:85-90`, `falkor.py:31`); the clone has exactly one licence file.
- Attached licences if installed:
  - `graphiti-core`: **Apache-2.0** (PyPI `license_expression`), hard deps
    `neo4j`, `numpy`, `openai`, `posthog`, `pydantic`, `python-dotenv`, `tenacity`
    (PyPI metadata). Apache-2.0 is compatible with MIT; if any of it is ever
    vendored into this repo, the Apache NOTICE/attribution obligations attach —
    keep it a dependency, do not vendor.
  - `falkordb` client: **MIT** (PyPI classifier).
- Privacy: Graphiti's default-on anonymous telemetry (PostHog) is a real
  no-SaaS/compliance gap until `GRAPHITI_TELEMETRY_ENABLED=false` is set; Synapse
  neither documents nor disables it (E13).
- Secrets: Synapse needs an LLM API key env var even when the endpoint is local
  Ollama (dummy value suffices), and `save_config` deliberately drops secrets from
  `synapse.json` (`provider.py:501-505`) — but that file is never read (E10).
- No keyring dependency, no second provider key when pointed at the Hermes
  local proxy / Ollama.

## 6. Gap report against mnemosyne-memory 3.15.1

### 6.1 Works out of the box (within Synapse's own stack)

- Hermes MemoryProvider lifecycle and both tools (`synapse_query`,
  `synapse_remember`) load and dispatch (E2, E4, E8).
- Batched turn capture with trivial-turn skipping; async ingest
  (`encoding.py:19-60`, `provider.py:210-274`).
- Brain-mode vs supplementary-mode system prompt (`provider.py:163-197`).
- RIF ranking penalty and pattern-completion expansion are wired into the query
  path (E-table §3).
- 126/127 tests pass with no FalkorDB, no `graphiti_core` and no `falkordb`
  client installed (`PYTHONPATH=src python -m pytest tests -q` → 126 passed,
  1 skipped); the skip is the FalkorDB session fixture
  (`tests/test_consolidate.py:50`). Provider tests are env/state unit tests, so
  the integration path is *not* covered by this result.

### 6.2 Needs a provider/install shim

- Plugin-directory discovery + venv install + env vars + telemetry off (§2.1).
- A Windows install script (copy, not symlink).
- If adapting: an edge-dict → triple adapter and a recall re-ranker in the engine
  (§2.2).

### 6.3 Conflicts with this repo's provider/engine contract

| Contract | mnemosyne 3.15.1 provider | Synapse | Evidence |
| --- | --- | --- | --- |
| Single store ownership | one engine DB per install, path precedence `memory.mnemosyne.db_path` > env > engine default | a FalkorDB graph + `$HERMES_HOME/synapse.json` (unread); no `db_path`; store lives on another host | `docs/HERMES_INTEGRATION.md:79-100`; `provider.py:496-505`; E15 |
| One external provider at a time | MemoryManager rejects a second external provider | replacing mnemosyne disables the engine provider and its 4 default/40 total tools | `agent/memory_manager.py:6-9,354-396`; `agent/agent_init.py:1351-1364` |
| Namespace / owner / bank model | `owner_id`, banks, `profile_isolation` | one `group_id = agent_identity or "default"`; ignores `user_id`, `chat_id`, `session_title`, `hermes_home` | `provider.py:74-82` (only session_id/agent_identity used); Hermes passes the rest (`agent/agent_init.py:1366-1409`) |
| Importance semantics | persisted 0–1 float, hybrid rank 50% vector + 30% FTS + 20% importance | 0–1 salience **computed then discarded**; no persisted importance; recall is substring `CONTAINS` | `mnemosyne/core/beam.py:13,605`; `provider.py:262-270`; `retrieval.py:48-58` |
| Capture containment | skips `cron`, `flush`, `subagent`, `background`, `skill_loop`; user turns only | ignores `agent_context`; `sync_turn` writes whatever it is given | `docs/HERMES_INTEGRATION.md:104-110`; vendored provider `__init__.py:1599,2113-2130`; Synapse `provider.py:74-82,210-225` |
| Single background consolidation worker | engine provider serialises consolidation behind a lock; MemoryManager has one background worker | unbounded per-event daemon threads; per-episode FalkorDB client with no-op close | `memory_manager.py:558-614`; vendored `__init__.py:3318-3345`; Synapse `provider.py:274,398,469`, `retrieval.py:100`, `falkor.py:196-199` |
| DB path precedence | `db_path` wins over profile isolation | no precedence; env only; wizard config unread | `docs/HERMES_INTEGRATION.md:81-91`; E10 |
| Tool names | canonical `mnemosyne_*` table (4 default, 40 total) | `synapse_query` / `synapse_remember`; fine for an alternative provider, but must not become a second registration under this repo's name | `docs/HERMES_INTEGRATION.md:134-182`; `AGENTS.md` "provider id mnemosyne" |
| Fail loud | broken store raises | silently swallows read/write errors | E16; `AGENTS.md` "Fail loud" |
| Backups | provider returns its DB path for `hermes backup` | returns `[]` | vendored `__init__.py:2145`; E15 |

## 7. Risks (ranked)

1. **Unverified live behaviour.** No Docker/Ollama/the host here, so nothing in this
   doc was executed against a running FalkorDB/Graphiti. Risk compounds with the
   unpinned Graphiti floor (E14): the `OpenAIGenericClient` import may not exist in
   the declared minimum version.
2. **Mandate compliance, not just engineering.** Cloud defaults are paid/SaaS;
   Graphiti telemetry is default-on; the FalkorDB port is unauthenticated in the
   shipped compose. All three need explicit mitigation on the host runbook.
3. **Overclaim vs code.** "Nine algorithms wired" and "brain mode loads profile
   facts from the graph" are not true in code (E9, §3). Any donor work must port
   *behaviour*, not the docs.
4. **Recall quality regression** if used as-is: substring `CONTAINS` recall with no
   ranking and an `at_time` path that discards the query (E11, E12), versus the
   engine's FTS5 + local bge-small embeddings (`mnemosyne/core/beam.py:13`,
   `mnemosyne/core/embeddings.py:70`).
5. **Operational surface**: an extra always-on service + LLM extraction latency per
   batch, thread/client churn, and memory outside `hermes backup`. The Hermes
   provider is currently zero-service (SQLite only), so this is a large
   operational downgrade for a personal install.
6. **Install-path trap**: following the repo's documented quickstart does not
   create a discoverable provider (E5, E6); users will report "no memory" while the
   stack looks healthy.

## 8. Recommendation

**ADAPT-AS-DONOR.** Concretely:
- Port the pure modules (`salience.py`, `forgetting.py`, `consolidation.py`,
  `pattern_completion.py`, `pattern_separation.py`, `prediction_error.py`) behind
  the engine's `TripleStore`/`EpisodicGraph`, replacing:
  - forgetting → existing `weibull_boost`/`weibull_decay_factor`;
  - reconsolidation → existing persisted `recall_count`/`last_recalled`;
  - consolidation contradictions → `TripleStore.end` + `VeracityConsolidator`;
  - pattern completion → `EpisodicGraph.find_related_memories(depth=2)`;
  - schema extraction → persona/canonical summarisation (it is the closest
  existing primitive; do not write "schema nodes");
- Lift RIF as a re-rank step in recall (session-scoped, no schema change);
- Do **not** import the ~520-line provider, FalkorDB, Graphiti, or any
  telemetry-enabled dependency. None of the donor modules import them (they take
  plain dicts), which is why this is an ~800-line harvest and not a stack adoption.

If the user explicitly wants a second, graph-native provider (accepting the
FalkorDB + LLM service), `INTEGRATE` is technically reachable via §2.1 + §4.3, but
it should then be a separate plugin id, never a change under `mnemosyne`.
