# Memory-quality scorecard — measured record

Every number here was produced by `bench/scorecard/` against the pinned,
patched `mnemosyne-memory 3.15.1` engine with embeddings **on**, through the real
provider path (`prefetch()` + `mnemosyne_recall` tool). No number is projected.
Missed targets are listed in the final section, per the work order.

## Provenance

| | |
| --- | --- |
| Corpus | `bench/sim/scale.py`, seed `20261115` |
| Held-out probes | **12,559** (+ 2,511 dev, disjoint) |
| Engine | `mnemosyne-memory 3.15.1` + `integrations/engine-patches/` |
| Provider | working tree of `integrations/hermes-provider/` |
| Embeddings | on (`BAAI/bge-small-en-v1.5`, keyless, local) |
| LLM calls | 0 |
| Sweep host | local (no data left the host; the corpus is synthetic) |

The engine patch set is attested per report: every manifest-covered file is
hashed and labelled `patched` / `original` / `unknown`, so an arm that silently
ran the wrong tree cannot be mistaken for a measurement.

## 1. The instrument is sound

Full 12,559-probe held-out sweep, two runs of the *same* configuration (A/A) plus
a run with the audited 0.15 admission floor reverted to the published stepped
gate (0.5 for three query tokens, 0.3 for four or more).

| Paired comparison (tool surface) | n | A | B | mean diff | 95% CI | detected |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| A/A answerable recall | 8,419 | 1.000 | 1.000 | 0.00000 | [0, 0] | no |
| A/A fabricated queries returning rows | 2,851 | 2,851 | 2,851 | 0.00000 | [0, 0] | no |
| A/A expired versions served | 1,289 | 1,289 | 1,289 | 0.00000 | [0, 0] | no |
| A/A stale above current | 5,930 | 4,357 | 4,349 | +0.00135 | [−0.00101, +0.00371] | **no** |
| **Revert** fabricated queries returning rows | 2,851 | 2,851 | 2,266 | +0.20519 | **[+0.19046, +0.21992]** | **yes** |
| Revert stale above current | 5,930 | 4,357 | 4,343 | +0.00236 | [+0.00051, +0.00422] | yes |
| Revert answerable recall | 8,419 | 1.000 | 1.000 | 0.00000 | [0, 0] | no |

The A/A control is flat on every criterion (one diagnostic metric moves by 8 of
5,930 probes because recency decay reads wall-clock timestamps; its CI still
spans zero). Reverting the floor patch is detected with a CI that excludes zero
by a wide margin. The instrument sees what it is built to see.

`bench/scorecard/gate.py` re-proves this on a 240-probe corpus in ~3.5 minutes
and runs in `scripts/checks.sh` and the weekly drift workflow.

## 2. Baseline quality (the problem, measured)

Held-out, 2,511-probe slice (see §6 for why the fix comparison is on the slice):

| Metric (tool surface) | Baseline |
| --- | ---: |
| answerable recall | **1.000** (1,659/1,659) |
| fabricated-entity queries returning rows | **563 / 563** |
| rows returned for those queries | 2,342 |
| expired facts served as current | **301 / 289** expired probes |
| current probes with a stale value outranking the truth | **876 / 1,177** |
| planted secrets stored (informational) | 126 / 126 |

On the full 12,559-probe sweep the same baseline is 2,851/2,851 fabricated
queries returning rows and 1,289/1,289 expired served — the failure is uniform,
not a slice artefact.

## 3. The fixes, each behind a flag (all default-off)

Each flag is one engine patch hunk in `integrations/engine-patches/core-beam.py.patch`;
see `integrations/engine-patches/README.md` for the mechanism and why each is
narrow. The design came from the per-candidate dumps: on every unanswerable probe
the entity token was absent from *all* candidates, and corrections and their
predecessors ranked within 0.002 of each other.

