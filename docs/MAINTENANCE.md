# Memory maintenance

A memory system that is never maintained does not fail loudly. It gets slower,
its file grows a freelist it never reuses, its consolidation tiers stop moving,
and one day an interrupted write leaves a file that no longer opens. This page
is the scheduled work that prevents that, and the rules that make it safe to run
unattended.

Everything here is deterministic, local, and keyless. The jobs run with
`--no-agent`, so they cost no model tokens and cannot be talked into doing
something else.

## Install it

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
scripts/maintenance/install-crons.sh --dry-run   # print the plan, change nothing
scripts/maintenance/install-crons.sh             # register what is missing
scripts/maintenance/install-crons.sh --list      # present / MISSING per job
```

The installer is idempotent: it looks each job up by name before creating it, so
re-running after an update repairs missing jobs and never duplicates the ones
already registered. It also rewrites the wrapper scripts under
`$HERMES_HOME/scripts/`, which is how a moved checkout or a changed interpreter
gets fixed. To back it out:

```bash
scripts/maintenance/install-crons.sh --uninstall
```

Nothing is deleted except the cron entries and their wrappers; your memory
database and its backups are untouched.

The plan prints the **Database** the jobs will maintain. Check that line before
you leave it unattended — a gateway that does not use `~/.hermes` (Docker images
and multi-profile hosts often do not) needs `HERMES_HOME` exported first,
because the resolved home is baked into every wrapper:

```bash
HERMES_HOME=/path/to/hermes-home scripts/maintenance/install-crons.sh
```

## What runs

| Job | Schedule | Command | What it does |
| --- | --- | --- | --- |
| `mnemosyne-backup` | daily 02:30 | `backup --keep 14` | Consistent snapshot, full integrity check, prunes to 14 |
| `mnemosyne-consolidate` | daily 21:20 | `consolidate` | Engine consolidation: working → episodic → BEAM tiers |
| `mnemosyne-verify` | Sunday 03:10 | `verify --full` | Full `integrity_check` + WAL/freelist thresholds |
| `mnemosyne-hygiene` | Sunday 03:40 | `hygiene` | Noise audit — reports candidates, deletes nothing |
| `mnemosyne-compact` | 1st of month 03:30 | `compact` | `VACUUM`, only above a 20% freelist, after a verified checkpoint |

The schedules are deliberately spread out: nothing here runs at the same minute
as anything else, and compaction is monthly because it is the only command that
rewrites the file.

### Output policy

Cron with `--no-agent` delivers stdout verbatim, and **empty stdout is silent**.
That is the whole contract:

- a clean run prints nothing;
- a threshold breach, a compaction, or a skipped engine call prints one line;
- a real failure writes to stderr and exits non-zero, which Hermes surfaces as a
  failed job.

If you never hear from these jobs, that is the intended state. `hermes cron runs
<name>` shows history.

## The commands, by hand

The installer only schedules `scripts/maintenance/mnemosyne-maintain.py`. You can
run the same commands yourself at any time.

```bash
S=scripts/maintenance/mnemosyne-maintain.py

