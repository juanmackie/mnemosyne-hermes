# Audited engine fixes

These diffs patch `mnemosyne-memory` 3.15.1 itself. The engine's standalone
`mnemosyne mcp` process does not use the Hermes provider's P20 fallback.
The installers apply the diffs to the selected Hermes runtime after its engine
dependencies are installed. The dependency range remains
`mnemosyne-memory[embeddings]>=3.15.1,<3.16`; the overlay accepts only the
source hashes declared in its manifest, so an unreviewed version in that range
is refused until its patches have been audited.

| Patch | Behavior |
| --- | --- |
| `core-beam.py.patch` | Update, get, invalidate, and forget use the existing scope helpers with one runtime snapshot and matching binds. Global working rows are editable across sessions. Successful updates clear recall caches; FTS/vector refresh stays intact. Remember dedup remains session-local, with a comment explaining that boundary. Entity recall extracts capped candidate phrases instead of fuzzy-matching the raw query string, then unions per-candidate matches (raw query kept as the fallback when extraction yields nothing). |
| `core-config.py.patch` | `config.yaml` is written and read as UTF-8 on every platform. The seed header contains an em dash; written through the locale encoding on Windows it landed as a cp1252 byte that the UTF-8 readers (this module and the Hermes provider) then refused to decode. A file an older release wrote in the locale encoding is still read once through that encoding, so an upgrade keeps its values. |
| `core-entities.py.patch` | Entity fuzzy-match keeps its exact match set with a content-aware pre-filter and a threshold-bounded banded Levenshtein: substring containment selects the loose prefix bound, other pairs are capped at length-ratio (disjoint alphabets score 0), and the surviving matrix aborts outside a Ukkonen band of ±max_dist. Long queries no longer fan out to hundreds of full Python matrices per recall. |
| `core-memory.py.patch` | The wrapper reports BEAM update success. BEAM authorizes the mutation; an existing legacy mirror is updated through the same connection and deferred transaction. Missing legacy rows do not turn a successful BEAM edit into `not_found`; denied IDs and rolled-back edits emit no wrapper update event. |
| `mcp_tools.py.patch` | An update with no fields returns a validation error before constructing a memory instance. |
| `core-llm-conflict-detector.py.patch` | Optional consolidation lock coordination yields during conflict-model calls and retains protection for cost logging. |
| `diagnose.py.patch` | Provider diagnostics accept an explicit store and profile log directory instead of deriving another database from the environment. |

P31 removes import-time default database initialization from `core.memory`.
Each real constructor (including the standalone default-memory helper) still
initializes its selected database. Both `Mnemosyne` and `BeamMemory` accept
`seed_config=False`, used by the Hermes provider and CLI so construction cannot
seed the process-global config. Standalone engine callers retain config seeding
by default. The provider opens its resolved bank directly through `BeamMemory`.
`doctor --no-fix` avoids constructors and diagnostic-log writes entirely.
Fresh-process tests in `tests/test_provider_profiles.py` exercise the real
constructors with two profiles, with and without a data-directory override,
and assert that no default DB or config is created.

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

