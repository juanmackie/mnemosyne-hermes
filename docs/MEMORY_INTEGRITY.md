# Central memory integrity

All canonical memory insertion paths pass through `LibsqlStorage`'s integrity gate.
The gate stores a whitespace/case-normalized SHA-256 `content_hash`, suppresses
active exact duplicates, and enriches the oldest active parent for near matches.
Cosine similarity over non-degenerate embeddings uses a `> 0.92` threshold;
when vectors are absent or unusable, bounded token Jaccard similarity is used as
a deterministic conservative fallback. Enrichment merges metadata and records
an `update` audit event rather than creating a recall row.

Every inserted memory receives at least one bounded entity projection. Supplied
links are inserted idempotently in both directions. Pipe-delimited content
(`subject | predicate | object`) and explicit `StructuredFact` values use the
same confidence-first, recency-second conflict rule. A winning fact deactivates
the prior value and writes a `supersede` audit event; weaker or older values are
retained as inactive evidence.

Migration 027 creates the fact relation and rebuilds the legacy maintenance
CHECK constraint without dropping its rows or indexes. The initializer
adds/backfills the hash column idempotently, and serialized migration/recovery
logic preserves maintenance history across interrupted upgrades. `OrphanRepair` is bounded per projection,
never deletes canonical memory rows, and persists exact projection counts in its
maintenance report. The scheduler adapter runs it once per UTC day at 02:00;
the persisted date idempotency key makes retries safe.