python3 $S report                      # one line: size, freelist, WAL, integrity, row counts
python3 $S verify                      # quick_check; -v for detail; --json for machines
python3 $S backup -v                   # snapshot + integrity check + retention
python3 $S consolidate -v              # tier consolidation, database-wide
python3 $S hygiene -v                  # noise audit, report only
python3 $S compact -v                  # VACUUM if freelist > 20%; --force to test
```

The database is resolved exactly as the provider resolves it: `--db-path`, then
`memory.mnemosyne.db_path` in the Hermes config, then `MNEMOSYNE_DB_PATH`, then
the engine default under `$HERMES_HOME`. The config is read by a small
indentation-aware parser rather than a YAML library, because a cron interpreter
without PyYAML would otherwise fall through to the engine default and maintain a
different database; when `--hermes-home` is given explicitly and is not a
directory, the script refuses instead of guessing.

## Safety rules (and why each exists)

These are not style preferences. Each one is a failure that was observed before
it was encoded.

**Read-only commands never invent a database.** `verify`, `hygiene`, and
`compact` refuse a path that does not exist. A typo in `--db-path` must fail, not
create an empty database that looks healthy.

**The engine is never allowed to pick its own target.** The engine CLI resolves
its database from `MNEMOSYNE_DATA_DIR` as `<data_dir>/mnemosyne.db` and ignores
`MNEMOSYNE_DB_PATH`. Left alone it will happily create an empty database and
report a successful consolidation. The runner pins the data directory, refuses a
database whose filename is not `mnemosyne.db`, and refuses to run if the target
is missing. When it cannot target the resolved database it prints a `skipped`
line instead of pretending to work.

**Snapshots are self-contained.** SQLite's backup API writes a copy that inherits
WAL mode from page 1. The snapshot is flattened to `journal_mode=DELETE` before
it is renamed, because a `.db` moved away from its `-wal` is a silently truncated
backup. Every snapshot passes a full `integrity_check` before it is kept; a
snapshot created in the same second replaces its own partial file atomically.

**Pruning only touches our own files.** Retention matches the
`mnemosyne-*.db` pattern this tool writes and nothing else. Set `--keep 0` to
disable pruning, or `--no-prune` for a single run.

**Only compaction rewrites the file, and only with a way back.** `compact`:

1. skips unless `freelist_count / page_count > 0.20` (or `--force`);
2. checks there is at least 2× the database size free on disk;
3. writes a full, verified `*-precompact.db` checkpoint;
4. `VACUUM`s in autocommit mode;
5. re-runs `integrity_check` and reports the before/after size.

If a live writer holds the database, `VACUUM` fails as `locked`/`busy`; that is
treated as a skip, not an error, and the next scheduled run retries.

**Hygiene is report-only.** `hygiene` calls the engine's `audit`, never
`clean`. Deciding that a memory is noise is a judgement call, and automated
deletion of memories is not something a cron job should be allowed to do.

## Restore from a snapshot

Snapshots are plain SQLite files in `$HERMES_HOME/backups/mnemosyne/`
(override with `--backup-dir` or `MNEMOSYNE_BACKUP_DIR`).

1. Stop the Hermes gateway so nothing holds the database.
2. Move the current database aside — do not delete it.
3. Copy the snapshot to the resolved database path.
4. Remove the stale `-wal` and `-shm` files next to the old database.
5. Start the gateway and run `hermes mnemosyne doctor --no-fix`.

```bash
systemctl --user stop hermes          # or however your install runs the gateway
DB=~/.hermes/mnemosyne/data/mnemosyne.db
mv "$DB" "$DB.broken-$(date +%s)"
cp ~/.hermes/backups/mnemosyne/mnemosyne-<stamp>.db "$DB"
rm -f "$DB-wal" "$DB-shm"
```

The engine also has `mnemosyne backup` / `mnemosyne restore` for gzipped
archives; this suite keeps plain `.db` files because they are inspectable with
any SQLite client, which matters more during an incident than a compression win.

## Tuning

| Knob | Default | Where |
| --- | --- | --- |
| Snapshots retained | 14 | `--keep` / the installer's job definition |
| Compaction threshold | 0.20 freelist | `--threshold` |
| WAL warning | 512 MiB | `WAL_WARN_BYTES` in the script |
| Freelist warning | 35% | `FREELIST_WARN_THRESHOLD` |
| Delivery target | Hermes default | `MNEMOSYNE_CRON_DELIVER` |
| Interpreter | Hermes' own venv | `PYTHON_BIN` |

Hourly or half-hourly memory churn on a busy agent produces a WAL that looks
alarming but is normal; the warning is a checkpoint hint, not corruption.

## What this suite deliberately does not do

- **No LLM in the loop.** No summarisation, dedup, or "cleanup" by a model.
- **No automatic memory deletion.** Deduplication and decay belong to the engine
  or to an agent with the user in the loop.
- **No vector reindex.** `mnemosyne reindex` is expensive and model-dependent;
  run it deliberately when the embedding model changes.
- **No network calls.** Nothing is uploaded, and no key is required.

## Windows

The maintenance script is pure Python and runs on native Windows. The cron
installer is bash; on Windows, register the same five commands with Task
Scheduler, pointing at `python` and
`<repo>\scripts\maintenance\mnemosyne-maintain.py` with the arguments shown in
the table above.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `skipped — engine tooling only targets a database named 'mnemosyne.db'` | The provider is configured with a non-standard filename; move the database to `.../data/mnemosyne.db` or run consolidation yourself. |
| `hygiene: skipped — engine CLI (mnemosyne) not found` | The pinned engine is not reachable from the interpreter the job uses. Re-run `./install.sh`, or set `MNEMOSYNE_CLI` to its full path. |
| `compact: skipped, database busy` | A writer held the database for the whole 30 s window. Harmless; it retries next month. |
| `verify: freelist …% — run compact` | The file has grown but not shrunk. Next monthly run will compact it, or run `compact` now. |
| `consolidate FAILED` with engine output | Read the last lines; usually a schema migration the engine wants to run. Run `hermes mnemosyne doctor --no-fix`. |