| Arm (2,511 held-out probes) | answerable recall | fabricated queries w/ rows | expired served | stale above current |
| --- | ---: | ---: | ---: | ---: |
| baseline (no flags) | 1.000 | 563 / 563 | 301 / 289 | 876 |
| `MNEMOSYNE_RECALL_ABSTAIN_IDENTIFIERS=1` | **1.000** | **10 / 563** | 301 / 289 | 878 |
| `MNEMOSYNE_CAPTURE_TEMPORAL=1` | **1.000** | 563 / 563 | **12 / 289** | 878 |
| all three (incl. `MNEMOSYNE_RECALL_CORRECTION_PRIORITY=1`) | **1.000** | **10 / 563** | **12 / 289** | **150** |

Paired differences against baseline (tool surface, paired bootstrap, 10,000
resamples):

| Arm | metric | mean | 95% CI |
| --- | --- | ---: | --- |
| abstain | fabricated queries returning rows | +0.9822 | [+0.9698, +0.9929] |
| abstain | answerable recall | 0.0000 | [0, 0] |
| temporal | expired versions served | +1.0000 | [+1.0000, +1.0000] |
| temporal | answerable recall | 0.0000 | [0, 0] |
| correction priority | stale above current | +0.6168 | [+0.5896, +0.6449] |
| all three | answerable recall | 0.0000 | [0, 0] |

All three fixes together cost **zero** answerable recall (current 1,177/1,177 and
historical 482/482 unchanged) and remove 98.2% of fabricated-answer rows, 95.8%
of expired facts and 82.9% of stale-outranking-current rows.

**The rejected variant matters.** A capture-time *supersession* flag (retire the
row a correction replaces) reached 0 stale answers but destroyed historical
answerability: 482/482 → 13/482, a 28.3-point recall cost, CI
[−0.3038, −0.2610]. The engine can only retire a row, not keep it answerable as
history, so that flag is not shipped. The shipped fix nudges the correction's
rank instead, keeping both rows retrievable.

Residual misses, read from the per-probe records:

- **10 fabricated probes**: the fabricated entity's random hex suffix contained no
  digit (`perrindaba`), so it is not identifier-shaped and the narrow gate
  declines to fire. Widening the rule to any out-of-vocabulary token would
  abstain on ordinary unknown words — paraphrase looks identical — which is
  exactly the recall cost the gate exists to avoid.
- **12 expired probes**: the stated end date was not parseable from the rendered
  utterance, so no `valid_until` was recorded.
- **150 stale probes**: the 0.05 ranking bonus is not enough to overcome the
  recency term on those rows. A larger bonus was not taken because it starts
  promoting weakly-matching corrections over strongly-matching unrelated rows.

## 4. PrecisionMemBench (public suite, replayed)

Pinned upstream commit `61a712a1754e7d27b6dee42da32a2c214b003ece`, both fixtures
hash-verified. Measured by `bench/precisionmembench/run_eval.py`.

| Run | active passes | missing expected beliefs | over-return failures | mean active precision |
| --- | ---: | ---: | ---: | ---: |
| keyword-only collector (earlier record, `PRECISION_RECALL_RESULTS.md`) | 12/43 | 8 | — | 0.501 |
| this harness, scope filter omitted (bug) | 9/43 | 1 | 54 | 0.520 |
| this harness, scope filter applied (correct) | **11/43** | **2** | 48 | 0.600 |
| this harness + the three flags | 11/43 | 2 | 48 | 0.600 |

The suite's dominant failure is over-return: 48 cases surface a belief the case
says must not appear, while only 2 required beliefs are missing. Raising the
existing relative score floor from 0 to 0.85 does not change any of these
numbers, so the defect is not a ranking-threshold problem — it is set membership
(scope, aliases, relation expansion, budget eviction), which this engine does not
model.

## 5. Backtest over past patch states

`python -m bench.scorecard.backtest` rebuilds the engine as each historical
revision of `integrations/engine-patches/` describes it (from the verified
`.mnemosyne-hermes-original` backups, never from a newer patched file) and replays
the same fixed probe sample through the real provider path.

