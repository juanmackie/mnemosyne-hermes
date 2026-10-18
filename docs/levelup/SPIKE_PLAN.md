# Spike plan — 3 sprints (plus a prerequisite), winner + donor path

Scope: integrate the **widemem-ai algorithm donor** (primary) and stand up the
**MemBase evaluation gate** (runner-up), without adopting a second store, extractor
or worker. The engine already owns the temporal graph, forgetting, consolidation
and local embeddings ([0_baseline_and_premises.md](./0_baseline_and_premises.md) §2).

Reordered by the **live consumer's** own ranking
([E_agent_consultation.md](./E_agent_consultation.md)): confidence/abstention
first, temporal validity second, consolidation third, then importance/provenance/
conflict resolution. Multi-hop graph work is deferred — the production graph has
only 13 edges.

Non-goals: shipping FalkorDB/Graphiti, a second `MemoryProvider` registration,
replacing the embedder without a measured win, patching `site-packages` (wiped by
`uv upgrade`).

Constraints in every sprint: preserve capture containment (`default_scope`,
`sync_roles`, `skip_contexts`, raw-capture opt-in, 8,000-char prefetch ceiling,
single consolidation worker); no new runtime network path (offline proof must keep
passing); engine changes only via `integrations/engine-patches/` + manifest;
provider edits via the vendored-patch process; CI lanes
`./test-all.sh --skip-llm`, `--require-engine`, `bash scripts/checks.sh`,
`pre-commit run --all-files`.

---

## Sprint 0 — write serialization + observability (prerequisite)

The live agent's strongest critique: deterministic resolver guards assume a single
writer and a total order, but production capture is concurrent. Do this **before**
any guard work.

**Gap to reproduce.** Two concurrent writers applying a supersede to the same
`(subject, predicate)` produce a lost update or a contradictory pair. Write the
reproducing test first.

**Work.**
1. Single-writer serialization around consolidation/claim writes (the engine
   already serializes foreground access; make the claim path explicit).
2. Deterministic total order: (transaction time, event time, node id).
3. Idempotency keys on writes so a retried capture is a no-op.
4. Per-operation counters: writes, supersedes, rejected/no-op actions, recall
   coverage — the telemetry Sprint 1 needs.

**Milestones.** M0.1 concurrent-writer repro fails; M0.2 serialization makes it
pass; M0.3 counters exposed through `mnemosyne_recall_diagnostics`.

**Rollback.** If serialization reduces background consolidation throughput beyond
the agreed budget, keep the lock off and do not start Sprint 2 guards.

---

## Sprint 1 — retrieval trust verdict (confidence/abstention) + telemetry

Consumer rank **#1**. Note: the production store already shows the failure — recall
returned 5 unrelated rows for a fabricated entity and the agent had to abstain by
judgment.

**Gap to reproduce.** Seed 10 unrelated facts, query an out-of-domain question,
show the current recall returns results with no confidence field and no way to say
"no match".

**Work.**
1. Calibrated fixture for the engine's own embedder (`bge-small-en-v1.5`),
   modelled on widemem's methodology but re-measured; record thresholds.
2. Confidence verdict on recall results behind a flag (default off):
   HIGH/MODERATE/LOW/NONE + `answerable` / `requires_review` / `reason`.
3. Calibrate and document the engine's existing **`MNEMOSYNE_RECALL_SCORE_FLOOR`**
   (default disabled) as the abstention lever — prefer the existing knob over a
   parallel mechanism.
4. Telemetry + safety: per-operation counts, weekly baseline diff, recall-coverage
   floor alert, and a **kill switch** that disables the verdict without redeploy.

**Milestones.** M1.1 measured precision/recall table; M1.2 flagged verdict;
M1.3 provider opt-in + tests; M1.4 telemetry/kill switch documented.

**Rollback.** If the out-of-domain false-positive rate exceeds the agreed ceiling,
leave the feature flag-only and record the numbers. Any default-behaviour change
without the flag → revert.

---

## Sprint 2 — temporal validity + resolver guards

Consumer rank **#2** (temporal) and **#6** (conflict resolution); the guards depend
on Sprint 0.

**Gap to reproduce.** (a) A fact past `valid_until` still returns as current.
(b) A proposer UPDATE removes an unrelated fact, DELETEs a low-information row, or
emits NONE without naming the covering memory.

**Work.**
1. **Temporal validity:** ensure `valid_until`/`superseded_by` are populated on
   supersede; filter or clearly flag expired rows at recall; add the two-dated-fact
   test (newer wins, older flagged).
2. **Resolver guards** (~120 LOC, pure Python) from widemem:
   supersession test; atomic-fact + verbatim-quote rule; NONE must name its target;
   hash-dedup fallback on proposer failure; scope guard before destructive writes.

**Milestones.** M2.1 expiry test passes; M2.2 guards as pure functions + unit
tests; M2.3 before/after golden-fixture comparison of claim outputs.

**Rollback.** Any unexplained diff on existing consolidation fixtures → revert. If
legitimate updates drop on real data, revert rather than relax a guard.

---

## Sprint 3 — engine-native embedding gate + acceptance suite

**Work.**
1. Add `bench/embedding_eval`: same corpus, same unit granularity, **the engine's
   retrieval path**, only the encoder swapped — the gate the live agent demanded.
   Keep bge-small vectors until it passes; rollback is the retained index.
2. Run on the Z640 with a local LLM so F1/BLEU/judge are available there.
3. Add `bench/live_acceptance` implementing the consumer's five tests
   ([E §5](./E_agent_consultation.md#5-acceptance-tests-the-agent-proposed-adopt-as-a-gate)):
   silent index drop, forced abstention, temporal supersede, multi-hop (stretch),
   provenance separation. Run against a staging profile, not production.
4. Record the reindex decision: **APPROVED** only if the engine-native gain beats
   run-to-run noise and rollback is proven; otherwise **REJECTED (keep bge-small)**.

**Milestones.** M3.1 harness runs; M3.2 Z640 QA numbers; M3.3 decision + acceptance
gate registered in `scripts/checks.sh`.

**Rollback.** A model switch forces a full 384→768 reindex. If reindex time, index
size or recall regress beyond budget, revert to bge-small. Never mix dims.

---

## Production ops items (owner approval required — not executed here)

These come from the live consultation and are **not** code changes:

1. Remove the orphan MCP server process (PID 91185, old gateway pgid 157) after
   verifying it is not serving — it coexists with PID 95708 on the same DB.
2. Run `hygiene_clean` for the **30 `secret_assignment` findings** (Gmail setup,
   `rabbit.tech` token) **after a backup**; add a capture-time secret denylist so
   they stop being stored.
3. Consolidate the **535-row unconsolidated backlog** and measure recall precision
   before/after.
4. Calibrate `MNEMOSYNE_RECALL_SCORE_FLOOR` from Sprint 1 before enabling it.
5. Any `hermes cron` job that triggers consolidation stays **unpinned** until a
   real `MODEL_OK` verification call succeeds; then pin the verified model.

## Cross-cutting acceptance (all sprints)

- Offline smoke test still passes with `network_attempts: []`.
- Capture-containment and engine-backed provider contract lanes pass.
- No `site-packages` edits; engine changes in `integrations/engine-patches/`.
- New gates registered in `scripts/checks.sh`.
- Candidate-repo issue filing checks `has_issues` first; all three report `true`.