P35 makes `config.yaml` UTF-8 at the write sites in `core/config.py`. The file
was always read as UTF-8 (`_warn_legacy_provider_defaults`, and the Hermes
provider's `configuration._mapping`), but the seed comment and `config set`
went through the locale encoding. On Windows that is cp1252, so the em dash in
the seed header became byte `0x97` and every UTF-8 reader failed with
`UnicodeDecodeError` -- the provider could not read its own profile config.
`_read_config_text` decodes UTF-8 first and falls back to the locale encoding,
so a store written by an older release still loads.

## Apply and verify

### Capture and abstention guards (all off by default)

Three opt-in behaviours ship in `core-beam.py.patch`. Each is gated by its own
environment variable, and each is **off unless set**, so the default engine
behaviour is unchanged. They exist because the scorecard's per-candidate score
dumps showed two failure modes with a common cause: a fact's *identity* and its
*validity window* were never modelled, so a query naming an entity the store had
never seen returned topical neighbours, and a fact stated "until 2025-06-01"
stayed current forever.

| Variable | Effect |
| --- | --- |
| (always on) | `BeamMemory.remember()` redacts recognized credential formats and contiguous 32+ character high-entropy token strings before sanitization, embedding, and persistence. Separators such as `_` and `-` split generic entropy candidates, so structured run and tenant identifiers remain readable. Labeled credentials and known provider token formats still match their dedicated rules. The write remains useful because surrounding prose is retained. This is a storage safety invariant, not a recall option. |
| `MNEMOSYNE_RECALL_ABSTAIN_IDENTIFIERS` | Return no rows when the query names a digit-bearing identifier or a lowercase identifier with a four-character hexadecimal suffix, and that token appears in no candidate. An unknown subject cannot be answered from the store; the nearest topical rows are off-subject by construction. Ordinary unknown words are ignored unless they have that identifier shape. Runs after every supplementary source, because the fact supplement re-emits the rows it derived from. **Measured on 12,559 held-out probes: 2,851/2,851 → 0/2,851 fabricated queries returning rows at zero answerable-recall cost.** |
| `MNEMOSYNE_CAPTURE_TEMPORAL` (supplement) | Also applies the currency check to the MEMORIA fact supplement and its re-emitted source rows, which were the one recall path with no `valid_until`/`superseded_by` filter. **Measured: 301/289 → 0 expired facts served as current** at zero answerable-recall cost. |
| `MNEMOSYNE_CAPTURE_TEMPORAL` | When an utterance states its own end date ("until 2025-06-01", "valid through …", "expires on …"), record it as `valid_until` so the row actually expires. Only explicitly written dates are used. |
| `MNEMOSYNE_RECALL_CORRECTION_PRIORITY` | Adds a bounded recall-ranking bonus to rows that begin with an explicit correction ("Correction: …", "Actually, …", "Update: …"). It does not retire or mark the earlier row superseded; both remain retrievable. Nothing is inferred from silence. |

These are measured by `bench/scorecard/` (see its README). The scorecard reports
their effect as paired differences with bootstrap CIs on a held-out split; a
flag that does not move its own metric is not kept.

### Sleep and degradation guards (on by default)

Three protection guards ship in `core-beam.py.patch` and are **on by default**
since 3.1.0. Each uses the engine's `_env_disabled` toggle: only an explicit
`0`/`false`/`no`/`off` (case-insensitive, whitespace-stripped) disables it;
unset, empty, or unrecognized values keep the guard on. The defaults changed
after the staged scorecard runs recorded in `bench/scorecard/README.md`: the
guarded arms matched no-sleep backward transfer, and demotion's only paired
changes were miss→hit. Measurement arms pin every flag explicitly, so the
scorecard baselines keep measuring the unguarded behavior.

### Consolidation coverage guard (on by default)

`MNEMOSYNE_SLEEP_VERIFY` checks a sleep summary against its sources before marking it verified. The initial check is deliberately conservative: after whitespace and case normalization, the text must reproduce the complete source texts in order, with the same punctuation. Structured names and aliases must be preserved, validity dates and visibility scopes must agree, and mixed-scope summaries stay session-scoped. This is a text and metadata check, not proof that a model's output is true. It can reject useful paraphrases.

On failure, sleep makes one bounded retry using at most two smaller groups when a model is available. If the retry still fails, sleep records the summary with `verified: false`; it does not feed that row into graph ingestion or model refresh. Recall filters incomplete summaries and the context path retains their source rows. Incomplete rows remain in the database and can be seen through direct inspection or export. Coverage failures are counted in the consolidation log without recording source content. No source demotion is enabled by this flag.

Set `MNEMOSYNE_SLEEP_VERIFY=0` to stop checking new summaries. The current patch continues to honor the metadata on summaries checked earlier. Restoring an older engine patch does not honor that marker and can expose an incomplete summary again; preserve a backup and assess those rows before any downgrade. The guard creates no new table, but it adds metadata to new episodic rows. After a patch upgrade, restart the gateway and MCP processes. Test on a scratch store before changing a live gateway. A summary marked verified only passed this conservative check; it has not received semantic or human review.

### Source demotion after verified consolidation (on by default)

`MNEMOSYNE_SLEEP_DEMOTE_SOURCES` lowers the recall weight of a working row whose content was consolidated into a summary that passed the `MNEMOSYNE_SLEEP_VERIFY` coverage check. The row's `importance` is capped at 0.25 (an idempotent one-way `MIN(importance, 0.25)`; rows already at or below the cap are untouched). Rows are never deleted and stay retrievable, and `mnemosyne_forget` remains the only deletion path. Groups whose summary failed the coverage check, and stores running with `MNEMOSYNE_SLEEP_VERIFY` explicitly disabled, are never demoted, so the flag alone cannot silently demote an unverified summary. Demotion counts appear in `consolidation_log.summary_preview` as `demoted=N` and in the `demoted_rows` field of the sleep report; no content is recorded. Reversing a demotion requires restoring `importance` by hand. Set `MNEMOSYNE_SLEEP_DEMOTE_SOURCES=0` to opt out of demotion; coverage verification and its metadata are unaffected.

### Episodic degradation preservation guard (on by default)

`MNEMOSYNE_DEGRADE_VERIFY` applies a key-token check to each tier-1→2 and tier-2→3 degradation step before the engine rewrites an episodic row in place. A key token is any run of letters and digits that contains a digit or a capital letter (identifiers, numbers, versions, names). After the tier's compression (LLM or keyless truncation), every key token of the stored content must still appear in the compressed text. A step that drops one is cancelled: it is counted in the `coverage_rejected` field of the degradation report (or `coverage_guard: not_evaluated` under `dry_run`), and the row stays at its current tier with its recall weight unchanged. Lowercase prose is not checked, so truncation that drops only prose is allowed. The check does not split groups, retry, or write new rows. It is a deterministic text check, not proof that compression preserved meaning, and it cannot recover content already lost by earlier transitions. Set `MNEMOSYNE_DEGRADE_VERIFY=0` to restore the unguarded in-place rewrite behavior.

`MNEMOSYNE_DEGRADE_ADDITIVE=1` (option A, opt-in) changes what a degradation step writes. The original row keeps its content, tier and recall weight. A step with changed content inserts one copy at the next tier. The copy has the compressed text, the original's `created_at`, source, scope and other columns, and `metadata_json` with `degraded_from` (the original's id) and `degradation_tier`. The original gets `degraded_at`, and the copy starts with `degraded_at` NULL, so the copy can degrade again. A step that changes nothing only marks the original. The guard above applies to these steps: a rejected step writes nothing and leaves the parent unmarked. This is a persisted-data change: it adds rows and does not rewrite or remove any. Storage grows by up to one row per tier per original. Recall collapses a copy into its parent when both are returned (`_collapse_degraded_lineage`), so each lineage contributes its topmost surviving row. The plan asked for the original's weight to be lowered. It is not, because an equal-weight copy would tie with it, so the original keeps full weight. Back up the store before enabling the flag. Rows that were degraded in place earlier are not affected.