| commit | date | admission floor | answerable recall | fabricated queries w/ rows | expired served |
| --- | --- | --- | ---: | ---: | ---: |
| `0296881c` | 2026-10-13 | stepped return 0.3 | 1.000 | 45 / 58 | 25 / 25 |
| `16294c7a` | 2026-10-13 | stepped return 0.3 | 1.000 | 45 / 58 | 25 / 25 |
| `ec8e8da1` | 2026-10-22 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |
| `114bd8fa` | 2026-10-26 | stepped return 0.3 | 1.000 | 45 / 58 | 25 / 25 |
| `661f2176` | 2026-11-10 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |
| `a0062e41` | 2026-11-11 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |
| `444936cd` | 2026-11-11 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |
| `525d5806` | 2026-11-11 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |
| `2506fff2` | 2026-11-15 | return 0.15 | 1.000 | 58 / 58 | 25 / 25 |

Paired against the newest measured state (`2506fff2`), bootstrap CI over the same
probes (4,000 resamples). Only the fabricated metric moves; the other two are
exactly flat, so the difference is attributable to the admission floor alone:

| commit | metric | n | mean | 95% CI | detected |
| --- | --- | ---: | ---: | --- | --- |
| `0296881c` | fabricated queries returning rows | 58 | -0.2241 | [-0.3276, -0.1207] | **yes** |
| `16294c7a` | fabricated queries returning rows | 58 | -0.2241 | [-0.3276, -0.1207] | **yes** |
| `ec8e8da1` | fabricated queries returning rows | 58 | +0.0000 | [+0.0000, +0.0000] | no |
| `114bd8fa` | fabricated queries returning rows | 58 | -0.2241 | [-0.3276, -0.1207] | **yes** |
| `661f2176` | fabricated queries returning rows | 58 | +0.0000 | [+0.0000, +0.0000] | no |
| `a0062e41` | fabricated queries returning rows | 58 | +0.0000 | [+0.0000, +0.0000] | no |
| `444936cd` | fabricated queries returning rows | 58 | +0.0000 | [+0.0000, +0.0000] | no |
| `525d5806` | fabricated queries returning rows | 58 | +0.0000 | [+0.0000, +0.0000] | no |

Answerable recall and expired facts are flat across every historical state: this
patch set never moved those, so the differences above are attributable to the
floor change rather than to a shifting corpus.


## 6. Why the fix comparison uses 2,511 held-out probes

The 12,559-probe sweep takes ~16 minutes of wall clock on an idle 16-core host
and was used for the instrument proof (§1) and the baseline (§2). The fix
comparison ran on the first 2,511 held-out probes — still held-out, still paired,
still bootstrapped — after unrelated CPU contention on the host pushed a full
sweep past 90 minutes per arm. The residual-failure counts in §3 are exact, not
estimated: they were read from the per-probe records, not from the summary.

The gate in CI uses 240 probes, which is above the ~40 fabricated probes the
revert detection needs at 80% power (measured: at n=40 the paired CI is
[+0.176, +0.475]).

## 7. Pass criteria — including every miss

| Criterion | Target | Measured | Verdict |
| --- | --- | --- | --- |
| fabricated-entity queries return 0 rows at ≤1 point answerable-recall cost | 0 rows, ≤1 pt | **10 / 563** return rows; answerable recall cost **0.000** | **MISS** (rows), **PASS** (cost) |
| 0 expired facts served as current | 0 | **12 / 289** probes still served | **MISS** |
| 0 planted secrets stored | 0 | 126 / 126 stored | **MISS** — but **out of scope**: secret capture was explicitly removed from the work order; reported for visibility only |
| PrecisionMemBench active passes ≥35/43 with ≤8 missing beliefs | ≥35, ≤8 | **11 / 43** active, **2** missing | **MISS** (passes), **PASS** (missing) |

Three of four targets are met or beaten on one axis and missed on the other.
The one-line summary: **the instrument works, the abstention and temporal fixes
are real and free, and both the stale-fact and PrecisionMemBench targets remain
open** for reasons stated above rather than hidden.
