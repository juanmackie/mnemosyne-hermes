# Audited engine fixes

These diffs patch `mnemosyne-memory` 3.15.1 itself. The engine's standalone
`mnemosyne mcp` process does not use the Hermes provider's P20 fallback.
`install.sh` applies the diffs after installing the engine and before changing
plugin discovery or Hermes configuration. The engine dependency range remains
`mnemosyne-memory[embeddings]>=3.15.1,<3.16`.

| Patch | Behavior |
| --- | --- |
| `core-beam.py.patch` | Update, get, invalidate, and forget use the existing scope helpers with one runtime snapshot and matching binds. Global working rows are editable across sessions. Successful updates clear recall caches; FTS/vector refresh stays intact. Remember dedup remains session-local, with a comment explaining that boundary. Entity recall extracts capped candidate phrases instead of fuzzy-matching the raw query string, then unions per-candidate matches (raw query kept as the fallback when extraction yields nothing). |
| `core-entities.py.patch` | Entity fuzzy-match keeps its exact match set with a content-aware pre-filter and a threshold-bounded banded Levenshtein: substring containment selects the loose prefix bound, other pairs are capped at length-ratio (disjoint alphabets score 0), and the surviving matrix aborts outside a Ukkonen band of ±max_dist. Long queries no longer fan out to hundreds of full Python matrices per recall. |
| `core-memory.py.patch` | The wrapper reports BEAM update success. BEAM authorizes the mutation; an existing legacy mirror is updated through the same connection and deferred transaction. Missing legacy rows do not turn a successful BEAM edit into `not_found`; denied IDs and rolled-back edits emit no wrapper update event. |
| `mcp_tools.py.patch` | An update with no fields returns a validation error before constructing a memory instance. |
| `core-llm-conflict-detector.py.patch` | Optional consolidation lock coordination yields during conflict-model calls and retains protection for cost logging. |

The BEAM diff also adds backward-compatible `sleep(..., db_lock=None)` for P22.
The provider passes its foreground RLock: sleep holds it over SQLite operations
and releases it only around model and embedding computations. Tier degradation
computes summaries/vectors before opening its savepoint, then conditionally
updates the unchanged row and preserves atomic content/vector rollback.
Consolidation checks its source contents and claim markers after slow work;
concurrently edited sources are requeued instead of producing stale summaries.
Conflict validation rechecks both source contents before invalidation, and
worker proposal writes reject stale vectors and enrichment after an intervening
edit or deletion during embedding.
Existing callers that omit `db_lock` continue to work. See
[`tests/test_provider_consolidation.py`](../../tests/test_provider_consolidation.py).

The runtime cross-session toggle now applies consistently to these ID tools
and provider update/validation. With it disabled, another session's private
rows remain inaccessible. With it enabled, private rows across sessions become
readable and mutable. Bank/database isolation remains intact. Cross-session
recall is therefore also an explicit authorization choice for ID operations.

## Apply and verify

Use the Python executable from the engine's venv, from the repository root:

```bash
python scripts/apply_engine_patches.py --dry-run
python scripts/apply_engine_patches.py
MNEMOSYNE_REQUIRE_ENGINE=1 python tests/test_provider_engine_contract.py
python tests/test_engine_patches.py
```

The applier reads distribution metadata without importing engine modules or
opening databases. `manifest.json` records original, patched, and patch SHA-256
digests. All targets and existing backups are checked before any source write.
Unknown engine revisions or local source edits are refused; a newer version
within the dependency range still needs its diffs and digests audited here.
Application is idempotent. Verified originals are retained beside each source
with the suffix `.mnemosyne-hermes-original`; source replacements are atomic,
and an ordinary replacement failure rolls back previously replaced files.

For a new patch revision, only explicitly recorded `previous_patched_sha256`
digests can upgrade in place, and only with the hash-verified original backup.
The updated full diff applies to that original; unknown local revisions remain
refused before any source write. Re-run the installer to upgrade an existing
audited installation, then restart the gateway and engine MCP processes.

To restore the audited originals:

```bash
python scripts/apply_engine_patches.py --restore
```

Restart the gateway and any engine MCP processes after applying or restoring.
Already-imported modules retain their previous code until process restart.
An engine reinstall can replace the patched files; rerun the applier or installer
afterwards. Uninstalling only the provider retains these engine fixes; restore
explicitly if the retained engine should return to upstream behavior.

The strict CI lane applies the patches and exercises real BEAM, MCP, and provider
handlers against temporary stores. The weekly newest-engine lane intentionally
tests upstream behavior without these patches so it can detect whether upstream
has incorporated the fixes. No live remote gateway verification is implied.

## Upstream and retirement

Source: `mnemosyne-memory` 3.15.1, MIT, Abdias J / AxDSan. The reviewable unified
diffs are suitable for applying to that upstream source revision; submission
and upstream merge are separate from applying them locally.

Remove a diff once an audited upstream release supplies its behavior and passes
the regression lane. P20 still supplies episodic editing and no-field validation
through the provider, so a working-row visibility fix alone does not retire all
of P20. P21 remains responsible for provider validation visibility. Do not widen
the engine pin without repeating `../hermes-provider/CONTRACT_AUDIT.md`.
