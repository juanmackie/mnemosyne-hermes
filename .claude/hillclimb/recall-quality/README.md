# recall-quality eval

Does `mnemosyne-lite recall` answer a natural-language question about a stored
memory? 94 labelled cases, one fixed 177-memory corpus, programmatic grading, no
model judge and no network.

Built per the `build-eval` guide. Generate the ignored `report.html` with
`node build-report-lite.mjs .` before reviewing per-case traces; it is an output,
not a committed file.

**Status: research harness, not an accepted retrieval change.** Round 1's
content-token OR improves recall but misses the negative-specificity hold
(0.600 vs. the 1.000 target) and fails the null probe. Its implementation and
scores are kept under `v1/` as evidence; shipped retrieval behavior remains the
baseline until a candidate clears both recall and specificity checks.

## Headline

| group | n | hit@5 | hit@1 | MRR | no-junk@5 |
|---|---|---|---|---|---|
| positive (held-out test) | 54 | **0.000** | 0.000 | 0.000 | 1.000 |
| positive (dev / train) | 27 | **0.000** | 0.000 | 0.000 | 1.000 |
| negative (held-out test) | 8 | **1.000** | 1.000 | 1.000 | 1.000 |

Read the two positive groups and the negative group as **different questions**:
"did it find the answer" and "did it stay quiet when there was nothing to say".
A single mean over both is meaningless — see `metrics.md`.

**hit@5 is 0.000 on all 81 positives, and that is not a broken grader.**
`diagnose_or_semantics.py` runs the identical cases, labels and `grade()`
function through an OR-joined MATCH and moves the held-out split to
**0.963 [0.875–0.990]** while the negatives collapse from 1.000 to 0.000. The
grader sees a win; the retriever is what returns nothing.

Cause: `storage._fts_match_expression` quotes *every* whitespace-separated token
and joins them with spaces, and space-joined FTS5 terms are an implicit **AND**.
A question like `what's my email address again?` requires the corpus to contain
`what`, `what's` and `again` as well as `email` — so it matches nothing. Only 4
of 6 sampled questions had every token present in the corpus.

`bench/measure.sh` cannot see this: its six shapes are single rare tokens, an
exact phrase, or a deliberately absent token — never a question with a function
word in it.

Both extremes are broken, and the eval brackets the fix: AND gives perfect
specificity and no recall; OR gives high recall and no specificity. Whatever
change lands (stopword handling, a minimum-match threshold, stemming, relaxation)
should be measured here, where both ends of the tradeoff are visible.

## Layout

| path | what it is |
|---|---|
| `report.html` | generated deliverable — per-case table; every row links to its trace (ignored) |
| `inputs.md` | every case in full, for review |
| `metrics.md` | rubric, polarity conventions, noise floor, provenance |
| `cases.jsonl` | 94 generated cases (32 train / 62 test: 27 positive + 5 negative in train) |
| `corpus.jsonl` | the 177 memories, with `age_days` fixture control |
| `inputs/` | the source sets, including the verified-absent negatives |
| `build_cases.py` | regenerates `cases.jsonl` and `inputs.md`; re-verifies every label |
| `runner.py` | the runner: real CLI, per-case grading, resume, error sidecar |
| `diagnose_or_semantics.py` | grader liveness check + the OR-semantics diagnostic |
| `baseline/` | committed `results.jsonl`; `errors.jsonl`, `traces/` and corpus DB are generated |
| `build-report-lite.mjs` | upstream lite report builder, vendored for self-containment |

## Running it

```bash
python3 build_cases.py --check --inputs-md   # inputs still match their source
python3 runner.py --probe                    # oracle + null, before trusting a number
python3 runner.py --variant baseline --reps 2 --k 5
node build-report-lite.mjs .                 # -> report.html + trajectory/scores.tsv
```

`--probe` pushes an oracle (a memory queried by its own content must come back)
and a null (negative cases must retrieve nothing) through the runner and the
grader. Run it before believing any number.

A new variant is `python3 runner.py --variant v1 --reps 2`, which creates
`v1/` beside `baseline/`. Add `v1/change.md` (first line becomes the report's
one-line description) and `v1/change.patch`, or the report's Harness Changes
panel is empty.

## Properties the runner keeps

- The measured path is the real CLI as a subprocess — argument parsing, FTS5,
  BM25 and JSON emission all included. Only fixture setup uses the storage API.
- Rows are written as each case completes; resume is idempotent at `(case, rep)`.
- A failed attempt goes to `errors.jsonl` with a failure class and **never**
  occupies the `(case, rep)` slot, so a re-run retries it.
- A hard per-case wall-clock ceiling (`--timeout-s`) fails the case as `timeout`
  rather than as a zero.
- Harness digest in `data/harness.sha` over the runner, the case builder, the
  cases and the corpus. Editing any of them makes the next run exit 2 until an
  operator re-approves with `--approve-harness` — a change detector, not a lock
  the runner can open for itself.
- No `usage` / `cost_usd` fields are written: this path makes no model calls, and
  a `$0.00` would read as a measurement rather than as the absence of one.

## Caveats

- `recall_ms` is process-inclusive (interpreter start plus SQLite open). It is a
  side-channel, not a latency verdict — `bench/measure.sh` is that instrument.
- The lite report renders the per-case metric table but not the perf columns;
  those live in `baseline/results.jsonl`.
- Single variant on disk, so the report is an eval viewer: no chart, no diff.
- Negatives pass trivially under AND semantics. They earn their keep as the
  guard that catches an over-broad relaxation, which is exactly what they did.
