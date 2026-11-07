# State machines investigation

This report inventories the principal state machines and confirmed reachable
failures in the repository. The engine is external; its internal state machine
and every process-crash interleaving have not been audited.

## Lite store opening and migration

| State | Event | Next state / effect |
| --- | --- | --- |
| Unopened | Construct | Create parent directory, connect and classify |
| Classified foreign or unsupported | Classification | Refuse before persistent database writes |
| Classified older lite | Open | Back up, configure, migrate in a transaction |
| Classified current or empty | Open | Configure and initialize schema |
| Locked | Operational lock error | Retry initialization up to `OPEN_ATTEMPTS`, then fail |
| Open | Close | Flush this thread's accesses, stop drain, close this thread's connection |

Classification precedes persistent pragmas and DDL. Migration backup precedes
schema changes. SQLite serializes migration writes; classification and backup
occur before the write lock, so concurrent migration and retry behavior needs
further investigation. See `PythonMemoryStorage._init_schema`.

## Lite memory and access accounting

| State | Event | Next state / effect |
| --- | --- | --- |
| Absent | Valid remember | Insert and commit, return ID |
| Insert collision | Integrity error | Generate another ID and retry once |
| Present | Recall | Return matching rows and buffer access IDs per thread |
| Buffered | Flush succeeds | Commit counts, clear buffer and patch local cache |
| Buffered | Flush fails | Drop batch, roll back its partial updates, invalidate cache |

Each thread owns its connection, pending buffer and recall cache. Other
connections' commits invalidate cached search results through SQLite's
`data_version`; local changes use `total_changes`. Access counts are explicitly
best effort. A remember retry is not an idempotency mechanism: repeating a
successful call creates another memory.

## Lite sync turn

| State | Event | Next state / effect |
| --- | --- | --- |
| Received | Skipped execution context, non-user speaker, foreign policy owner, or blank text | Skipped, no write |
| Received | Otherwise | Capture user text as a memory |
| Captured | Same request repeated | Capture another memory; there is no deduplication key |

Input validation precedes dispatch. `session_id` is accepted but does not
provide deduplication. See `tools.call_tool`.

## Confirmed issue and disposition

**Access batch atomicity (fixed; limited impact: incorrect popularity counts).**
If the second UPDATE in a buffered batch fails, SQLite leaves the first UPDATE
in an open transaction. A later `remember` commits that partial batch, despite
the stated drop-on-error behavior; other connections can also encounter a
held write lock until that later commit. `tests/test_access_transaction.py`
reproduced the incorrect count (1 instead of 0) before the fix. The batch now
uses a savepoint and rolls back its partial work on error. The test exercises a
subsequent write; broader concurrent fault injection remains outstanding.

## Lite restore

| State | Event | Next state / effect |
| --- | --- | --- |
| Requested | Source missing or existing destination fails classification | Refused; destination unchanged |
| Preflight passed | Confirmation declined | Cancelled; destination unchanged |
| Confirmed | Destination exists | Take SQLite safety backup |
| Staging | Copy SQLite backup or replay gzip SQL, open and migrate staging store | Validated stage, or error and staging cleanup |
| Validated | Destination exists | Copy stage through SQLite backup API, coordinating with live connections |
| Validated | Destination absent | Rename stage to destination |
| Installed | Success | Return destination and safety backup path |

The destination must be classified before persistent writes; the source must be
validated before installation. The safety snapshot is taken before staging, so
writes committed between snapshot and installation can be overwritten by the
restore and are not in that snapshot. Restore is an explicit replacement, not a
merge. It has no cross-process application-level lock preventing two restores
or a destination path replacement during preflight; those interleavings are not
established safe by the regression test.

**Live restore loses restored state — high impact, fixed.** Before the
fix, `os.replace` swapped the main file under existing SQLite connections while
their WAL remained in use. With an open store containing `old`, restore of a
backup containing `restored` returned success; a write through the old
connection followed by a new connection yielded `old` and the new write, not
`restored`. `tests/test_restore_live.py` fails on the old implementation and
passes with SQLite backup into an existing destination. It also covers a 4096-byte
live database and a differently sized source: the staging copy is converted and
its rowid-dependent indexes rebuilt before backup. Writes concurrent with the
restore can be superseded by the replacement; the test does not establish a
point-in-time recovery guarantee for those writes.

## Lite MCP server

