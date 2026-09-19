# Plan: port the Dream-RSI recall memo onto the current `storage.py`

## Context

Dream-RSI cycle 3 (`mnemosyne-recall`) produced a candidate claiming **325× faster `recall()`**
(0.0014 ms vs a 0.45 ms "baseline"). That claim needs two corrections before anything is applied:

1. **The loop optimized a stale snapshot.** The task's seed workspace
   (`%TEMP%\dreamrsi-mnemo\seed`, frozen 2026-09-17) holds a **634-line** `src/lib/storage.py`.
   The repo's file is now **923 lines** — it has since gained fail-closed schema handling
   (`StorageError`/`StorageSchemaError`, `_classify_schema`, `_init_schema_resilient`), WAL
   contention retries (`_connect`/`_configure`), and **`_search_candidates`** (a normalized-content
   snapshot + per-query candidate cache). Commits `41c6d7b`, `e84bd3e`, `fc81e2c` landed after the
   seed was cut. So the candidate's 0.45 ms baseline is not the project's baseline — the project
   already measures **~0.051 ms**.
2. **The candidate's freshness model is a correctness regression.** It validates its memo with a
   rate-limited `os.stat` fingerprint (once per 256 calls). Measured against the repo's own
   contract tests, that model **fails 3 of 8** `test_recall_freshness.py` cases. The candidate's own
   proposal admits this.

The candidate's *mechanism*, however, is sound and the repo does not have it. This plan ports it
onto the current code with a freshness model the repo already trusts.

### Measured evidence (prototype in a scratch copy, repo harness `./.auto/measure.sh`)

| Variant | `search_p50_ms` (2 runs) | `assert_ok` | `test_recall_freshness.py` | full `pytest tests -m 'not integration'` |
|---|---|---|---|---|
| **Baseline** (repo `HEAD`) | 0.0515 / 0.0507 | 1 | 8/8 | 97 passed, 45 skipped |
| **Port: memo + `_search_version`** | **0.0058 / 0.0058** | 1 | **8/8** | 97 passed, 45 skipped |
| Port + per-set access aggregation | 0.0058 / 0.0053 | 1 | 8/8 | 97 passed, 45 skipped |
| Candidate's `os.stat` freshness (isolated) | — | — | **3 failed** | — |

**~8.8× p50 improvement on the current code, with zero test deltas.** Provider suite
(`unittest discover -s integrations/hermes-memory-provider/tests -t .`) is 21 OK on both.

## Approach

Add a bounded memo of finalized `recall()` results, validated by the freshness primitive the repo
already uses for its candidate cache — `_search_version(conn)` (`PRAGMA data_version` +
`conn.total_changes` minus access-flush changes). That single choice is what makes this correct where
the candidate was not: it catches another connection's commit, this connection's uncommitted write,
and this connection's own committed write.

Three properties keep it honest:

- **Validated, not timed.** `entry[0] == memo_version` — no rate-limited probe, no staleness window.
  This is what passes `test_second_instance_insert_update_delete` and
  `test_recall_cache_invalidates_on_writes_and_flushes`.
- **Flush-invalidated.** `_flush_accesses()` writes `access_count`/`last_accessed`, which the cached
  rows carry, so it clears the memo. (The repo deliberately *excludes* those writes from
  `_search_version`, so the memo cannot rely on the version for this.)
- **Copies out.** The memo stores pristine dicts and every return path hands back `dict(r)` copies,
  preserving the caller-mutation isolation `test_thread_writer_and_access_metadata` pins.

**Deliberately not ported** (each rejected on evidence, not taste):

