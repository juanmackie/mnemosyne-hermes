# Proposed recall cases

These are the six fixed inputs retained from `bench/measure.sh`. The runner sends only the query and its filters to `PythonMemoryStorage.recall()`; the expected checks stay in the eval harness.

| ID | Input (`query`; filters) | Expected outcome |
| --- | --- | --- |
| `common_term_namespace` | `memory`; namespace `agent:hermes`, max 10 | Exactly 10 results; each contains `memory` and has namespace `agent:hermes`. |
| `rare_term` | `xylophone`; max 100 | Exactly 7 results; each contains `xylophone`. |
| `multiword_phrase` | `midnight lantern protocol`; max 100 | Exactly 2 results; each contains all three tokens. |
| `full_scan_miss` | `zzzqqqnomatch`; max 100 | Zero results. |
| `importance_floor` | `project`; namespace `agent:hermes`, max 10, importance >= 5 | Between 1 and 10 results; each contains `project`, matches the namespace, and meets the importance floor. |
| `wide_limit` | `memory`; max 50 | Exactly 50 results; each contains `memory`. |

The fixture uses the benchmark's seeded synthetic 3,000-row corpus. `xylophone`, the phrase, and the miss are explicitly planted/asserted by the existing benchmark; the other cases cover namespace, importance, and result-limit behavior. These are workload probes, not a representative sample of production queries.