| State | Event | Next state / effect |
| --- | --- | --- |
| Starting | Database missing | Raise `StorageError`; create nothing |
| Starting | Existing store opens | Enter request loop with one storage object |
| Request line | Invalid JSON-RPC envelope or params | Return invalid-request / parse error when an ID can be read |
| Request line | Notification without ID | Ignore without response |
| Request line | `initialize`, `ping`, or `tools/list` | Return method result |
| Tool call | Tool succeeds | Return structured content and `isError: false` |
| Tool call | Tool raises | Return text error and `isError: true` |
| Request line | Unknown method | Return method-not-found |
| EOF or loop exit | Finally | Close storage and its connection |

The stdio channel carries only newline-delimited JSON; storage exceptions stay
separate from “no match,” and shutdown closes the thread-local storage
connection. See `src/mnemosyne_lite/mcp.py` and `tests/test_lite_mcp.py`.

## Provider lifecycle

Source: `MnemosyneMemoryProvider.initialize`, `_initialize_session`,
`shutdown`, `_start_consolidation` in the vendored provider.

| State | Event | Next state / effect |
| --- | --- | --- |
| New / inactive | Initialize primary | Clear prior state, resolve configuration, construct beam; active on success |
| New / inactive | Initialize skipped context | Skipped; no beam or active provider contribution |
| Initializing | Config failure | Inactive; error recorded and raised |
| Initializing | Beam failure | Unavailable; error recorded, provider not activated |
| Active | Reinitialize while consolidation running | Refused |
| Active / unavailable | Shutdown | Stop worker admission, wait bounded time, close foreground resources and deactivate |
| Stopping with worker | Worker exceeds timeout | Defer owned host-backend cleanup until worker finishes |

Active-provider accounting is per instance; reinitialization must not retain a
prior session's beam or audit writer. The consolidation lifecycle lock serializes
admission and reinitialization; the beam lock serializes foreground access and
worker database operations. The engine's own internal lifecycle is outside this
repository's implementation and has not been independently audited here.

## Provider consolidation

| State | Event | Next state / effect |
| --- | --- | --- |
| Idle / succeeded / failed | Eligible trigger and budget available | Running; isolated worker with separate connection |
| Running | Another trigger | Reuse worker; increment reuse count |
| Stopping, no beam, ineligible, or budget exhausted | Trigger | Skip and record count |
| Running | `TypeError` in sleep | Retry once with local-only backend |
| Running | Worker completes / raises | Succeeded / failed; close worker connection and publish status |
| Running | Shutdown timeout | Continue worker; cleanup deferred |

At most one worker is admitted per provider instance. A TypeError retry relies
on the engine skipping rows already consolidated; it is not a general-purpose
exactly-once guarantee. Tests in `tests/test_provider_consolidation.py` cover
lifecycle behavior and pass in the local unit suite. The real engine is not
installed here, so its integration lane remains unverified locally.

## Provider prefetch and exact-query cache

| State | Event | Next state / effect |
| --- | --- | --- |
| Call begins | No active beam or skipped context | Empty context |
| Cache disabled / key unavailable | Query | Assemble sources without cache |
| Cache enabled | Valid key, fresh entry and recall-stat bump succeeds | Return cached context |
| Cache enabled | Miss, stale entry, or bump failure | Remove invalid entry and assemble sources |
| Assembling | Source raises | Record source error and continue with other sources |
| Assembling | Database generation unchanged | Store bounded entry with query-specific recall IDs and counts |
| Assembling | Database generation changed | Return current result without caching |

Cache keys bind query, session, profile, database path and generation,
visibility, source revisions, render knobs, and owner. Entries are bounded to
eight and expire by TTL. Cache-hit recall accounting updates the IDs recorded
for that entry. Calls may run concurrently; per-call cache payload must not be
stored in shared last-result diagnostics.

**Prefetch cache associates recall IDs with the wrong query — medium impact, fixed.** Concurrent `prefetch` calls could overwrite shared
`_prefetch_last_recall_ids` after one call's recall but before its cache insert.
The resulting cached content belonged to query A while the recall-stat IDs
belonged to query B. P37 keeps recall IDs and bank counts per thread/call. The
controlled interleaving in `tests/test_provider_pending.py::test_prefetch_cache_payload_keeps_recall_ids_per_call`
asserts each query caches its own IDs.

## Provider turn capture and pending writes

| State | Event | Next state / effect |
| --- | --- | --- |
| Turn received | No beam or skipped context | No capture |
| Eligible turn | Acquire per-provider BEAM lock; connection is idle and supports deferred commits | Begin one transaction for selected roles and identity capture |
| Capture underway | All selected writes succeed | Commit the full turn; record completion |
| Capture underway | Any write or final commit fails | Roll back the whole turn; record failure |
| Capture underway | Concurrent turn arrives | Wait for the lock, then start its own transaction |
| Capture requested | Connection is missing, already in a transaction, or lacks deferred commits | Fail closed before writing |
| Pending approval file | Exclusive claim succeeds | Apply through beam; remove record and claim on success |
| Pending approval file | Another apply owns claim | Refuse concurrent replay |
| Claimed approval | Ambiguous error or process crash | Keep claim for manual recovery; do not auto-replay |

