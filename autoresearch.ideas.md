# Ideas backlog — memory search speed (p50)

Seeded from `.auto/ideas.md` (memory-search section) plus fresh reads of
`src/lib/storage.py`. **Session 2 resumed 2026-09-24: run 10 keep /
segment-2 best 0.0100ms** — open threads below, updated as the loop goes.

## Tried this session (see worklog)

- ~~Fresh copies via `r.copy()`~~ — **kept, run 6** (`3535fcc`).
- ~~Counter.update pending batching~~ — kept (run 5), then superseded by
  run 7's batch-list form.
- ~~id-tuple batch pending, expand-at-flush~~ — **kept, run 7**
  (`af2d969`): the structural win; `ACCESS_FLUSH_DISTINCT` now bounds
  batches, hits cap binds first.
- ~~Memo-hit micro-trim bundle~~ — **kept at run 10** (`b0f7c3a`, −7.4%
  vs segment-2 baseline). Proves run 8's discard was machine drift: probe
  the machine before burying a theoretically sound idea.

## Open — from .auto/ideas.md

- **Trigram posting-list index for substring search** — residual cost of
  a search is the full scan for rare/phrase/no-match queries. A
  `trigram -> rowid` table is sound for substring search: a row can only
  match if it contains every trigram of the query, so a no-match query
  answers from the index alone. Needs: populate on `remember`, backfill on
  open, queries shorter than 3 chars fall back to the scan, and a
  candidate-count cap that falls back to the scan for common terms.
  FTS5 was rejected for token semantics; this is the sound
  substring-preserving version. (Primarily a **miss-path/p99** lever now
  — p50 is memo-hit-bound.)
- **`SELECT` only needed columns** — `recall` uses `SELECT *`;
  an explicit column list removes the column-order coupling with
  `_row_to_dict` and shrinks per-row copy work on the miss path.
- **NULL-guard tradeoff** — only revisit if a mixed-version writer really
  matters; measured 1.17–1.25x slower on scan-bound shapes; the migration
  backfill covers the real case.

## Open — this session's analysis

- **PRAGMA data_version (~1.25µs/hit, largest single remaining item)** —
  cached-statement cursor reuse saves only ~0.1µs (already statement-
  cached); skipping invalidation would break the external-commit contract
  (provider/CLI concurrency is real). Only pursue if a cheaper
  external-commit signal appears (update-hook exposure in Python, etc.).
- **Trim `_search_candidates` snapshot work** — `fetchall()`s all
  `id, content_lower` on version change even when ≤128 candidates (or
  zero) will match; lazy cursor + early exit at the cap.
- **Candidate narrowing without `id IN (...)` round-trip** — VALUES temp
  table or Python-side filtering for ≤128 candidates; measure first,
  may be noise.
- **Ranks-check cheapening** (`{(row[3], row[7]) ...}` per narrowed call)
  — low priority, miss path only.
- **Extend the Python candidate cache to namespaced queries** — q0/q4
  skip it (`if not namespace`), so their misses pay the full instr scan;
  would need namespace/importance in the snapshot. p99/q-shape lever.
- **Miss-path SQL string rebuilds** — predicate/join/`params[:-1] +`
  rebuilt per narrowed call; cache per (candidates signature) on the
  miss path. p99 lever.
