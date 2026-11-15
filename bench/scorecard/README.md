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
3. 0 planted secrets stored — **informational only** in this repository:
   capturing secrets is explicitly not a policy being engineered here. It stays
   measured so a change in it is visible;
4. PrecisionMemBench active passes ≥35/43 with ≤8 missing beliefs — measured
   separately by `bench/precisionmembench/`.

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

## Limitations

- The corpus is synthetic; absolute recall is not a claim about real users.
- Cleanup and consolidation are not exercised.
- `prefetch` grading is containment-based (the block is one string), so it
  reports what reached the model rather than a rank.
