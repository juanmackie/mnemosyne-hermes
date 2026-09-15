# M4 — Retrieval and Embedding Parity (Deliverable)

Status: INVENTORY PRESERVED / TUNING BLOCKED. Existing retrieval components referenced in `ARCHITECTURE.md`, `docs/HIERARCHICAL_MEMORY.md`, `docs/REASONING_MEMORY.md`, `docs/BOOTSTRAP.md`. Embedding identity (`bge-small-en-v1.5`) preserved in `.mnemosyne_notes` and adapter contracts. Migration `libsql/006_vector_search.sql` and `sqlite/006_vector_search.sql` document sqlite-vec integration.

Blocker: Upstream source MISSING; verified DB clone NOT FOUND; no live embedding comparison or evaluation dataset available. Any embedding model switch (per M4 instructions) must be measured on verified Z640 baseline first — impossible without upstream source and verified DB.

Completed: Migration inventory preserved; retrieval references preserved; vector index design preserved; profile/namespace isolation contracts preserved.
Not done: Frozen baseline query verification; ranking regression tests; isolation adversarial tests; embedding identity reconciliation with upstream.