**Partial turn capture — medium impact, fixed.** Previously, `sync_turn`
committed each role through a separate `BeamMemory.remember()` call. An
assistant or tool write failure after a successful user write left an incomplete
turn, and retry behavior depended on per-row deduplication and enrichment. P38
uses the pinned engine's `_deferred_commits` context while holding the existing
per-provider BEAM lock. The transaction spans user, assistant, opt-in tool, and
derived identity writes; a write or final commit failure rolls back the whole
turn. The connection is checked for the engine's deferred-commit protocol and
an idle transaction before any role is written. Unit regressions inject a
second-role failure, assert the first role is absent, retry successfully, and
force two concurrent calls to serialize. Real-engine regressions in
`tests/test_provider_engine_contract.py` cover the pinned connection behavior;
the engine is unavailable in this environment, so those tests only run in the
strict engine lane. See `sync_turn` and `_atomic_sync_turn_writes`.

Pending application creates an exclusive `.applying` claim before the write;
concurrent requests fail closed. A process crash or ambiguous write failure
leaves the claim for manual recovery, avoiding an automatic duplicate at the
cost of requiring intervention. This does not make the engine write and claim
removal one atomic transaction. See `_handle_apply_pending`.

**Concurrent approval duplicates — high impact, fixed.**
`_handle_apply_pending` read the shared JSON and called `beam.remember` before
unlinking it. Two simultaneous apply calls could both read the record and write
the same approved memory. P36 creates an exclusive `.applying` claim first;
the losing caller returns a claimed error without writing. The regression blocks
the first fake BEAM write while invoking the second call and asserts exactly one
write. A crash or ambiguous error retains the claim for manual inspection,
which trades automatic retry for duplicate avoidance.

## Provider installer deployment

| State | Event | Next state / effect |
| --- | --- | --- |
| Preflight | Invalid or unverified existing plugin | Refuse before replacing it |
| Confirmation accepted | Acquire per-Hermes-home lock or recover it if the owner died | Serialize install/uninstall mutation |
| Dependencies installed | Prepare link or copy in a sibling staging directory | Existing deployment remains in place |
| Staged plugin valid | Move existing verified plugin aside, promote stage, retain backup | Verify dependencies, config, registration and loader selection |
| Activation checks pass | Commit deployment | Remove previous plugin backup |
| Activation check fails | Restore previous plugin and prior provider selection | Preserve recovery copy if plugin rollback fails |
| Uninstall preflight | Link or verified copy | Remove plugin; purge option separately removes engine/data |

Both installers serialize mutation for a Hermes home. The Bash installer
records a PID lock and reclaims dead-owner locks; PowerShell holds an exclusive
file handle, released by process termination. Copies are verified before
promotion, and the previous deployment remains available until final activation
checks pass. On failure, both installers restore the previous plugin and
attempt to restore the selected provider; the Bash regression injects a failed
provider verification after promotion. Hermes PM dependency changes and engine
patches are external side effects: PM selection is asked to restore, while
package installation or patch changes can remain after failure.

**Verified-copy replacement on copy failure — high impact, fixed.** Previously
`install.sh --copy` removed the verified installed directory before copying its
replacement. A failed `cp` therefore left Hermes without the provider. The
installer now stages the copy and provenance first, then promotes it while
preserving the old verified directory until promotion succeeds. The regression
`tests/test_install.py::test_failed_copy_preserves_existing_verified_provider`
injects a copy failure and verifies the old package and provenance remain byte
identical.

**Failed activation leaves the replacement selected — medium impact, fixed.**
The installer previously deleted its saved plugin immediately after promotion.
A later engine-patch, config, provider-registration or loader check could fail
while leaving the candidate plugin selected and the previous plugin gone. Both
installers now retain the previous deployment through verification and restore
it on failure, while attempting to restore the prior `memory.provider`
selection. If restoring a plugin fails, the installer preserves the recovery
copy and reports its path.
`tests/test_install.py::test_failed_post_promotion_verification_restores_previous_provider_and_config`
forces registration verification to fail after promotion, then asserts the old
copy and prior config are restored. PowerShell source checks pin the same
rollback ordering; its runtime is unavailable here.

## Update and maintenance