Unset the flag to keep the existing in-place rewrite behavior. The flag is independent of `MNEMOSYNE_SLEEP_VERIFY`. Restoring an older engine patch silently re-enables unguarded rewrites; preserve a backup before any downgrade. Neither guard enables source demotion.

### Recall precision cutoff

`MNEMOSYNE_RECALL_SCORE_FLOOR` enables an optional relative score cutoff.
For example, `0.6` keeps candidates whose score is at least 60% of the highest
candidate score. The cutoff preserves the order of surviving candidates and
can retain several answers. It runs before the linear and polyphonic return
limits and recall counters, and also filters fact supplements and enhanced
recall output. Engine and provider cache keys include the setting.

Unset or `0` preserves the existing recall behavior. Values must be finite
numbers between `0` and `1`. Invalid values produce a warning and preserve the
existing behavior. A value of `1` retains tied highest scores. Scores are
hybrid ranking signals, not probabilities. The example `0.6` is not a
recommended production threshold.

After applying the patches, set the variable in the environment of the
`mnemosyne mcp` process or Hermes gateway and restart that process. The setting
does not apply to `mnemosyne-lite`. Test candidate thresholds against the full
PrecisionMemBench suite before selecting a deployment default. The replay
tool and measurement limits are documented in
[`bench/PRECISION_RECALL_RESULTS.md`](../../bench/PRECISION_RECALL_RESULTS.md).

