# Grading rubric — recall-quality

Every check is programmatic and deterministic. No model grades any case, so the
score cannot drift between runs and costs nothing per rep. (There is no Claude
reachable in this environment — no `ANTHROPIC_API_KEY`, no `~/.hermes/config.yaml`
— so a judge-based grader was not an available option, and this repo is keyless by
design anyway.)

## What is being measured

The shipped CLI, as a subprocess, against a real store:

```
mnemosyne-lite recall --db-path data/corpus.db --query "<case>" --max-results 5 --format json
```

Everything sits on the measured path: argument parsing, the FTS5 index, BM25
ordering, JSON emission. Only fixture construction (building the corpus DB) uses
the storage API directly.

## Relevance

A returned row is **relevant** to a positive case when any of the case's
`relevant` strings occurs in that row's `content`, case-insensitively. The labels
are verbatim spans from the corpus rows that answer the question, so relevance is
substring containment — no fuzzy matching, no prefix guessing, no model.

For a negative case no row is relevant: the question's answer does not exist in
the corpus, so **any** row returned is a false positive.

## Metrics

| id | label | polarity on positive | polarity on negative |
|---|---|---|---|
| `hit_at_5` | hit@5 | a relevant row is in the top 5 | the store returned **nothing** |
| `hit_at_1` | hit@1 | a relevant row is first | the store returned nothing |
| `mrr` | MRR | 1 / rank of the first relevant row | 1.0 when nothing is returned |
| `precision_at_5` | no-junk@5 | every returned row was relevant | the store returned nothing |

**The polarity flips by case kind.** That is why `tags[1]` is `positive` or
`negative`, and why a single mean across both is meaningless. Read the per-group
rows: `POSITIVE (test)` and `NEGATIVE (test)` are two different questions —
"did it find the answer" and "did it stay quiet when there was nothing to say".

`no-junk@5` is vacuously true when nothing is returned. Returning nothing is not
returning something wrong; scoring it 0 would conflate "abstained" with
"surfaced junk". An earlier draft of this rubric did conflate them and was
corrected before any baseline was recorded.

## Considered and deliberately left out

- **A judge / rubric score.** No reachable Claude, and for `content`-containment
  answers a programmatic check is strictly cheaper and more reliable.
- **nDCG.** With one or two relevant documents per case it tracks MRR and adds a
  discount term that changes no decision here.
- **A recall-side synonym expansion metric.** The pre-pull store had a
  domain-vocabulary feature (`mnemosyne-lite` has no synonym surface any more).
  Measuring it would require a second labelled set; noted as future work rather
  than smuggled in.
- **Latency as a gate.** `recall_ms` is recorded as a side-channel but is
  process-inclusive (Python interpreter start plus SQLite open), so it is not a
  clean latency measurement — `bench/measure.sh` is the right instrument for
  that. It is not a pass/fail criterion here.

## Splits

| split | cases | purpose |
|---|---|---|
| `train` | 32 (27 positive + 5 negative) | the dev set; used while developing the grader |
| `test` | 62 | 54 positives + 8 negatives, held out; run once, at the end |

## Noise floor

Binary pass rates: roughly `1/sqrt(n * reps)`. The held-out positive split is
n=54 at 2 reps, so the resolution is about **±9.6 points**; the train split
(n=27) about ±13.5. Treat a change smaller than that as noise. The runner prints
95% Wilson intervals per group rather than raw rates for this reason.

Two reps are enough to catch non-determinism; the CI is dominated by case count,
not reps. Adding reps to shrink a 9.6-point floor is the expensive route —
adding cases is the cheap one.

## Provenance

Every reported number must carry: commit (`git rev-parse --short HEAD`), the
dataset revision (`build_cases.py --check` prints the cases sha), `k`, and the
harness digest (`data/harness.sha`). A number without those is not comparable.
