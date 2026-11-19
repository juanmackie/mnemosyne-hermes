# PrecisionMemBench replay (single-turn suite)

Replays the public [tenurehq/precisionMemBench](https://github.com/tenurehq/precisionMemBench)
single-turn suite against the real Mnemosyne provider, and reports the two
numbers the quality scorecard's fourth pass criterion names:

- **active passes** — cases that require query-dependent retrieval, out of 43;
- **missing expected beliefs** — required beliefs the context never contained.

## Fixtures

Pinned to upstream commit `61a712a1754e7d27b6dee42da32a2c214b003ece`. Both files
are MIT-licensed upstream and are stored byte-identical, so the recorded digests
verify on every run:

| File | SHA-256 (CRLF form, as published) |
| --- | --- |
| `retrieval.cases.json` | `6ab5a626356f4a6e947bee8e2e9a95af9ead7cfd58237e4aae6f02ae490db0ba` |
| `beliefs.seed.json` | `d6ae1c270faba0e8bf2bc0eb4527cd008d481c451a9cd557b5ab85f918fe33f3` |

`bench/PRECISION_RECALL_RESULTS.md` records an earlier digest for the cases file
that differs in one character (`…947be8e…`); the bytes published at the pinned
commit hash to `…947bee8e…`. `verify_fixtures()` trusts the bytes and fails
loudly on any drift, so the typo cannot hide a fixture change.

`.gitattributes` marks `bench/precisionmembench/*.json` as `-text` so a checkout
with `core.autocrlf=true` cannot silently alter the bytes.

## Why this is a re-implementation, not a wrapper

The upstream runner is TypeScript and drives an HTTP adapter (`/add`, `/search`,
`/reset`). The provider here is a Python object, and the interesting failure —
an engine recall that returns the wrong rows — only shows up when `/search` is
answered by the real engine. `run_eval.py` therefore transcribes the adapter's
reader interface (`baseAdapter.ts`) and calls the provider directly:

- `beliefToText` (`canonical_name_aliases`: canonical name, aliases, content,
  why-it-matters), `seedMetadata`, and the `pinned`/`superseded_by` filters;
- `buildContext`: pinned facts and open questions from the seed, query results
  with pinned ids excluded, relation expansion of returned relation beliefs, the
  `maxBeliefs` cap, and the scope filter;
- the assertion set from `retrieval.external.eval.test.ts`: `mustInclude`,
  `mustExclude`, `shouldInclude`, `shouldOnlyInclude`, `maxCount`/`minCount`,
  `orderedBefore`, pinned/open-question expectations and the persona prelude.

Three replay adaptations are recorded in every report rather than hidden:

- **Scope.** The upstream adapter forwards `scope` to `/search` and the provider
  is meant to filter. Mnemosyne's engine `scope` is session/global *visibility*,
  not a benchmark domain tag, so the filter is applied against each belief's own
  `scope` list — the same predicate the expectations are written against. An
  earlier revision of this harness omitted it and understated the provider
  (11 active passes instead of 9; the fix moved precision 0.52 → 0.60).
- **Multi-scope fan-out** issues one provider recall per active scope when the
  provider does not advertise native multi-scope support. The ranked results
  are interleaved and deduplicated (`multiScopeFanout: true`).
- **Structured labels.** The upstream seed path supplies `canonical_name` and
  `aliases` separately. This replay also stores them in engine metadata so P48
  can test exact label filtering. Reports set `structuredLabelMetadata: true`;
  results from runs without that field used different seed metadata.

## Run

```bash
# with the pinned patched engine importable and the provider on PYTHONPATH
python -m bench.precisionmembench.run_eval --output .scorecard/pmb.json

# gate a target
python -m bench.precisionmembench.run_eval \
  --require-active-passes 35 --require-missing-beliefs 8

# consolidation: one forced sleep after seeding, before recall
python -m bench.precisionmembench.run_eval --consolidate
python -m bench.precisionmembench.run_eval --consolidate --sleep-verify
python -m bench.precisionmembench.run_eval --consolidate --sleep-verify --sleep-demote
```

The default run never sleeps, so `--sleep-demote` cannot change its numbers.
With `--consolidate`, the report records the sleep status, the number of items
consolidated, and the demoted rows. Summary rows have no benchmark alias, so
the adapter drops them from context and only the original beliefs are scored.

Measured on 2026-11-19 with the vendored provider and the audited engine
(patch hash `273614c8…`): the default run gives 43/43 active passes, 0
over-return and 0 missing. `--consolidate` alone gives 22/43, with 42 missing,
because unverified episodic summaries displace the originals in recall.
`--sleep-verify` restores 43/43 and serves no summary rows. In this replay the
single 35-item group fails the exact-reproduction check, so
`--sleep-demote` demotes 0 rows and its 43/43 result is not evidence about
demotion. Demotion was measured in the staged scorecard instead.

## What it does not claim

The engine is seeded with one concatenated text per belief, unscoped recall is
used for candidate generation, and no LLM, embedding server or HTTP hop is
involved. Structural cases pass on synthesized data and are not evidence of
engine capability. P48 narrows only private engine results, before shared-surface
results are merged; it does not filter shared-surface rows. This is a replay of a
public suite against this provider, not the benchmark's published deployment
numbers.
