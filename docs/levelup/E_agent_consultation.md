# E. Live agent consultation — what the memory consumer says

Date: 2026-10-18. Method: five **read-only** prompts sent to the production Hermes
agent (v0.22.1, the host) over the api_server OpenAI transport
(`/v1/chat/completions`, `X-Hermes-Session-Key: levelup-consult`), each in a fresh
session. The agent was asked not to write memories or modify anything; it used its
own read-only tools to answer. Raw replies:
`.levelup-work/consult_raw.json`, `.levelup-work/consult_followup.json`.

This is the consumer's view, not ours. It is the first time the plan was checked
against the system that actually uses it.

## 1. Deployment facts (agent-reported, quoted from its tools)

| Item | Value |
|---|---|
| Provider | `mnemosyne` (`config.yaml`: `provider: mnemosyne`, `memory_enabled: true`) |
| Top-level provider tools | `mnemosyne_remember`, `mnemosyne_recall`, `mnemosyne_stats`, `mnemosyne_forget` (4, as designed) |
| **Additional MCP surface** | **36** `mcp__mnemosyne__*` tools from `$HERMES_HOME/mnemosyne-venv/bin/mnemosyne mcp` (triple/graph/persona/canonical/hygiene/sync/…) |
| Engine | `mnemosyne 3.15.1`, regular `site-packages` install, **Python 3.13**; no `mnemosyne 2.4.0` collision, no shadowing (editable package is `mnemosyne_hermes 0.1.0` only) |
| DB | `$HERMES_HOME/mnemosyne_data/mnemosyne.db`, 55,861,248 bytes, WAL active; backups in `$HERMES_HOME/backups/` |
| Embeddings | `BAAI/bge-small-en-v1.5`, dim 384, **fastembed 0.8.0**, `embeddings_off: false` |
| Importance | 0.0–1.0 float observed; `importance_weight: 0.2` is the hybrid weight, not a scale |
| Working memory | 2,925 total / 2,390 consolidated / **535 unconsolidated** |
| Episodic | 491, 447 int8 vectors |
| Memoria graph | 2,819 facts, 75 timelines, **13 KG edges**, 257 instructions, 130 preferences |
| Recall shape | returns ranked results with scores/tiers; **always top-k, no abstention threshold** |

Two operational findings worth their own section (§4): **two MCP server processes**
and **30 secret assignments in the memory store**.

## 2. The agent's ranked gaps (its order)

| # | Gap | Severity | Observable failure it named |
|---|---|---|---|
| 1 | Calibrated confidence + abstention | **critical** | recall hits are stated as fact even when stale/weak/unrelated; "nearly every wrong answer starts here" |
| 2 | Temporal validity | high | `valid_until`/`superseded_by` exist but are sparsely populated; old host IPs, cron policies and prices surface as current |
| 3 | Consolidation / summarization | high | 535–536 unconsolidated rows dilute recall precision; pending-ingest replace patterns visible |
| 4 | Importance ranking + active forgetting | medium-high | low-value noise competes evenly; precision decays over months |
| 5 | Provenance tagging | medium | cannot reliably tell user-stated vs tool-observed vs inferred; misattributes corrections |
| 6 | Write-time conflict resolution | medium | contradictory values coexist until manual consolidation |
| — | Multi-hop graph recall | **deferred** | "the memoria graph has only 13 edges, so there is almost nothing to traverse yet" |

Why this order (its words): "expected impact equals how often you are exposed per
turn times how costly a wrong answer is."

**This validates the primary recommendation.** Confidence/abstention — the one gap
we planned to port from widemem — is the consumer's #1. It also adds a
**temporal-validity slice we had not planned**, and demotes multi-hop graph work,
which is consistent with rejecting synapse as a backend.

## 3. The agent's critique of our plan

Three things it says are wrong or missing, and three it would do differently:

| # | Critique | Our response |
|---|---|---|
| 1 | "The MemBase +11 recall@10 cannot be attributed to nomic-v1.5" — MemBase conflates encoder with chunking/retrieval/reranking | **Partially accepted.** Our harness held unit granularity, chunking (one unit/message, NaiveRAG defaults) and top-k fixed, and swapped only the encoder, so the delta is attributable *within that protocol*. But it was **not the engine's retrieval path**, which is the real confounder. Accept the fix: gate the reindex on an **engine-native A/B on the real corpus**, with bge-small vectors retained until it passes. |
| 2 | "Deterministic conflict-resolver guards over a bitemporal store without writer serialization and a total order" — concurrent writers break the guards before they run | **Accepted.** Add a **Sprint 0**: single-writer serialization, deterministic total order (transaction time, event time, node id), idempotency keys — *before* any resolver guards. |
| 3 | "A confidence/abstention layer with no telemetry, baseline diff, coverage floor, or kill-switch" — silent veto, unobservable | **Accepted.** Add per-operation telemetry, a weekly baseline diff, a recall-coverage floor alert, and a no-redeploy kill switch to Sprint 1. |