- **`os.stat` rate-limited freshness** — fails 3/8 freshness tests.
- **Per-set access aggregation** (the candidate's headline addition) — 0.0053–0.0058 ms vs
  0.0053–0.0058 ms, i.e. no measurable gain, for ~60 lines of slot/close-out state machine. It also
  collides with `test_python_hardening.py:176` (`len(s2._pending())` must stay a `{id: hits}` dict).
- **Single-entry register** — marginal, and adds per-thread state that must be broken on every
  invalidation path.
- **The candidate's file wholesale** — it is written against the stale seed and would delete the
  schema/robustness work listed above.

## Files to modify

- `src/lib/storage.py` — the only file. ~44 added lines, no schema, dependency, or contract change.

## Reuse (do not reimplement)

| Existing | Path | Role |
|---|---|---|
| `_search_version(conn)` | `src/lib/storage.py:588` | memo validator (data_version + total_changes − ignored) |
| `_flush_accesses()` | `src/lib/storage.py:347` | gains the `_recall_cache.clear()` |
| `_pending()` / `pending_hits` | `src/lib/storage.py:340` | unchanged access accounting |
| `_row_to_dict()` | `src/lib/storage.py:508` | unchanged row conversion |
| `_search_candidates()` | `src/lib/storage.py:604` | unchanged; still runs on memo misses |

## Steps

- [x] Branch: `git checkout -b perf/recall-result-memo` (repo works on feature branches; never `main`).
- [x] In `__init__` (after `self._local = threading.local()`), add `self._recall_cache = {}` with the
      explanatory comment.
- [x] Add `RECALL_CACHE_MAX = 256` next to `ACCESS_FLUSH_DISTINCT` / `ACCESS_FLUSH_HITS`.
- [x] In `_flush_accesses()`, add `self._recall_cache.clear()` immediately before
      `self._maybe_checkpoint()` (after the try/except, so it also runs on failure).
- [x] In `recall()`, after `conn = self._conn()`, add the memo lookup:
      `memo_version = self._search_version(conn)`, key `(query, namespace, max_results, min_importance)`,
      serve on `entry[0] == memo_version`, applying the same buffered-access accounting, and return
      `[dict(r) for r in cached]`.
      **Use a distinct name (`memo_version`), not `version`** — `_search_candidates()` reassigns
      `version` further down, which would silently make the memo store the wrong token and always miss.
- [x] After `results = [self._row_to_dict(row) for row in rows]`, compute
      `ids = tuple(r["id"] for r in results)`, FIFO-evict when `len(cache) >= RECALL_CACHE_MAX`, and
      store `(memo_version, results, ids)`. Store **before** the access-accounting block, because that
      block can call `_flush_accesses()` which clears the memo.
- [x] Change the tail `return results` to `return [dict(r) for r in results]`.

Exact patch for the two behavioural hunks:

```python
# --- in recall(), after conn = self._conn() ---
memo_version = self._search_version(conn)
key = (query, namespace, max_results, min_importance)
entry = self._recall_cache.get(key)
if entry is not None and entry[0] == memo_version:
    cached, ids = entry[1], entry[2]
    if cached:
        pending = self._pending()
        for r in cached:
            pending[r["id"]] = pending.get(r["id"], 0) + 1
        hits = getattr(self._local, "pending_hits", 0) + len(ids)
        self._local.pending_hits = hits
        if len(pending) >= self.ACCESS_FLUSH_DISTINCT or hits >= self.ACCESS_FLUSH_HITS:
            self._flush_accesses()
    return [dict(r) for r in cached]

# --- after results = [self._row_to_dict(row) for row in rows] ---
ids = tuple(r["id"] for r in results)
cache = self._recall_cache
if len(cache) >= self.RECALL_CACHE_MAX:
    try:
        cache.pop(next(iter(cache)))
    except (StopIteration, KeyError):
        pass
cache[key] = (memo_version, results, ids)
```

## Verification

Run from the repo root; record actual output, do not assume.

```bash
# 1. The two files that pin recall semantics + access-count internals
python -m pytest tests/test_recall_freshness.py tests/test_python_hardening.py -q
#    expect: 26 passed

# 2. Full non-integration suite — compare against the baseline run
python -m pytest tests -m 'not integration' -q
#    expect: 97 passed, 45 skipped  (identical to baseline)

# 3. Provider suite (the other half of .auto/checks.sh)
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . 
#    expect: Ran 21 tests — OK

# 4. The benchmark itself, twice, to confirm reproducibility
bash .auto/measure.sh | grep -E 'assert_ok|search_p50_ms|search_p99_ms'
#    expect: assert_ok=1, search_p50_ms ~= 0.005–0.006  (baseline was ~0.051)
```

If the repo's clean-user lane is available, `bash scripts/smoke-hermes-onboarding.sh` is the
end-to-end acceptance for the provider surface (per `AGENTS.md`); it exercises `recall()` through the
real provider, so it is the strongest confirmation that the memo is invisible above the store.

Known, expected test skip: `tests/test_python_hardening.py::test_refusal_corpus_if_available` skips
unless `MNEMOSYNE_BANK_CORPUS` (or `~/.mnemosyne`) exists — unchanged by this work.

## Risks and follow-ups

- **Cross-thread hit rate.** `_search_version` includes the per-connection `total_changes`, so a memo
  entry written by one thread may not validate on another thread and will be re-fetched. Correctness
  is unaffected (it fails closed to SQL); the effect is a lower hit rate under a thread pool. The
  benchmark is single-threaded, so this is not covered by the numbers above — measure under
  concurrent load before claiming a production speedup there.
- **Bounded staleness is now zero** for own writes and other connections' commits, so the memo does
  not reproduce the candidate's 256-call staleness window.
- **Memory:** ≤256 entries per instance; each entry holds a result list (≤100 dicts). Bounded like the
  existing query cache.
- **Dream-RSI housekeeping (separate, recommended).** The seed in `.dream-rsi` is stale relative to
  `src/lib/storage.py`, so every future cycle will keep measuring the wrong baseline and re-deriving
  already-shipped optimizations. Refresh the seed workspace to the current code (and re-measure its
  `history/seed/score.json`) before running another cycle; label the recorded cycle-3 artifacts
  (`.dream-rsi/trace_pool/iter0003`, `.dream-rsi/work/r0003`, the 0.0014 ms score) as measured against
  the stale seed so they are not compared against the current code.
