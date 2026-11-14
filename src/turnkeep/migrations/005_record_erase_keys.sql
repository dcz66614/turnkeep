-- Migration 005: per-record verification keys for erasure (P0-R12-01).
-- A PARTIAL erase persists needle hashes (HMACs) in its event so the
-- repair / residual / export paths can still judge and clean the live
-- copy. Those hashes were keyed by the per-scope secret in
-- scope_secrets, so once the record reached terminal COMPLETE
-- (repair-COMPLETE, no remaining copies) the hashes stayed enumerable
-- from the live DB: the scope key is shared, and it must stay while
-- any other record in the scope is still PARTIAL (even for a different
-- payload), so a scope-key rotation cannot retire them (R12
-- re-evaluation of the R11-rejected rotation direction).
-- Each record therefore gets its OWN 32-byte verification key, stored
-- HERE, in the live DB only — NEVER in the append-only event log, and
-- NEVER exported (export_scope copies events + records only). The
-- record's needle hashes are HMACs under this key (see
-- erasure._needle_hashes). When the record reaches terminal COMPLETE
-- (its erase event records erase_status COMPLETE, no remaining copies)
-- the key row is DELETED by the erase path, so the historical hashes
-- in the append-only events become unverifiable: there is no key left
-- in the DB that can recompute them, and enumerating 0000-9999 (or any
-- low-entropy space) finds nothing. A later repair of the same record
-- re-creates a fresh key lazily if it goes PARTIAL again; the old
-- hashes stay retired (a new random key cannot verify them).
-- Legacy: databases created by an older release carry scope-keyed
-- hashes in their erase events. Those still verify via the retained
-- scope key (erasure._record_verification_keys tries the per-record
-- key first, then the scope key), so repair / residual judgments on
-- pre-fix data keep working. The scope key is never deleted by this
-- release, and a legacy DB's scope-keyed hashes stay enumerable until
-- it is exported to a fresh DB (the documented remedy,
-- docs/KNOWN_LIMITATIONS.md §25). A fresh DB never creates a scope
-- key at all: _needle_hashes uses the per-record key, and the scope
-- key is only created on demand for legacy verification.
CREATE TABLE record_erase_keys (
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  record_id TEXT NOT NULL,
  secret_hex TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (scope_id, record_id)
);
