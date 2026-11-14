-- Migration 003: per-scope HMAC key for erasure needle hashes (P0-R4-04).
-- Needle hashes persisted in erase events must not be enumerable from an
-- export: raw SHA-256 of a low-entropy needle (e.g. a 4-digit PIN) is
-- brute-forceable from events.jsonl alone. The hashes are therefore
-- HMAC-SHA256 keyed by a per-scope secret stored here, in the live DB
-- only. This table is NEVER exported (export_scope copies events and
-- records only) and must never be added to any export. A scope's key is
-- created lazily on first erase (erasure._scope_hmac_key) inside the
-- erase transaction; pre-fix databases gain the empty table on open and
-- get a key on their next erase. Old raw SHA-256 hashes already in
-- events stay enumerable — pre-fix exports cannot be un-shared.
CREATE TABLE scope_secrets (
  scope_id TEXT PRIMARY KEY REFERENCES scopes(id),
  secret_hex TEXT NOT NULL,
  created_at TEXT NOT NULL
);
