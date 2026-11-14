# Backup, restore and crash recovery (T039)

## Backup

```sh
# from Python (the CLI has no backup command yet — see below)
from turnkeep.backup import create_backup, restore_backup, diagnose
meta = create_backup(db, "backup.sqlite3")   # live, consistent snapshot
```

`create_backup` uses SQLite's online backup API: a consistent snapshot of
the live DB (WAL included), written to a temp file and atomically renamed.
A sidecar `backup.sqlite3.meta.json` records sha256, schema version, scope
list and record count. The backup is verified (`integrity_check` + counts)
before it is reported.

## Restore

`restore_backup(backup, dest, force=False)` verifies the backup first
(header, `integrity_check`, schema version, expected tables), refuses to
overwrite an existing DB without `force=True`, copies atomically and
verifies the restored DB before returning.

## Crash diagnosis

`diagnose(path)` is read-only and never repairs or deletes anything. It
reports `OK` / `DEGRADED` (e.g. a WAL file with uncheckpointed frames) /
`CORRUPT` (missing file, bad header, failed `integrity_check`) with a
recommended action and per-scope epoch / last-event findings.

## Restart handoff

`python scripts/restore_check.py --fixture restart-handoff` seeds a scope,
begins and finishes a run, backs up live, restores, reopens and resumes
the run — then verifies a truncated backup and an unforced overwrite are
both refused.

## Erasure interaction

A backup is a point-in-time copy. Restoring a backup taken **before** an
erasure resurrects the erased payload — that is expected behaviour, and
exactly why backups are listed as uncontrolled residuals in `docs/retention.md`.
A backup taken **after** an erasure stays clean. Never restore an old
backup to "undo" an erasure without re-erasing in the restored DB.
