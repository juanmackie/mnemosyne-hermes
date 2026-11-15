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
| `MNEMOSYNE_RECALL_ABSTAIN_IDENTIFIERS` | **1.000** | **10 / 563** | 301 / 289 | 878 |
| historical broad-anchor experiment (removed) | **0.819** | **0 / 563** | 301 / 289 | 693 |
| `MNEMOSYNE_CAPTURE_TEMPORAL` | **1.000** | 563 / 563 | **0 / 289** | 876 |
| `MNEMOSYNE_RECALL_CORRECTION_PRIORITY` | **1.000** | 563 / 563 | 301 / 289 | **150** |
| all recommended flags | **1.000** | **10 / 563** | **0 / 289** | **150** |

Paired differences against baseline (tool surface, paired bootstrap, 10,000
resamples):

| Change | metric | mean | 95% CI |
| --- | --- | ---: | --- |
| abstain (narrow) | fabricated queries returning rows | +0.9822 | [+0.9698, +0.9929] |
| abstain (narrow) | answerable recall | 0.0000 | [0, 0] |
| abstain (broad) | fabricated queries returning rows | +1.0000 | [+1.0000, +1.0000] |
| abstain (broad) | answerable recall | **−0.1808** | [−0.1996, −0.1622] |
| temporal | expired versions served | +1.0415 | [+1.0208, +1.0657] |
| temporal | answerable recall | 0.0000 | [0, 0] |
| correction priority | stale above current | +0.6168 | [+0.5896, +0.6449] |

The original digit-only identifier rule left 10 fabricated probes whose random
hex suffix contained no decimal digit. Extending that same narrow rule to
lowercase tokens with a four-character hexadecimal suffix closed the gap on the
full held-out corpus without losing answerable recall; see the follow-up below.

**Two variants were measured and rejected**: a capture-time *supersession* flag
reached zero stale answers but cost 28.3 points of historical recall
(482/482 → 13/482) because the engine can only retire a row; and the broad
abstention rule above, which meets the zero target at 18 points of recall, above
the one-point budget.

## 4. PrecisionMemBench (public suite, replayed)

Pinned upstream commit `61a712a1754e7d27b6dee42da32a2c214b003ece`, both fixtures
hash-verified. Measured by `bench/precisionmembench/run_eval.py`.

| Run | active passes | missing expected beliefs | over-return failures | mean active precision |
| --- | ---: | ---: | ---: | ---: |
| keyword-only collector (earlier record, `PRECISION_RECALL_RESULTS.md`) | 12/43 | 8 | — | 0.501 |
| earlier replay, scope filter omitted | 9/43 | 1 | 54 | 0.520 |
| earlier replay, first-scope-only adapter | 11/43 | 2 | 48 | 0.600 |
| corrected adapter semantics, current engine | **18/43** | **1** | **32** | **0.705** |

The corrected replay routes open questions to their own output, enforces user
and current-state isolation, and fans out across active scopes as the upstream
adapter does. These adapter fixes changed the measured result; they did not
change engine retrieval. Thirty-two cases still return extra beliefs, while one
required belief is missing.

Relative score-floor experiments show a precision/recall trade rather than a
passing setting: floor `0.70` produced 21/43 active passes with 3 missing;
`0.75` produced 24/43 with 10 missing; and `0.85` produced 29/43 with 16
missing. A context limit of 8 at floor `0.85` had the same 29 passes and 16
missing. None meets both target bounds.

Additional retrieval diagnostics (2026-11-15) did not produce a valid engine
fix. Extreme vector/FTS weight settings returned the same belief sets as the
default because the 20-row recall budget already covered each case's candidate
set. Raw provider-score cutoffs of `0.30`, `0.35`, and `0.40` produced 19/43
passes with 4 missing, 23/43 with 16 missing, and 26/43 with 29 missing.
Polyphonic recall produced 1/43 passes with no missing beliefs. A diagnostic
filter using the fixture's structured canonical names and aliases reached
39/43 with 1 missing, but it is not part of the pinned upstream adapter and
uses metadata that the engine was not given; it does not count as a provider
improvement and was rejected.
An offline whole-token-overlap gate using only each query and returned belief
text produced 18/43 passes with 1 missing at zero required matches, 17/43 with
3 missing at one match, 15/43 with 34 missing at two matches, and 4/43 with 53
missing at three matches; it was rejected as a useful global filter.

