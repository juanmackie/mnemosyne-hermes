# Memory-quality scorecard

The instrument that measures whether this memory stack retrieves the *right*
fact, abstains when it has none, and stops serving facts that are no longer
true. Synthetic, deterministic, keyless.

## What it measures

Every probe is graded on two surfaces an agent actually sees:

| Surface | How it is exercised |
| --- | --- |
| **tool** | `MnemosyneMemoryProvider.handle_tool_call("mnemosyne_recall", …)` — an explicit agent lookup |
| **prefetch** | `MnemosyneMemoryProvider.prefetch(query)` — what is injected automatically |

Each probe is one of four kinds, and the simulator's event timeline is the only
source of truth:

| Kind | Correct behaviour | Reported as |
| --- | --- | --- |
| `answerable_current` | the current value is returned | answerable recall |
| `answerable_historical` | the value valid at `as_of` is returned | answerable recall |
| `fabricated` | **nothing** — the entity exists nowhere | fabricated queries returning rows |
| `expired` | **nothing** — the validity window ended | expired facts served as current |

The four pass criteria (see `RESULTS.md` for the measured values):

1. fabricated-entity queries return 0 rows, at ≤1 point answerable-recall cost;
2. 0 expired facts served as current;
3. 0 planted secrets stored — the pinned engine redacts credential formats and
   high-entropy token-shaped strings at the shared `remember` boundary before
   sanitization, embedding, or persistence;
4. PrecisionMemBench active passes ≥35/43 with ≤8 missing beliefs — measured
   separately by `bench/precisionmembench/`.

## Carried lessons (P55)

A lesson written under one model is often recalled under another. The
`carried_lesson` probe measures whether that still works and whether the recall
tells the truth about where the lesson came from:

- the life carries one lesson written through `mnemosyne_remember` with
  `MNEMOSYNE_MODEL_ID` set to `model_a`, in the shape the provider's guidance
  asks for (root cause, what was tried, what was expected, what happened);
- the declared model is cleared after the lesson ingest, and the probe's query is
  issued under no declared model (`model_b` names the model the query is made
  "under"); provenance is stamped at write time, so the switch cannot rewrite
  what the store recorded — which is the property being measured;
- the lesson's distinctive token appears nowhere else in the life, so "the
  lesson was returned" is an exact label, not a heuristic.

Pass criteria, on both surfaces (tool and prefetch):

1. the lesson is returned at the same rate as the untagged `answerable_current`
   control (within 0.1 in the repo gate; A/A must show a zero paired difference);
2. the `learned under …` provenance line is present on every returned lesson;
3. zero lessons are reported under a model other than the one they were written
   under.

The probe is opt-in per corpus (`python -m bench.scorecard.cli corpus --lessons`)
so a corpus built without it keeps its exact digest. `bench/scorecard/gate.py`
always turns it on: the four pass criteria above are unchanged, and the lesson
criteria are checked alongside them.

## Why the corpus looks like this

`bench/sim/scale.py` makes unique synthetic lives. Two design choices carry the
labels:

