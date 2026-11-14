# Storage (T007)

- Engine: SQLite, one file per scope via `storage.data_path()` (default
  `$TURNKEEP_DATA_DIR` or `~/.local/share/turnkeep/`, dir 0o700, file 0o600).
  The path never depends on the caller's cwd.
- Pragmas on every open: `WAL`, `foreign_keys=ON`, `synchronous=FULL`,
  `busy_timeout=10000`.
- Schema: target schema v1 (`specs/TARGET_SCHEMA.sql`, 12 tables) applied by
  migration `src/turnkeep/migrations/001_initial.sql`; `PRAGMA user_version=1`
  + `schema_meta` row. Unknown future version -> refuse. A starter-format DB
  (memories.source_refs_json) is refused — product migrations never apply onto
  it; use a new product database. Upgrade backs the file up first
  (`<db>.pre-v1.bak`); a failed migration rolls back and keeps the original.
- Constraints are real DB constraints: FKs, `one_active_value_per_slot`
  partial unique index, kind/state CHECKs, `json_valid` CHECKs — not just
  Python checks (see tests/storage/test_constraints.py).
- No network / model calls; write transactions are `BEGIN IMMEDIATE` and
  short. Events, epoch bump and idempotency rows are written in the same
  transaction as the business change (see T009).