The regression lanes use temporary stores and cover cross-topic lexical
neighbors, rambling queries, multiple required answers, working and episodic
tiers, session visibility, cache refresh and returned-row recall accounting.
Domain labels such as `domain:writing` are not the engine's `session`/`global`
scope. The cutoff does not enforce domain isolation. See the
[scope analysis](../../bench/PRECISION_SCOPE_ANALYSIS.md).

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

**Regenerating the diff must not drop a hunk.** The `core-beam.py` revision in
`114bd8f` was rebuilt from a regenerated diff and silently lost the
`_minimum_recall_relevance` hunk that `ec8e8da` had added, which re-broke the
multi-term recall regression and left `main` red for two weeks. The digest
checks cannot catch this on their own: a dropped hunk still produces a
self-consistent source/patch pair. `tests/test_engine_recall_scope.py` is the
behavioural gate that would have caught it; `./test-all.sh` runs it on the same
lane as the patch audit.

To restore the audited originals:

```bash
python scripts/apply_engine_patches.py --restore
```

Restart the gateway and any engine MCP processes after applying or restoring.
Already-imported modules retain their previous code until process restart.
An engine reinstall can replace the patched files; rerun the applier or installer
afterwards. Uninstalling only the provider retains these engine fixes; restore
explicitly if the retained engine should return to upstream behavior.

`hermes mnemosyne doctor` re-verifies the runtime side of this contract: it
prints the resolved engine file and hashes the engine sources this interpreter
imports against `manifest.json` (skipped, informationally, when no manifest sits
beside a copied provider). A `.rej` file beside an engine target means a manual
`patch(1)` attempt left rejects; the digest comparison in doctor — not the
debris' presence — is what gates the check. If doctor reports a mismatch, run
`hermes mnemosyne doctor` without `--no-fix`: when the engine still matches the
installed distribution, it re-applies the audited patches in place through
`scripts/apply_engine_patches.py` (keeping the `.mnemosyne-hermes-original`
backups), re-checks the files before exit, and prints the restart step. When
the resolved engine is not the installed distribution — a separate venv, a
shadowing copy — the repair refuses to write, and the fix is
`./install.sh --python <this interpreter>` instead. Restart the gateway after
either path.

The strict CI lane applies the patches and exercises real BEAM, MCP, and provider
handlers against temporary stores. The weekly newest-engine lane also applies
the audited patches before testing the provider contract. It reports setup,
patch-audit, and contract failures, including refusal of an unknown upstream
source revision. A new release needs a source audit to determine which fixes
it incorporates before updating or retiring the patches. No live remote gateway
verification is implied.

## Upstream and retirement

Source: `mnemosyne-memory` 3.15.1, MIT, Abdias J / AxDSan. The reviewable unified
diffs are suitable for applying to that upstream source revision; submission
and upstream merge are separate from applying them locally.

Remove a diff once an audited upstream release supplies its behavior and passes
the regression lane. P20 still supplies episodic editing and no-field validation
through the provider, so a working-row visibility fix alone does not retire all
of P20. P21 remains responsible for provider validation visibility. Do not widen
the engine pin without repeating `../hermes-provider/CONTRACT_AUDIT.md`.
