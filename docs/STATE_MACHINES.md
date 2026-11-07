# State machines investigation

This is a partial inventory, not a completed audit of the provider, installers,
maintenance jobs, restore, or all concurrent interleavings. The engine is an
external dependency; its internal states have not been audited here.

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

Remaining work: audit all shipped state machines and retries, reproduce and
prioritize any additional reachable invalid states, and add concurrency tests.

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

**Issue 1 — live restore loses restored state (high impact, fixed).** Before the
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
lifecycle behavior; this environment lacks pytest and the engine, so they were
not rerun in this investigation.

## Provider turn capture and pending writes

| State | Event | Next state / effect |
| --- | --- | --- |
| Turn received | No beam or skipped context | No capture |
| Eligible turn | Enabled role, length and filter pass | Write user, assistant and opt-in tool messages in sequence |
| Capture underway | A write fails | Failure telemetry; later writes in that turn are skipped |
| Pending approval file | Valid apply | Write through beam, then remove pending file |
| Pending approval file | Invalid / missing / write failure | Report failure; behavior depends on validation failure |

Turn writes are sequential, not a single transaction in this wrapper; a failure
after an earlier successful write leaves a partial turn. Replaying a turn can
repeat earlier writes, depending on engine behavior. Pending application also
has a write-then-unlink window: a crash between those operations can replay the
write. These are code-path observations, not independently reproduced engine
failures; exactly-once behavior cannot be claimed from this wrapper. See
`sync_turn` and `_handle_apply_pending`.

## Update and maintenance

| Machine / state | Event | Next state / effect |
| --- | --- | --- |
| Update unlocked | Start | Acquire checkout lock or refuse concurrent run |
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
git operations are not covered by that lock. Maintenance snapshot naming checks
availability before creation; two processes can choose the same name, so the
name check alone does not establish concurrent uniqueness. These are audit
limits, not reproduced failures in this report. See `scripts/update.sh` and
`scripts/maintenance/mnemosyne-maintain.py`.

**Issue 2 — maintenance snapshot name collision (medium impact, fixed).**
`unique_path` previously checked `exists()` and returned an unclaimed filename.
Concurrent backup/compaction processes could both receive the same path, then
overwrite the same snapshot. It now reserves candidates atomically with
`O_CREAT | O_EXCL`; failed snapshot creation removes its reservation. A
12-thread regression test checks that callers receive distinct paths.

## Verification and scope

The access-batch and live-restore unittest regressions and `test_recall_freshness.py`
pass. Python compilation passes, and a 10-thread direct reservation probe
produced 10 distinct files. The repository gate was attempted: pytest is
unavailable in this environment and vendored provider `pyproject.toml` fails its
manifest hash check; no vendored files were modified. Engine-backed and live
Hermes checks were not run. This report covers the main lifecycle and
persistence machines, but does not prove all state combinations, external engine
behavior, process crashes, or every installer interleaving safe.
