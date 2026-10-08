# Hermes public-prefetch evaluation

**Recorded:** 2026-10-08. Synthetic engineering fixtures on Windows, pinned patched `mnemosyne-memory 3.15.1`, embeddings disabled, no LLM calls. This is not production session traffic.

Baseline commit: `588e589d648e321fcb5b120b1e33d879413e41e3`. Fixture SHA256: `e6c70195d7740a959f4a2bf1afbdb1362225a1bbff28b41ce30102678e4753a0`.

| Outcome | Previous provider | Candidate (cache off) | Candidate (cache on) |
| --- | --- | --- | --- |
| Passed cases | 6/9 | 9/9 | 9/9 |
| Execution errors | 0 | 0 | 0 |
| Largest returned context | 15456 chars | 2965 chars | 2965 chars |

Both candidates preserve all required fact/identity labels, exclude foreign session/profile and expired rows, and contain no prohibited tool/delegation label. The baseline fails two operational-capture cases and the total-output budget. The complete ceiling is 8,000 characters; visible per-line truncation keeps this fixture smaller.

Candidate source SHA256 (base64url): `su45uyQhjca_5SldDVnyukQtd9o4oLtJ-0Z-kkVVscs`.

## Latency observations

Each case calls the public `prefetch()` hook 12 times: one first call and 11 exact repeats. Values below are warm-call medians; initialization is measured separately in JSON. These are local observations, not a population p95 claim.

| Synthetic case | Previous p50 ms | Cache off p50 ms | Cache on p50 ms | Candidate chars |
| --- | --- | --- | --- | --- |
| greeting | 1.380 | 1.203 | 0.175 | 137 |
| testing | 1.942 | 1.633 | 1.175 | 416 |
| database | 1.562 | 1.414 | 1.120 | 300 |
| topic-switch | 1.910 | 1.541 | 1.053 | 294 |
| no-match | 1.436 | 1.297 | 0.169 | 137 |
| short-followup | 0.347 | 0.333 | 0.159 | 137 |
| operational-capture | 2.066 | 1.625 | 1.017 | 416 |
| canonical-profile | 1.608 | 1.354 | 0.160 | 262 |
| large-context | 5.017 | 2.818 | 0.938 | 2965 |

The enabled cache recorded 99 hits / 108 calls (9 misses) on deliberately repeated queries. Hermes queues the just-finished query, and a user's next query can differ: this measured repeat rate is not a real next-turn hit rate. Cache stays **off by default**. Default uncached latency is broadly similar to the baseline; the clear demonstrated gains are context quality and bounded size.

## Reproduce and inspect

Install the provider into an isolated test environment and apply the audited engine patches. Run from the repository root:

```bash
python bench/hermes_prefetch_eval.py --provider-ref 588e589d648e321fcb5b120b1e33d879413e41e3 --output .evals/hermes-prefetch/baseline.json
python bench/hermes_prefetch_eval.py --require-pass --output .evals/hermes-prefetch/candidate.json
python bench/hermes_prefetch_eval.py --cache --require-pass --output .evals/hermes-prefetch/candidate-cache.json
```

Each JSON report has a companion HTML report with the actual injected synthetic context, grading outcomes, source counters, lock wait/recall time, first-call latency and warm p50/p95. Results are ignored under `.evals/`; no live configuration or memory is read. Provider source hashes are included in the run metadata.

The corpus covers greetings, short follow-ups, direct fact queries, topic switches, no-match, raw operational captures, stale rows, foreign contacts, canonical profile ownership and oversized identity. Graders check required/allowed/forbidden labels, duplicate markers and size; oracle, empty, wrong, sensitive and oversized responses validate grader discrimination. Labels are content-based fixture oracles, not ranked engine-ID precision@5/MRR or semantic answer scoring. Character/4 token estimates are explicitly proxies.

Native correction/restart/dedup/episodic retirement, external SQLite writer invalidation and owner/session isolation are verified separately by lifecycle/prefetch regression tests. Pinned host extracts check checkpoint failure propagation and WAL backup/restore. Complete live gateway/model turns, billed token accounting, embedding/paraphrase quality and production cache-hit rate remain unmeasured.
