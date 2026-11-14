-- Migration 007: live-DB-only purge-pending markers (P0-R16-03).
-- A failed historical cleanup (purge_residue) left no persistent
-- trace: purge_residue() returned cleaned=False, but the failure
-- wrote no row anywhere, so a later residual_report() still called
-- the tables clean (live_tables_clean=true, file_purge_pending=[],
-- top level COMPLETE_WITH_RESIDUALS) while the deleted key's bytes
-- were still recoverable from the live DB file (P0-R16-03). The fix
-- makes a failed cleanup persistent: when purge_residue cannot clean
-- the residue (busy checkpoint, or the probe still finds a
-- recoverable key), a row is written HERE, one per scope.
-- The row names the scope and the checkpoint failure only — never
-- payload material (the detail holds the checkpoint triple and the
-- boolean probe results, never a recovered PIN — P1-R16-02) — and
-- is live-DB-only: NEVER in the append-only event log, NEVER
-- exported (export_scope copies events + records only). A later
-- purge_residue re-run that succeeds (busy==0 checkpoint + VACUUM +
-- file check) clears the row; a repair re-run does NOT clear it
-- (repair cannot know whether the historical residue is gone).
-- residual_report() consults this table alongside
-- maintenance_pending: a pending row makes live_tables_clean false
-- (see erasure.residual_report), so the report distinguishes
-- logical-table-clean from historical-cleanup-incomplete. The row
-- is listed in file_purge_pending with record_id
-- "purge_residue:<scope_id>" so it is distinguishable from a
-- per-record erase marker. Pre-fix databases gain the empty table
-- on open; a cleanup that failed before this migration has no row
-- — its purge status is unknown to this table, and running
-- purge_residue again is the remedy (it re-probes the files and
-- writes the row on failure).
-- Crash-window disclosure (P0-R14-01 does not apply here): the
-- marker is written AFTER the failure is observed, so a crash
-- between observing the failure and writing the row leaves no
-- marker — that state is equivalent to "purge_residue never ran":
-- the residue is still in the file and the next purge_residue run
-- re-runs the full physical cleanup: residue within the probe's
-- 4-digit PIN range can be re-discovered by it, anything else is
-- cleared by VACUUM and a busy==0 checkpoint, which do not depend
-- on the probe. The report saying clean before
-- the marker is written is a KNOWN window, disclosed here and in
-- docs/KNOWN_LIMITATIONS.md §29 — never claimed otherwise. Unlike
-- P0-R14-01, nothing is irrecoverably lost in that window: there
-- the key was already deleted and the marker was the only
-- protection; here the residue stays detectable.
-- No REFERENCES clause on scope_id (unlike maintenance_pending):
-- purge_residue may be called with a --scope the DB never
-- registered, and a marker write must never fail on a foreign key.
CREATE TABLE purge_pending (
  scope_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (scope_id)
);
