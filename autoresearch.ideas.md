# Ideas backlog — memory search speed (p50)

Seeded from `.auto/ideas.md` (memory-search section) plus fresh reads of
`src/lib/storage.py`. Prune aggressively: mark tried/failed ideas, delete
duplicates. When this file empties, the research is done.

## From .auto/ideas.md

- **Trigram posting-list index for substring search** — residual cost of a
  search is the full scan for rare/phrase/no-match queries. A
  `trigram -> rowid` table is sound for substring search: a row can only
  match if it contains every trigram of the query, so a no-match query
  answers from the index alone. Needs: populate on `remember`, backfill on
  open, queries shorter than 3 chars fall back to the scan, and a
  candidate-count cap that falls back to the scan for common terms.
  FTS5 was rejected for token semantics; this is the sound
  substring-preserving version.
- **`SELECT` only needed columns** — `recall` uses `SELECT *`;
  `_row_to_dict` reads positions/names 0-9 and `content_lower` was
  deliberately appended last so migrated and fresh DBs agree. An explicit
  column list removes that ordering constraint but must be kept in one
  place so it cannot drift from `_row_to_dict`. Fetching only needed
  columns may also shrink per-row copy cost.
- **NULL-guard tradeoff** — only revisit if a mixed-version writer really
  matters; the per-row fallback predicate was 1.17–1.25x slower on
  scan-bound shapes and the migration backfill covers the real case.

## Fresh ideas (this session's read of storage.py)

- **Trim `_search_candidates` snapshot work** — on version change it
  `fetchall()`s `id, content_lower` for all 3000 rows even when the query
  will match ≤128 (or zero) rows. Options: lazy cursor iteration with
  early exit at the 128 cap (already breaks, but the fetchall materializes
  everything first), or skip the snapshot entirely when the query cache
  already holds a usable entry for this version.
- **Candidate narrowing without `id IN (...)` round-trip** — the narrowed
  query re-sends up to 128 ids as bind params; comparing against a VALUES
  temp table or filtering+r sorting in Python (rows already in memory for
  ≤128 candidates… but rows must still come from SQL per the contract)
  might shave the bind/plan overhead. Measure first — may be noise.
- **Memoize `PRAGMA data_version` per (total_changes tick)** —
  `_search_version` runs on every recall (memo hit path too). data_version
  only changes on other-connection commits; could refresh it only when
  this connection's total_changes moved since last check… still must run
  on first call per unknown external state — likely small, measure.
- **Ranks check without tuple rebuild** — `ranks = {(row[3], row[7]) for
  row in rows}` builds a set every narrowed call to detect LIMIT-tie
  divergence; a cheaper equivalent (e.g. compare last-row order keys
  monotonicity + count) might help if p50 is memo-dominated… low priority.
- **Skip `_recall_cache` dict-copy on hit for internal callers** —
  `recall()` returns `[dict(r) for r in cached]` every call (fresh-copy
  contract for external callers). If internal benchmark shapes dominated
  by this copy, an opt-in "borrowed" mode could help — but changing the
  return contract is risky; only if profiling shows the copy dominates.