| Machine / state | Event | Next state / effect |
| --- | --- | --- |
| Update unlocked | Start | Acquire checkout lock; recover it if its recorded owner is dead |
| Update running | Another live owner | Refuse concurrent run |
| Update running | Process exits normally | Remove owned lock |
| Update checking | Fetch failure / divergence | Error |
| Update checking | Equal or local ahead | Up to date |
| Update checking | Remote ahead, check mode | Report and exit 10 |
| Update applying | Wrong branch or tracked changes | Refuse |
| Update applying | Fast-forward, install and doctor succeed | Updated |
| Update applying | Install or doctor fails | Reset checkout and attempt old installation; report failure |
| Backup | Database missing | Skip |
| Backup | Snapshot integrity passes | Publish snapshot then prune |
| Backup | Integrity fails | Remove partial and fail |
| Compact | Below threshold / insufficient space / busy | Skip |
| Compact | Verified checkpoint then VACUUM and integrity check | Completed, or error with checkpoint retained |

`update.sh` serializes its own invocations with a directory lock, but external
git operations are not covered by that lock. Maintenance snapshot names are reserved atomically, preventing backup and
compaction from selecting the same file. See `scripts/update.sh` and
`scripts/maintenance/mnemosyne-maintain.py`.

**Updater stranded by a killed process — medium impact, fixed.**
The updater created a lock directory and relied on an `EXIT` trap to remove it.
A kill or host crash skips that trap, making every later update refuse to run.
The lock now records its PID, refuses a live owner, and reclaims the directory
when that owner is gone; acquisition verifies ownership to handle concurrent
reclaimers. `tests/test_update.py::test_check_recovers_lock_left_by_dead_process`
leaves a dead-owner lock and asserts the check proceeds and removes it.

**Maintenance snapshot name collision — medium impact, fixed.**
`unique_path` previously checked `exists()` and returned an unclaimed filename.
Concurrent backup/compaction processes could both receive the same path, then
overwrite the same snapshot. It now reserves candidates atomically with
`O_CREAT | O_EXCL`; failed snapshot creation removes its reservation. A
12-thread regression test checks that callers receive distinct paths.

## Verification and scope

Focused pytest suites were run with pytest and its pure-Python dependencies
extracted under `/tmp`, and temporary test data under an executable workspace
path:

```text
PYTHONPATH=/tmp/mnemosyne-pytest-site:src python -m pytest \
  tests/test_vendored_provider.py tests/test_install.py \
  tests/test_install_powershell.py tests/test_update.py \
  tests/test_provider_pending.py tests/test_lite_cli.py \
  tests/test_recall_freshness.py tests/test_access_transaction.py \
  tests/test_restore_live.py tests/test_provider_prefetch.py \
  tests/test_provider_lifecycle.py tests/test_provider_engine_contract.py \
  tests/test_lite_mcp.py tests/test_python_hardening.py \
  tests/test_maintenance_script.py -q
142 passed, 10 skipped
```

The broader project suite also passed:

```text
PYTHONPATH=/tmp/mnemosyne-pytest-site:src \
PYTEST_ADDOPTS=--basetemp=/mnt/tank/t3code/.mnemosyne-pytest \
bash test-all.sh --skip-llm
341 passed, 14 skipped
```

`bash -n install.sh scripts/update.sh`, Python compilation, `git diff --check`,
the 20-way update-lock stress probe, 12-way atomic snapshot reservation test,
and 16-way cold-open test passed. The provider pending, partial-turn, and
prefetch-race regressions pass, including rollback and retry after a role failure
and concurrent turn serialization. A real-engine test for the same paths is included in
`tests/test_provider_engine_contract.py`, but skipped locally because the engine
is not installed. The pinned engine wheel source was inspected; live Hermes
checks were not run. The vendored-provider gate exposed
a stale metadata digest for the P25 directory-plugin `pyproject.toml`: its
contents already carried the declared Android dependency marker patch, but its
manifest still recorded the earlier 237-byte file. The metadata hash, byte
count, and line count now match the 676-byte, 13-line file. The same gate had an
outdated assertion that rejected P25's Android-specific dependency split; it
now verifies both platform branches against the manifest's engine version pin.
All four vendored-provider checks pass. The installer failure regression also
confirms that post-promotion verification restores the former copy and config.
The complete `bash scripts/checks.sh` registry also reports `ALL PASS` when
run with pytest's temporary directory placed under the workspace
(`PYTHONPATH=/tmp/mnemosyne-pytest-site:src` and
`PYTEST_ADDOPTS=--basetemp=/mnt/tank/t3code/.mnemosyne-pytest`). Engine-backed
checks that require an installed engine were skipped, as expected in this
environment.

The remaining limits are engine runtime behavior, live Hermes, crash recovery
of a pending `.applying` record, package and patch side effects if activation
fails, Windows runtime behavior, and other untested installer interleavings.
The report does not claim those unverified paths safe.