Its three alternatives (controlled harness on the real corpus; serialize writers
first; flag + telemetry + kill switch) are now the plan.

## 4. Follow-up findings (read-only, verified by the agent)

1. **Import hygiene is clean.** It corrected its own first answer: no editable
   `mnemosyne 2.4.0`; `import mnemosyne` resolves to
   `$HERMES_HOME/mnemosyne-venv/lib/python3.13/site-packages/mnemosyne/__init__.py`
   at **3.15.1**. The editable package is `mnemosyne_hermes 0.1.0` (the bridge).
   The repo's name-collision warning is therefore **not** triggered on this host.
2. **Double surface is live — and doubled in processes.** The 4 provider tools and
   the 36 MCP tools are both active. The agent observed **two MCP server
   processes** (`$HERMES_HOME/mnemosyne-venv/bin/python …/bin/mnemosyne mcp`):
   PID 91185 (old gateway pgid 157) and PID 95708 (current gateway pgid 95634).
   Same DB. Identical recall queries returned **different result sets** on the two
   surfaces (only 2 of 5 overlapped). That is an orphan-process / scope-drift
   issue, not a duplicate-write symptom (no writes were made during the audit).
3. **Secrets are being captured into memory.** `mnemosyne_hygiene_audit` (dry-run,
   3,991 rows scanned) reports: **198 noise candidates**, **30
   `secret_assignment` findings** (examples: a Gmail setup memory, a
   `rabbit.tech` install token), **17 duplicate content lines**. (An earlier
   doctor-style check in the critique reported 247 orphans / 52 hygiene items /
   4 secrets — different tool, different window; both point the same way.)
4. **Abstention works at the model layer, not the tool layer.** Asked about a
   fabricated entity (`Norsk Hydrokinetic turbines`), both surfaces returned 5
   unrelated results and the agent still **abstained**. So the model saves it, but
   the retrieval tool gives it nothing to distinguish "no match" from "weak match"
   — exactly the gap #1 above, and the engine's `MNEMOSYNE_RECALL_SCORE_FLOOR`
   (default off) is the existing lever.

## 5. Acceptance tests the agent proposed (adopt as a gate)

| # | Test | Pass criterion | Failure caught |
|---|---|---|---|
| 1 | Silent index drop: remember a unique probe phrase, ask in a fresh session | exact stored value returned verbatim | write accepted but never embedded/indexed (healthy-looking, dies on first real use) |
| 2 | Forced abstention: ask about a fabricated entity | states no memory and declines | confabulation under recall pressure |
| 3 | Temporal supersede: write two dated facts for one slot, ask "today" | only the newer value, older flagged superseded | stale fact served as current |
| 4 | Multi-hop: `Alice manages Bob`, `Bob owns Project Orion`; ask "who owns the project connected to Alice?" | returns Orion's owner via traversal | graph edges stored but invisible at query time |
| 5 | Provenance separation: same attribute stated vs tool-observed, conflicting | one value, named source/veracity, conflict flagged | silent merge or unattributed certainty |

Tests 1–3 and 5 are directly runnable against this deployment; test 4 is
**expected to be weak today** (13 KG edges) and should be a stretch gate until the
graph is populated. These should live in `bench/` as a repeatable acceptance gate,
not run ad hoc against production (several write probe facts).

## 6. What changed in the plan because of this

- **Sprint 0 added**: writer serialization + idempotency + observability
  groundwork, before resolver guards.
- **Sprint 1** gains telemetry, coverage floor, baseline diff and kill switch.
- **New temporal-validity slice** added to the spike plan (populate and respect
  `valid_until`/`superseded_by`; filter or flag expired rows at recall).
- **MemBase** reindex gate becomes engine-native on the real corpus, with
  bge-small retained as rollback.
- **Acceptance suite** (above) added as a repo gate.
- **Ops items flagged, not executed** (need owner approval): remove the orphan MCP
  process (PID 91185, verify first), run `hygiene_clean` on the 30 secret
  assignments **after a backup**, consolidate the 535-row backlog, and calibrate
  the recall score floor.

## 7. Verdict from the consumer

Ranked priority for a "top-class" memory system, as the agent that uses it sees:
**confidence/abstention → temporal validity → consolidation → importance/forgetting
→ provenance → conflict resolution**, with multi-hop graph work deferred until the
graph has data. That is a concrete, testable order, and it is compatible with the
integration plan in [README.md](./README.md) — with the temporal slice and the
writer-serialization prerequisite added.
