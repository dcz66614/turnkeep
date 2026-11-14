-- Turnkeep target schema draft, design version 0.1.
-- NOT a migration for the delivered starter database.
-- Apply only to a new product database after implementation review.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
BEGIN;

CREATE TABLE schema_meta (
  version INTEGER PRIMARY KEY CHECK(version > 0),
  installed_at TEXT NOT NULL
);
CREATE TABLE scopes (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0),
  created_at TEXT NOT NULL
);
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  key TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  kind TEXT NOT NULL CHECK(kind IN ('requirement','fact','decision','attempt','procedure','handoff')),
  state TEXT NOT NULL CHECK(state IN ('active','conflicted','superseded','retracted','tainted','expired','erased')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  valid_until TEXT,
  cause_event_id TEXT,
  UNIQUE(scope_id,id)
);
CREATE UNIQUE INDEX one_active_value_per_slot ON memories(scope_id,key) WHERE state='active';
CREATE INDEX memory_scope_state ON memories(scope_id,state,updated_at);
CREATE INDEX memory_slot_history ON memories(scope_id,key,created_at);

CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  source_type TEXT NOT NULL CHECK(source_type IN ('user_statement','tool_observation','agent_inference','imported_text')),
  source_ref TEXT NOT NULL,
  content_digest TEXT,
  observed_at TEXT NOT NULL,
  payload_ref TEXT,
  erased_at TEXT,
  UNIQUE(scope_id,id)
);
CREATE TABLE memory_sources (
  scope_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  PRIMARY KEY(scope_id,memory_id,source_id),
  FOREIGN KEY(scope_id,memory_id) REFERENCES memories(scope_id,id),
  FOREIGN KEY(scope_id,source_id) REFERENCES sources(scope_id,id)
);
CREATE TABLE dependencies (
  scope_id TEXT NOT NULL,
  child_id TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  parent_revision_at_use INTEGER NOT NULL CHECK(parent_revision_at_use > 0),
  relation TEXT NOT NULL DEFAULT 'requires' CHECK(relation='requires'),
  PRIMARY KEY(scope_id,child_id,parent_id),
  CHECK(child_id <> parent_id),
  FOREIGN KEY(scope_id,child_id) REFERENCES memories(scope_id,id),
  FOREIGN KEY(scope_id,parent_id) REFERENCES memories(scope_id,id)
);
CREATE INDEX dependency_reverse ON dependencies(scope_id,parent_id,child_id);
-- Arbitrary cycles must be rejected by the application transaction.
CREATE TABLE replacements (
  scope_id TEXT NOT NULL,
  old_id TEXT NOT NULL,
  new_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(scope_id,old_id,new_id),
  CHECK(old_id <> new_id),
  FOREIGN KEY(scope_id,old_id) REFERENCES memories(scope_id,id),
  FOREIGN KEY(scope_id,new_id) REFERENCES memories(scope_id,id)
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  principal_binding TEXT NOT NULL,
  task TEXT NOT NULL,
  snapshot_epoch INTEGER NOT NULL CHECK(snapshot_epoch >= 0),
  snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
  status TEXT NOT NULL CHECK(status IN ('open','accepted','abandoned')),
  created_at TEXT NOT NULL,
  accepted_at TEXT,
  result_memory_id TEXT,
  request_digest TEXT,
  receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
  UNIQUE(scope_id,id),
  FOREIGN KEY(scope_id,result_memory_id) REFERENCES memories(scope_id,id)
);
CREATE TABLE observed_reads (
  scope_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  observed_revision INTEGER NOT NULL,
  PRIMARY KEY(scope_id,run_id,memory_id),
  FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,id),
  FOREIGN KEY(scope_id,memory_id) REFERENCES memories(scope_id,id)
);
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  scope_epoch INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  actor_binding TEXT NOT NULL,
  affected_ids_json TEXT NOT NULL CHECK(json_valid(affected_ids_json)),
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
);
-- metadata_json must exclude sensitive record bodies; point to controlled payloads.
CREATE INDEX events_epoch ON events(scope_id,scope_epoch);
CREATE TABLE idempotency (
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  principal_binding TEXT NOT NULL,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope_id,principal_binding,operation,idempotency_key)
);
CREATE TABLE import_batches (
  id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL REFERENCES scopes(id),
  status TEXT NOT NULL CHECK(status IN ('staging','validated','published','failed')),
  source_digest TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json))
);
COMMIT;