- **Entity tokens are unique per (life, slot).** A fabricated-entity probe is
  therefore honest: its query shares ordinary topic vocabulary ("router
  maintenance window") with stored facts, so a lexical retriever *does* return
  rows — but the named entity exists nowhere. Correct behaviour is zero rows.
- **Expired facts stay in the timeline** with an explicitly stated end date
  ("… until 2025-06-01"). Serving one later is a stale answer, not an
  unknowable one.

## Paired probes and the bootstrap

The unit of analysis is one probe measured under two arms. `12,559` held-out
probes is the sample size at which a 2.5% paired effect is detectable at 80%
power (`bench/mnemobench/power-record.json`). Every comparison reports a
paired bootstrap CI over probes; the resample seed is derived from the data, so
a report is reproducible from its JSON.

## Usage

```bash
# 1. corpus (12,559 held-out + 2,511 dev probes)
python -m bench.scorecard.cli corpus --output .scorecard/corpus.json

# 2. one arm (resumable; each life is checkpointed)
python -m bench.scorecard.cli run --arm baseline \
  --corpus .scorecard/corpus.json --output .scorecard/arm-baseline.json --workers 8

# 3. an arm with a flag-gated fix
MNEMOSYNE_RECALL_ABSTAIN_IDENTIFIERS=1 \
  python -m bench.scorecard.cli run --arm abstain \
  --corpus .scorecard/corpus.json --output .scorecard/arm-abstain.json --workers 8

# 4. paired comparison with CIs
python -m bench.scorecard.cli compare --baseline .scorecard/arm-baseline.json \
  --candidate .scorecard/arm-abstain.json --corpus .scorecard/corpus.json \
  --output .scorecard/compare-abstain.json

# 5. prove the instrument (CI-sized)
python bench/scorecard/gate.py
```

Requirements: the pinned patched engine (`mnemosyne-memory>=3.15.1,<3.16` plus
`scripts/apply_engine_patches.py`) and the provider on `PYTHONPATH`. Embeddings
stay ON: the opt-out variables are **scrubbed**, not merely unset, so an
operator shell with `MNEMOSYNE_EMBEDDINGS_OFF=1` cannot silently measure the
keyword-only path.

Runs are CPU-bound on ingestion, so `run` is parallel over lives (each life is
an independent store) and checkpoints every finished life under
`<output>/lives/<arm>-<digest>/`. Rerunning resumes; `--no-resume` forces a full
remeasurement.

## Per-candidate score dumps

`--dump-every N` records, for every sampled probe, each engine candidate with
its score components (`keyword`, `fts`, `dense`, `importance`, `recency_decay`,
`final`), tier, source path, kept/dropped state, and three facts derived from
the row text rather than from engine internals: whether the probe's entity, its
answer, or one of its stale values appears in that row. Every row carries
`probe_id`/`kind`/`entity`, so a dump can be filtered and joined without
positional grouping.

The two capture/recall fixes in `integrations/engine-patches/` were chosen from
these dumps: for unanswerable probes the entity token was absent from *all*
candidates, and for corrected facts the stale value ranked at or above the
correction.

## What this does not measure

- Paraphrase and multi-hop reasoning: probes are single-fact lookups.
- Latency and cost: those live in `bench/measure.sh` and the prefetch eval.
- A live Hermes gateway: this drives the provider object directly.
- Anything requiring an LLM: no model is called, no key is needed.

Secret detection is measured separately from retrieval. Credential redaction
runs at the engine's shared capture boundary, before the value can reach
storage or embeddings; a failing secret criterion makes the scorecard fail.

## Limitations

- The corpus is synthetic; absolute recall is not a claim about real users.
- Cleanup and consolidation are not exercised.
- `prefetch` grading is containment-based (the block is one string), so it
  reports what reached the model rather than a rank.

## Staged sleep retention experiment (experimental)

`python -m bench.scorecard.retention --corpus .scorecard/corpus.json --output .scorecard/retention.json --workers 4` runs four stages in disposable stores. It compares no sleep, forced sleep, forced sleep with accelerated episodic degradation, source deletion and deliberate summary loss, plus an A/A repeat. Exit 2 means an instrument control failed or the command arguments were invalid; exit 1 means controls passed but a quality invariant failed; exit 3 means the measurement failed unexpectedly; exit 0 means both controls and quality invariants passed. A/A tolerates at most 1% of observations changing, with a minimum allowance of one observation, to accommodate wall-clock ranking drift. This is an operational tolerance, not a statistical equivalence claim. It requires the exact manifest-attested patched 3.15.1 engine. Synthetic stored events enter through `mnemosyne_remember`; probes use recall and prefetch. No user database is opened. Optional LLM work is disabled and sleep uses the engine's keyless fallback.

Current-answer probes enter the matrix when their final labelled value is first ingested. Historical probes are excluded because prefetch cannot specify query time. Fabricated and expired probes run after the final stage. The deletion controls delete working rows after each life's earliest measurable cohort, once without sleep and once after sleep; neither measures production deletion behavior. The post-sleep deletion arm reports whether summaries retain answers. A separate synthetic loss control removes those consolidated sources and replaces the summaries with a placeholder through the provider update path. The instrument compares this loss control with the post-sleep source-deletion arm, so it fails its controls if erasing the summaries causes no additional detectable loss. Degradation ages scratch episodic rows to 2000 and checks row IDs and tiers after each transition. This also changes recency scoring. Keyless tier-1 compression may only truncate long content, so the arm may not exercise summary content loss. Its accelerated timing is a stress test, not a simulation of natural wall-clock aging. Inspect transition results as well as retention.

For the experimental consolidation coverage comparison, pass `--sleep-verify` to a second run against the same corpus and audited engine. The report records this setting. Compare paired sleep-arm records from the two reports; the guard is pinned off when the option is omitted, even if the shell exports the flag (the engine default is on since 3.1.0; every measurement arm pins the flag explicitly). Pass `--sleep-demote` the same way to add `MNEMOSYNE_SLEEP_DEMOTE_SOURCES=1` on top of the coverage guard; demotion without the guard demotes nothing, by design.

Tool answer hits and ranks require the entity and value in the same retrieved row, unlike the existing scorecard's answer-only grading, so the two recall rates are not directly comparable.

The report pairs probes and bootstraps whole lives, because probes from one life are correlated. It uses a fixed seed of 719 and defaults to 2,000 resamples (`--resamples` changes that count). A two-life control interval is only an instrument check, not a reliable population confidence interval. The existing 12,559-probe power calculation does not establish power for this staged, clustered analysis. A/A and deletion controls validate the instrument, not product quality. The report's backward-transfer comparisons are candidate minus baseline; zero within a confidence interval is not proof of equivalence. A statistically justified equivalence margin and power analysis are needed before claiming no degradation. Prefetch output is a formatted block, so a value and entity elsewhere in the same block can still be mistaken for a matching fact. No conclusions should be drawn from a failed control, missing stages, or a small run.

A 240-probe development-sized seven-arm run on 2026-11-19 (seed 20261115; `python -m bench.scorecard.cli corpus --probes 240 --dev-probes 0 --probes-per-life 20 --output /tmp/mnemosyne-retention-240-corpus.json`, then the retention command above with that corpus and `--workers 4`; corpus digest `d3dcb205191bebd0efb0d57c5b3e5fc55cf318e9641f1f49b703114cd9184102`) detected its source-deletion and isolated summary-loss controls and matched A/A on both surfaces. In the exploratory post-sleep source-deletion comparison, the mean recall change was −0.274 over 62 probes in 11 lives; the 95% life-cluster bootstrap interval was [−0.585, 0.0] on both surfaces. This small diagnostic does not establish the frequency of loss. The default no-sleep arm returned 164 tool rows and 54 prefetch rows across fabricated probes; its expired-served count was zero in this tool-ingestion run. Thus the zero-fabrication invariant failed. The earlier-cohort backward-transfer difference was zero in this small run, which does not establish safety or equivalence. Originals remain retrievable after sleep, so they can also mask loss in an episodic summary. The existing default-path record in `RESULTS.md` likewise reports fabricated-query failures; its zero-fabrication result applies to an opt-in configuration, not the default.

### Paired coverage-guard check on the same 240-probe corpus, 2026-11-19

A second run with `--sleep-verify` (the experimental `MNEMOSYNE_SLEEP_VERIFY=1` consolidation guard) used the same corpus, seed and engine copy (`eab11d6b…`, the manifest-attested 3.15.1 with the guard patch); corpus digest `d3dcb205191bebd0efb0d57c5b3e5fc55cf318e9641f1f49b703114cd9184102`. Both runs report 12 lives, 62 post-sleep deletion probes in 11 lives and 86 backward-transfer observations. On both surfaces the guard changed measured backward transfer by exactly 0.0 (paired interval [0.0, 0.0], 11 lives), A/A still differed on 0/379 observations, and the working-source deletion control was detected. The post-sleep source-deletion comparison moved from mean −0.274 with a life-cluster interval of [−0.585, 0.0] (not detected) to −0.419 with [−0.754, −0.113] (detected) on both surfaces; the isolated summary-loss control stayed detected on both (mean −0.581, [−0.887, −0.246]). This does not establish that the guard preserves recall; both runs failed the zero-fabrication invariant identically (54 fabricated rows in the sleep arm), and the paired sample is one small corpus. Coverage-failure counts are recorded in `consolidation_log.summary_preview` only, not in the retention report. The raw JSONs are temporary local artifacts.

A third 240-probe run added `--sleep-demote` to `--sleep-verify` on the same corpus digest (`d3dcb205…`). Its engine differs from the verify-only run only in `core/beam.py` (demotion patch, `b98ee520…`). Its controls passed, and it failed the same zero-fabrication invariant. Against the verify-only run, the sleep arm changed 9 of 379 prefetch answer observations, all from miss to hit (0 hit to miss). The tool surface changed on 0 of 379 observations, and the no-sleep arm changed on none. This is one small corpus and one run; it does not establish that demotion improves recall. The flag stayed off by default at the time of this run; the 3.1.0 default change is recorded below.

### Full held-out staged run, 2026-11-19

Generated with `python -m bench.scorecard.cli corpus --probes 12559 --dev-probes 0 --probes-per-life 20 --output /tmp/mnemosyne-retention-full-corpus.json`, then measured with `python -m bench.scorecard.retention --corpus /tmp/mnemosyne-retention-full-corpus.json --output /tmp/mnemosyne-retention-full-results.json --workers 8`. Corpus digest: `da6252f74da7d6481c863d5ac93ed5d9dff416abca046d71fb77ee1b3b2ce142`. This is a synthetic 628-life input; the earlier-cohort backward-transfer comparison covers 4,550 current-answer probes in 609 lives, not every probe in the corpus. Historical probes are excluded.

Both surfaces passed the A/A, working-source deletion and isolated summary-loss controls. A/A differed on 0/19,422 tool observations and 4/19,422 prefetch observations. Compared with no sleep, sleep's backward-transfer difference was 0.0 for tool and −0.00571 for prefetch (95% life-cluster bootstrap interval [−0.01062, −0.00197]). The accelerated sleep-plus-degradation comparison was 0.0 on both surfaces in this sample; its degenerate bootstrap interval does not establish equivalence. Originals remain available in those arms and may mask summary loss.

The exploratory post-sleep source-deletion arm returned fewer targeted answers than sleep alone: tool difference −0.247 [−0.283, −0.215] and prefetch difference −0.281 [−0.319, −0.246], over 2,910 probes in 609 lives. This is a synthetic intervention, not a measured loss from a live store. The deliberately erased-summary control detected further loss on both surfaces.

The quality invariants failed: in the default no-sleep arm, 2,803 of 2,851 fabricated probes returned rows on each surface (8,632 tool rows and 2,803 prefetch rows), and 45 expired facts were served among 1,289 expired probes on each surface. The command exited 1, indicating passed controls and failed quality invariants. Optional LLM consolidation, historical staged probes, natural aging, real user data, and power for a 2.5-point clustered effect remain unmeasured. The earlier 12,559-probe power claim does not establish power here. The raw JSON is a temporary local artifact and is not a published result.

**Provider-corrected full run, 2026-11-19 (628 lives, corpus digest `da6252f7…`).**
Earlier retention runs imported the engine wheel's copy of the provider, not the vendored one. The scorecard now refuses any non-matching import and records `provider_import` in the report. All numbers below come from runs with the vendored provider (`provider_import.matches_vendored: true`) and the audited engine (`core/beam.py` patch hash `273614c8…`). Three runs used the same corpus and engine: default, `--sleep-verify`, and `--sleep-verify --sleep-demote`.

- Controls passed in all three: A/A, deletion, and the summary-loss control.
- The zero-fabrication and expired-served invariants failed in every arm, as documented above. Expired-served was 45 in every arm.
- Backward transfer on the tool surface was 0 in every sleep arm.
- Default (unverified) sleep lost a small, significant amount on prefetch: mean −0.0057, life-clustered CI [−0.0106, −0.0020].
- `--sleep-verify` removed that loss: prefetch backward transfer was 0, CI [0, 0], the same as no sleep. Its fabricated tool rows in the sleep arm were 8,507, against 8,400 for default sleep and 8,632 for no sleep.
- Degradation, with or without `MNEMOSYNE_DEGRADE_VERIFY`, had backward transfer 0. The guard rejected 3,327 of 5,958 eligible steps. This corpus shows no recall loss from degradation to prevent.
- Demotion (`--sleep-demote`, on top of verify) raised prefetch answers in the sleep arm by a mean of +0.0054 per observation (life-clustered CI [+0.0025, +0.0086]). The no-sleep arm's noise floor was +0.0002, CI [−0.0005, +0.0010]. Tool answers did not change, and sleep-arm fabricated tool rows fell from 8,507 to 8,292.

These runs do not establish that the guards clear the zero-fabrication invariant, which fails in every arm. They also do not establish a general gain: one synthetic corpus, keyless summaries, and accelerated degradation.

**Phase 1 closure (2026-11-19).** Phase 1 is closed under the baseline standard: `--sleep-verify` must not add fabricated rows beyond the no-sleep arm. On the 628-life corpus the verified sleep arm returns 8,507 fabricated tool rows, against 8,632 with no sleep. Prefetch is 2,803 in both. Verified sleep therefore passes this standard. It returns 107 more fabricated tool rows than unverified sleep, which is a known trade-off. The strict zero-fabrication invariant still fails in every arm, including no sleep. It is tracked as a separate product item, not as part of Phase 1.

**Phase 2 closure (2026-11-19).** Phase 2 ships option B: `MNEMOSYNE_DEGRADE_VERIFY=1`, a per-step key-token check. On the 628-life corpus, backward transfer for the guarded arm matches no sleep (0 on both surfaces), and the guard rejected 3,327 of 5,958 eligible steps. Option A (`MNEMOSYNE_DEGRADE_ADDITIVE=1`) stays in the engine as an opt-in, off by default. It does not meet the no-sleep match gate. Its prefetch backward transfer is −0.0057, CI [−0.0106, −0.0020], the same as unverified sleep, and its storage cost is unmeasured. Option A is not a shipped default.

**Default change (3.1.0, 2026-11-19).** `MNEMOSYNE_SLEEP_VERIFY`, `MNEMOSYNE_SLEEP_DEMOTE_SOURCES` and `MNEMOSYNE_DEGRADE_VERIFY` now default on in the engine patch, by owner decision after the Phase 1/2 closures above: the guarded arms matched no-sleep backward transfer, and the demote arm's only paired changes were miss→hit. Opt out with an explicit `0`/`false`/`no`/`off`; unset or unrecognized values keep the guard on. `MNEMOSYNE_DEGRADE_ADDITIVE` (option A) remains opt-in. All scorecard arms pin these flags explicitly, so the recorded baselines keep measuring the unguarded behavior.
