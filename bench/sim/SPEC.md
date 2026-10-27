# MnemoSim v0

MnemoSim generates deterministic, synthetic lives. The simulator owns ground truth; templates only render events into session messages. No language model decides labels.

## Event record

Each JSONL event has these required fields: `event_id`, `life_id`, `domain`, `subject`, `attribute`, `value`, `valid_from`, `valid_to`, `transaction_at`, `provenance`, `disposition`, and `injection`. `valid_*` describes when a fact is true in the simulated world. `transaction_at` describes when the store learns it. Provenance is one of `user_stated`, `tool_observed`, `inferred`, `hypothetical`, or `quoted_third_party`. Disposition is `store` or `never_store`; hypothetical statements, secrets, and private data about other people must be `never_store`.

Sessions may expose an event directly, indirectly, through a tool observation, through a later correction, or not at all. A probe includes the exact answer set and an `as_of` time. A never-told probe has an empty answer set and is labelled unanswerable. Injection content is data, never an instruction to the evaluator.

## v0 boundaries

`python bench/sim/generate.py --seed N --output FILE` writes one canonical life spanning personal, coding, and homelab domains. The seed is included in the output. Output contains synthetic content only. This first fixture demonstrates schema and deterministic labels; it does not claim the roadmap scale target or language-model surface realization. Public benchmarks remain eval-only and are not downloaded or used here.