The retained per-case replay groups the remaining 25 failed cases as follows:
alias resolution 10/22 pass; scope disambiguation 0/4; ranking stability 0/2;
fuzzy matching and prefix guards 1/5 (including the one missing belief);
budget/capacity 0/2; and design boundaries 0/1. Counter-signal retrieval
(2/2), relation expansion (4/4), and type routing (1/1) pass. The 24
over-return cases contain 36 extra-belief assertions. This points to precise
candidate selection across aliases and scope as the dominant gap, not missed
answerable retrieval in general.

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
| fabricated-entity queries return 0 rows at ≤1 point answerable-recall cost | 0 rows, ≤1 pt | extended narrow rule: **0 / 2,851** rows at **0.000** answerable-recall cost | **PASS** |
| 0 expired facts served as current | 0 | **0 / 289** | **PASS** |
| 0 planted secrets stored | 0 | **0 / 629** stored on the full held-out run | **PASS** |
| PrecisionMemBench active passes ≥35/43 with ≤8 missing beliefs | ≥35, ≤8 | **18 / 43** active, **1** missing | **MISS** (passes), **PASS** (missing) |

### Capture redaction follow-up (2026-11-15)

The initial 126/126 result above predates the capture-time guard. After wiring
redaction into the pinned engine's shared `BeamMemory.remember()` boundary, a
fresh embeddings-on synthetic run over 12 held-out lives (240 probes) stored
**0 of 12** planted secrets. The engine contract regression also reads back
both a labeled API key and an unlabeled high-entropy token and confirms only
`[REDACTED]` remains. The full 12,559-probe rerun has not yet been completed;
the original full-corpus number remains historical baseline evidence.

### Held-out retrieval follow-up (2026-11-15)

On the full 12,559-probe held-out split, the extended narrow identifier rule,
temporal validity guard, and correction priority were run together through the
provider tool and prefetch paths with embeddings enabled: answerable recall
**1.000**, fabricated queries returning rows **0/2,851**, expired facts served
**0**, and planted secrets stored **0/629**. The run had 0 probe errors and
took 908.7 seconds. The label set is synthetic; PrecisionMemBench remains a
separate public-suite result below.

**Summary.** The instrument works and is proven (A/A flat, revert detected). The
extended narrow abstention rule, temporal fix, and capture redaction meet their
targets on the full held-out simulator split with no answerable-recall loss.
The corrected PrecisionMemBench replay passes 18/43 active cases with 1 missing
belief; its 35-pass target remains unmet. Stale facts are reduced 82.9% and the
historical answers survive. The remaining benchmark problem is over-return
(32 cases), and measured score-floor changes trade that error for missing
beliefs without meeting both targets.

### Default-path risk and score-threshold audit (2026-11-15)

A fresh full held-out run of the pinned engine's default settings measured all
12,559 probes through the tool and prefetch paths with embeddings on, 628 planted
secrets, and zero probe errors. This is the untreated baseline used to quantify
the failure modes; it does not contradict the opt-in-fix results above.

| Failure type | Tool risk / denominator | Prefetch risk / denominator | Tool answerable coverage |
| --- | ---: | ---: | ---: |
| Fabricated entity returned | 2,851 / 2,851 (100%) | 2,851 / 2,851 (100%) | N/A |
| Expired fact served | 1,289 / 1,289 (100%) | 1,289 / 1,289 (100%) | N/A |
| Stale value ranked above current | 4,354 / 5,930 (73.4%) | 157 / 5,930 (2.6% lack current answer) | 5,930 / 5,930 (100%) |
| Historical answer present | — | 2,489 / 2,489 (100%) | 2,489 / 2,489 (100%) |
| Planted secrets persisted | 0 / 628 (0%) | — | — |

An offline score-floor sweep on captured tool candidates tests whether the
engine's composite score separates the failure types. At floors `0.30` and
`0.35`, answerable coverage stayed at 100%, but fabricated and expired risks
stayed at 100%, and stale-above-current stayed at 73.4%. At `0.40`, fabricated
risk fell only to 2,266 / 2,851 (79.5%); expired and stale risks did not move,
while answerable coverage stayed at 100%. The floor reduced returned rows per
probe (fabricated: 4.15 → 2.90, about 30% fewer rows; expired: 4.01 → 3.16,
about 21% fewer; current: 5.17 → 4.32, about 16% fewer), but did not make
those answers safe. Row count is only a context-size proxy; token or monetary
cost was not measured. This is diagnostic filtering of captured tool
candidates, not an implemented provider behavior. It gives no basis for
shipping a global score cutoff.

The independent 2,511-probe dev split was also run through the same default
provider with embeddings on (198.5 seconds, zero errors): tool recall was
100% for 1,690 answerable probes, while 579/579 fabricated probes returned
rows, 242/242 expired probes served expired facts, and stale values outranked
current values on 818/1,215 current probes. Prefetch missed the current answer
on 13/1,215 current probes and served expired facts on 242/242 expired probes.
The capture audit stored 0/127 planted secrets. This dev run is descriptive;
no thresholds were selected from it.
