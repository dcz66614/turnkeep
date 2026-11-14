-- Migration 006: live-DB-only maintenance-pending markers (P0-R13-01).
-- A terminal COMPLETE erase deletes the record's verification key and
-- then purges the live file with PRAGMA wal_checkpoint(TRUNCATE). The
-- checkpoint can return busy != 0 (a concurrent read transaction holds
-- a snapshot) without raising: the WAL is not reset and the deleted
-- key's bytes stay recoverable from the live DB file / WAL, while the
-- erase event already recorded COMPLETE and residual_report() called
-- the tables clean (false-clean). The fix makes the purge outcome
-- persistent: when the checkpoint is busy at a terminal COMPLETE erase,
-- the key is RESTORED (so the historical hashes stay verifiable and the
-- repair / residual paths keep working) and a row is written HERE.
-- The row names the record and the checkpoint failure only — never
-- payload material — and is live-DB-only: NEVER in the append-only
-- event log, NEVER exported (export_scope copies events + records
-- only). A later repair re-run retries the checkpoint; when it
-- succeeds the key is deleted and the row is cleared. residual_report()
-- consults this table: a pending row makes live_tables_clean false
-- (see erasure.residual_report), so the report distinguishes
-- logical-table-clean from file-cleanup-incomplete. Pre-fix databases
-- gain the empty table on open; a record erased COMPLETE by the old
-- code under a busy checkpoint has no key and no row — its purge
-- status is unknown to this table, and a repair re-run is the remedy
-- (it re-runs the checkpoint and, on success, completes the purge).
CREATE TABLE maintenance_pending (
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  record_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (scope_id, record_id)
);
