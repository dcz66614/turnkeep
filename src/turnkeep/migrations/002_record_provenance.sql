-- Migration 002: record-level import provenance (P1-c).
-- Maps a local record to the origin package it was imported from, so an
-- imported record can be traced back to its origin id and origin scope.
-- Applied by storage.migrate() after 001_initial.sql.
CREATE TABLE record_provenance (
  scope_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  origin_id TEXT NOT NULL,
  origin_scope TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY(scope_id, memory_id)
);
CREATE INDEX record_provenance_origin ON record_provenance(origin_scope, origin_id);
