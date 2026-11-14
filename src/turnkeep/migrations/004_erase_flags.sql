-- Migration 004: live-DB-only compensation flags for erasure (P0-R11-02).
-- A COMPLETE erase persists no needle hashes in its event (the hashes
-- would be the only enumerable material for the payload in the live DB,
-- since the per-scope HMAC key sits in the same DB's scope_secrets
-- table). But the repair path still needs to know which repair-reason
-- labels the erased payload blocked: a label that IS the payload (a
-- payload literally "repair", repaired with reason "repair") must be
-- rejected, and the repair path's receipt-derived needles are gone
-- after a COMPLETE erase. Those blocked labels are stored HERE, in
-- the live DB only — NEVER in the append-only event log, and NEVER
-- exported (export_scope copies events and records only): a blocked
-- label IS a fixed vocabulary word, and persisting it in an event
-- would write the payload itself into history when the payload is
-- literally that label (P0-R4-02 / the R5 repair-label test). The
-- table holds, per (scope, record), the blocked REPAIR_REASONS labels
-- as a JSON array of label strings, computed from the live needles at
-- erase time and unioned across the first erase and any repair. It
-- reveals at most whether the erased payload relates to the two fixed
-- repair labels (2 bits), and only when it does — it cannot enumerate
-- an arbitrary low-entropy payload (a PIN blocks no label, so its row
-- holds an empty array, and a row is only written when a label is
-- blocked or to record that none was). Pre-fix databases gain the
-- empty table on open; their erase events carry no flags, so repair
-- vetting for them falls back to the stored needle hashes as before.
CREATE TABLE erase_flags (
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  record_id TEXT NOT NULL,
  blocked_repair_reasons TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (scope_id, record_id)
);
