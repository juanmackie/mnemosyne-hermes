# Workflow: make the graph lane real + prove it (supermemory borrow: typed edges)

Status: DONE-ish — P2 shipped (typed `extends` on fact reaffirmation);
read-lane is_latest supersession demoted to P5 (not scoreboard-movable, see
P3 below).
Owner: mnemosyne dev agent
Borrowed from: supermemory graph memory — every new fact enters via
`updates` (replaces), `extends` (enriches), `derives` (infers across
memories), with `isLatest` keeping retrieval on current truth.

## Verified current state (2026-09-11, HEAD e506814 + local)

- `LinkType` = 8 variants (extends/builds_upon/contradicts/implements/
  references/referenced_by/clarifies/supersedes) — src/types.rs:470.
  The old 5-width CHECK data-loss bug is ALREADY repaired on open
  (`widen_link_type_check`, src/storage/libsql.rs:1888, rebuilds the
  table preserving rows; covered by the libsql_link_type_tests).
- The deterministic distiller (session_extract) dedup-skips near-duplicate
  candidates (>= SKIP_THRESHOLD 0.92) and discards the relation: no turn-fact
  graph edge is ever written for a restated fact. That is the gap P2 closes.
- No `is_latest` column/index anywhere (rg confirms).
- Supersession in recall = flat `SUPERSEDED_PENALTY 0.35`
  (src/utils/retrieval.rs:206) applied when `superseded_by` is set; real
  ingestion writes supersession only via consolidation (content-derived ids)
  and structured facts — never via a `supersedes` edge.
- Membench `graph_linked` class is broken as a graph test: gold rows
  lexical-match their anchor via FTS5 stemming, so FTS alone scores it;
  edges are irrelevant (maintainer finding, .auto/ideas.md 2026-09-11).
  4 queries/split (needs >=10/class before ranking work).

## Phases / gates

- P1 Read-lane baseline: run the existing membench heldout harness (serial,
  bge-small) as-is on current HEAD, record overall + frozen guards. This is
  the BEFORE no-regression floor for P2. No harness surgery: the graph_linked
  gold fix is deferred (maintainer's own call — the class is stem-leaky
  (`keeps`~`kept` via FTS5 porter) and rewiring 8 golds risks the whole split).
- P2 Write-side typed edges (SHIPPED): when a later turn's candidate is
  dedup-skipped against an existing knowledge memory, record a bidirectional
  `extends` edge from the turn's source to that fact — the reaffirmation
  signal that used to be discarded.
  - `LibsqlStorage::add_typed_edge` (src/storage/libsql.rs): idempotent
    INSERT OR IGNORE, both directions, existence-guarded, no self-links,
    Ok(false) when a target vanished mid-race.
  - `MemoryManager` (ln_with_config_metadata): the dedup loop keeps the
    existing-memory hit; after the batch store, dedups targets and writes
    the edges.
  - Tests: tests/typed_edges_extract.rs — restating a fact creates the
    `extends` edge to the restating turn's source AND no duplicate row
    (fails on old code = deterministic BEFORE; the AFTER gate is passing it).
- P3 edge-aware `is_latest` in recall: DEMOTED to P5. Real ingestion never
  writes a `supersedes`/`updates` edge today (the deterministic distiller only
  dedups on >= SKIP_THRESHOLD similarity; it cannot detect contradiction),
  and membench `temporal_latest_wins` fixtures supersede via the
  `superseded_by` pointer, not edges — so an edge-aware `is_latest` would not
  move that score. It becomes meaningful only once a contradiction/derives
  detector writes real `supersedes` edges. Keeping pointer-supersession +
  0.35 penalty as the read-lane truth source for now.
- P4 Proof: (a) membench heldout BEFORE->AFTER as a no-regression guard
  (a write-lane change must not move seeded recall); (b) deterministic
  ingest->edge integration test (fails-before / passes-after);
  (c) parity + full lib suite, fmt clean.
- P5 (deferred, separate workflow): DERIVES inference + `supersedes`/`updates`
  writes from contradiction/versioning signals (task-6); then edge-aware
  `is_latest` replaces the 0.35 penalty; graph_linked gold rewrite + grow to
  >=10 queries when ranking work needs it.

## Guard rails

- Do not touch pinned fixture seeds; content-derived-id change off-limits.
- Serial membench eval only (workers>1 is non-reproducible).
- frozen guards 0.981481 / 0.962963 / 1.0 and lib 881 are the floor.
- membench DB regen needs the bge-small model (MNEMOSYNE_EMBEDDING_MODEL)
  or the recall-time encoder mismatches the baked embeddings (run 39 trap);
  regenerate only if read-lane scores look wrong.
- THE READ-LANE SCOREBOARD CANNOT MOVE from P2 by construction: P2 only adds
  an edge in the extraction write path; membench seeds the corpus through
  storage `store`, not `sync_and_learn`, so seeded recall is untouched. The
  honest P2 benchmark is the deterministic fails-before/passes-after test;
  the scoreboard is the no-regression guard.
