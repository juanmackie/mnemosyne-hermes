# MnemoBench power plan

Generated from `power-record.json` by `python bench/mnemobench/power.py`.

The planning approximation assumes paired differences bounded by [-1, 1], an unknown SD conservatively set to 1, two-sided alpha=0.05, and 80% power. These are screening lower bounds; use the paired bootstrap and Holm-corrected registered sweeps for decisions.

Current retrieval fixture: 27 queries; conservative MDE = 0.539.

| Minimum paired effect | Minimum queries (before Holm adjustment) |
|---:|---:|
| 10.0% | 785 |
| 5.0% | 3140 |
| 2.5% | 12559 |

The 27-query replay is a smoke fixture, not a decision-grade evaluation.
