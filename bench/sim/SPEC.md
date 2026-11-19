# MnemoSim v0

MnemoSim generates deterministic, synthetic lives. The simulator owns ground truth; templates only render events into session messages. No language model decides labels.

## Event record

Each JSONL event has these required fields: `event_id`, `life_id`, `domain`, `subject`, `attribute`, `value`, `valid_from`, `valid_to`, `transaction_at`, `provenance`, `disposition`, and `injection`. `valid_*` describes when a fact is true in the simulated world. `transaction_at` describes when the store learns it. Provenance is one of `user_stated`, `tool_observed`, `inferred`, `hypothetical`, or `quoted_third_party`. Disposition is `store` or `never_store`; hypothetical statements, secrets, and private data about other people must be `never_store`.

Sessions may expose an event directly, indirectly, through a tool observation, through a later correction, or not at all. A probe includes the exact answer set and an `as_of` time. A never-told probe has an empty answer set and is labelled unanswerable. Injection content is data, never an instruction to the evaluator.

## v0 boundaries

`python bench/sim/generate.py --seed N --output FILE` writes one canonical life spanning personal, coding, and homelab domains. The seed is included in the output. Output contains synthetic content only. This first fixture demonstrates schema and deterministic labels; it does not claim the roadmap scale target or language-model surface realization. Public benchmarks remain eval-only and are not downloaded or used here.

## Probe kinds

Every probe has `probe_id`, `kind`, `query`, `as_of`, `answers`, `must_abstain`
and `entity`. Kinds:

| Kind | Answers | Correct behaviour |
| --- | --- | --- |
| `answerable_current` | the current value | serve it; a superseded value must not outrank it |
| `answerable_historical` | the value inside a past window | serve it for that `as_of` |
| `expired` | none | serve nothing |
| `fabricated` | none | serve nothing |
| `carried_lesson` | the lesson's distinctive token | serve the lesson with its provenance line |

`carried_lesson` is opt-in (`make_corpus(..., lessons=True)`), and then adds one
lesson per life: `life["lessons"]` holds the write (text, `model_a`, `model_b`),
and the probe carries `model_a`/`model_b`/`lesson_id`. The write happens under
`model_a`, the query under `model_b`; the graded facts are the same as for any
other probe plus `learned under …` provenance. Without `lessons=True` the corpus,
its digest and its probe order are exactly what they were before the kind
existed.
