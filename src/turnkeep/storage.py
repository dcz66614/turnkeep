"""Turnkeep product storage (T007). Target schema v1 (specs/TARGET_SCHEMA.sql).

- SQLite WAL, foreign_keys=ON, busy_timeout, synchronous=FULL.
- schema_version via PRAGMA user_version + schema_meta table.
- Migrations: 001_initial.sql, 002_record_provenance.sql, applied in order.
  Unknown future version -> refuse.
  A database in the *starter* format (memories.source_refs_json) is refused:
  never apply product migrations onto an unrecognised starter DB.
- No network / model calls anywhere; no write transaction is held across them.
- Shared helpers (_now/_id/_json/_normal/_text/_ids/_sources/_dump/_usable/
  _validate_parents/_revision/_state/_taint/_publish) are used by the
  T008-T017 modules so every module shares one transaction/validity logic.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import TurnkeepError

SCHEMA_VERSION = 7
KINDS = {"requirement", "fact", "decision", "attempt", "procedure", "handoff"}
SOURCE_TYPES = {"user_statement", "tool_observation", "agent_inference", "imported_text"}
STATES = {"active", "conflicted", "superseded", "retracted", "tainted", "expired", "erased"}
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id():
    return uuid.uuid4().hex


def _normal(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_normal(item) for item in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        result = {}
        for key, item in value.items():
            key = unicodedata.normalize("NFC", key)
            if key in result:
                raise TurnkeepError("INVALID_INPUT", "Object keys collide after Unicode normalization")
            result[key] = _normal(item)
        return result
    raise TurnkeepError("INVALID_INPUT", "Only finite JSON values with string object keys are supported")


def _json(value):
    return json.dumps(_normal(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise TurnkeepError("INVALID_INPUT", f"{field} must be a nonempty string")
    return unicodedata.normalize("NFC", value)


def _ids(values):
    if not isinstance(values, (list, tuple, set)):
        raise TurnkeepError("INVALID_INPUT", "IDs must be a list, tuple, or set")
    return sorted({_text(v, "id") for v in values})


def _sources(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise TurnkeepError("INVALID_SOURCE", "At least one typed source reference is required")
    unique = {}
    for source in values:
        if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
            raise TurnkeepError("INVALID_SOURCE", "Source type must describe how the information was obtained")
        _text(source.get("id"), "source id")
        encoded = _json(source)
        unique[encoded] = json.loads(encoded)
    return [unique[k] for k in sorted(unique)]


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def default_data_dir(system=None):
    """Platform data dir (T028): Windows %APPDATA%\\Turnkeep, macOS
    ~/Library/Application Support/turnkeep, Linux ~/.local/share/turnkeep.
    $TURNKEEP_DATA_DIR overrides all of them."""
    import platform as _platform
    override = os.environ.get("TURNKEEP_DATA_DIR")
    if override:
        return Path(override)
    system = system or _platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Turnkeep"
        return Path.home() / "AppData" / "Roaming" / "Turnkeep"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "turnkeep"
    return Path.home() / ".local" / "share" / "turnkeep"


def data_path(scope_id=None, base_dir=None):
    """Deterministic on-disk location; never depends on the caller's cwd.
    Default base: $TURNKEEP_DATA_DIR or the platform dir (T028).
    Returns a Path; creates the directory with 0o700."""
    base = Path(base_dir) if base_dir else default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    if scope_id is None:
        return base
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(scope_id))
    return base / f"{safe}.sqlite3"


def _is_starter_format(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(memories)")]
    return "source_refs_json" in cols


_TARGET_TABLES = {"scopes", "memories", "sources", "memory_sources", "dependencies",
                  "replacements", "runs", "observed_reads", "events", "idempotency",
                  "import_batches", "schema_meta", "record_provenance", "scope_secrets",
                  "erase_flags", "record_erase_keys", "maintenance_pending",
                  "purge_pending"}


def _precheck(db):
    """Refusal checks that run BEFORE any pragma mutates the file header,
    so a refused database is left byte-identical.

    The version and table reads run inside one deferred read transaction
    so concurrent first-openers observe a single snapshot: without it a
    migrator can commit between the two reads and a fresh database looks
    like an unrecognised existing one (user_version still 0 from the
    first read, target tables already present from the second) — a
    spurious "Unrecognised existing database" refusal that failed
    test_concurrent_first_open_all_succeed intermittently (~1 run in 7).
    A deferred read transaction takes only a SHARED lock, which is
    compatible with a migrator's RESERVED lock, so this does not block
    concurrent migration; it just makes the two reads consistent.
    """
    db.execute("BEGIN")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise TurnkeepError("SCHEMA_VERSION", "Database is from a newer Turnkeep version",
                                found=version, supported=SCHEMA_VERSION)
        if version == 0:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" in tables and _is_starter_format(db):
                raise TurnkeepError("SCHEMA_VERSION",
                                    "Starter-format database detected; product migrations never apply onto it. Use a new product database.")
            if tables & _TARGET_TABLES:
                raise TurnkeepError("SCHEMA_VERSION",
                                    "Unrecognised existing database; refusing to migrate. Use a new product database.")
        return version
    finally:
        # A read transaction: COMMIT releases the snapshot; on the raise
        # paths the connection is closed by the caller, which rolls back.
        # Guarded because the transaction may already be gone.
        try:
            db.execute("COMMIT")
        except sqlite3.Error:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass


def _backup_before_upgrade(path):
    backup = Path(str(path) + f".pre-v{SCHEMA_VERSION}.bak")
    if Path(path).exists():
        shutil.copyfile(path, backup)
    return str(backup)


def _migration_files():
    """(target_version, path) for every NNN_*.sql in migrations/, in order.
    The leading number is the schema version the file brings the DB to, so
    a future 003_*.sql is picked up with no migrate() rewrite."""
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        head = path.stem.split("_", 1)[0]
        if head.isdigit():
            files.append((int(head), path))
    return files


def _split_statements(sql):
    """Split a migration script into individual statements, using
    sqlite3.complete_statement so semicolons inside strings or comments
    never split. Statements keep their leading comments."""
    statements, buf = [], ""
    for line in sql.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf)
            buf = ""
    if buf.strip():
        statements.append(buf)
    return statements


def _executable(statement):
    """True unless the statement is only comments, or a PRAGMA / BEGIN /
    COMMIT that must not run inside the migration transaction: the
    connection pragmas are set by open_db(), and BEGIN/COMMIT are owned
    by migrate() itself."""
    body = "\n".join(ln for ln in statement.splitlines()
                     if not ln.strip().startswith("--")).strip()
    if not body:
        return False
    first = re.match(r"[A-Za-z]+", body)
    return first is None or first.group(0).upper() not in ("PRAGMA", "BEGIN", "COMMIT")


def migrate(db, path=None):
    """Apply pending migrations in order. Returns the resulting version.
    Refuses unknown future versions and starter-format databases.

    Each pending migration runs inside its own BEGIN IMMEDIATE
    transaction: its statements are executed one by one with db.execute()
    — never executescript(), which would implicitly COMMIT first and drop
    the write lock — together with the schema_meta row and the
    user_version bump, then a single commit. A second concurrent opener
    waits on the lock, re-reads user_version inside the transaction and
    skips migrations that are already applied."""
    version = _precheck(db)
    for target, sql_file in _migration_files():
        if target <= version:
            continue
        if target > SCHEMA_VERSION:
            raise TurnkeepError("SCHEMA_VERSION",
                                "Migration file beyond supported schema version",
                                found=target, supported=SCHEMA_VERSION)
        # Serialise concurrent first-opens (e.g. two MCP servers on one DB):
        # BEGIN IMMEDIATE takes the write lock; a second opener waits, then
        # re-reads the version inside the tx and skips the migration.
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current < target:
                if current == 0 and path is not None:
                    _backup_before_upgrade(path)
                sql = sql_file.read_text(encoding="utf-8")
                for stmt in _split_statements(sql):
                    if _executable(stmt):
                        db.execute(stmt)
                db.execute("INSERT INTO schema_meta(version, installed_at) VALUES (?, ?)",
                           (target, _now()))
                db.execute(f"PRAGMA user_version={target}")
                version = target
            else:
                version = current
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return version


def _set_journal_mode_wal(db, timeout=10.0):
    """Switch the journal mode to WAL, retrying on a locked database.

    PRAGMA journal_mode does NOT honour the busy handler: if a concurrent
    first-opener already holds the write lock (its migration's
    BEGIN IMMEDIATE), the mode change fails fast with 'database is
    locked' instead of waiting. Retry until the lock clears (bounded by
    timeout); a genuinely busy database still raises after the wait.
    This is the second half of the P1-e first-open race — the first half
    (executescript() dropping the lock inside migrate()) was fixed by
    running migrations statement-by-statement inside one transaction."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def open_db(path, *, timeout=10.0):
    """Open (creating if needed) a product database with the T007 pragmas.
    Returns a sqlite3.Connection with Row factory. Caller owns closing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    db.row_factory = sqlite3.Row
    # Pre-check BEFORE any pragma that mutates the file header, so a refused
    # database is left byte-identical (T007: failed migration keeps original).
    try:
        _precheck(db)
    except TurnkeepError:
        db.close()
        raise
    db.execute("PRAGMA foreign_keys=ON")
    _set_journal_mode_wal(db, timeout=timeout)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=10000")
    # P0-R15-01: explicit secure_delete=ON, read back and fail closed.
    # secure_delete is a PER-CONNECTION setting — it is NOT persisted
    # in the file (its default comes from the SQLite compile option
    # SQLITE_SECURE_DELETE, usually OFF) — so every open must set it
    # and confirm the read-back is 1; setting it once does not hold
    # for the lifetime of the database. Without it, an erasure that
    # deletes a record's verification key leaves the deleted cell
    # bytes in the main DB file (the checkpoint copies the page with
    # its freeblock residue, and VACUUM's compact pages go to the WAL
    # with no checkpoint after them), so the PIN stays recoverable
    # from an ordinary file read while erase reports COMPLETE and
    # the residual report reads clean. A connection that cannot
    # enable it must not be used for erasure at all.
    db.execute("PRAGMA secure_delete=ON")
    row = db.execute("PRAGMA secure_delete").fetchone()
    if row is None or int(row[0]) != 1:
        db.close()
        raise TurnkeepError(
            "SECURE_DELETE_UNAVAILABLE",
            "secure_delete could not be enabled on this connection; "
            "refusing to open the database for erasure")
    migrate(db, path=path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


@contextmanager
def transaction(db, *, write=False):
    """One transaction; BEGIN IMMEDIATE for writes. Rolls back on any error.
    No model/network call may run inside this context (enforced by review,
    not by the DB: callers must finish inference before opening a write tx)."""
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


# ---- shared helpers used by T008-T017 modules (same tx, same rules) ----

def ensure_scope(db, scope_id, display_name=None):
    db.execute("INSERT OR IGNORE INTO scopes(id, display_name, created_at) VALUES (?,?,?)",
               (scope_id, display_name or scope_id, _now()))


def scope_epoch(db, scope_id):
    row = db.execute("SELECT epoch FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if row is None:
        raise TurnkeepError("NOT_FOUND", "Unknown scope", scope_id=scope_id)
    return row[0]


def row_or_404(db, scope_id, record_id):
    row = db.execute("SELECT * FROM memories WHERE scope_id=? AND id=?",
                     (scope_id, record_id)).fetchone()
    if row is None:
        raise TurnkeepError("NOT_FOUND", "Record does not exist in this scope",
                            record_id=record_id)
    return row


def record_sources(db, scope_id, record_id):
    rows = db.execute(
        "SELECT s.id, s.source_type AS type FROM memory_sources ms "
        "JOIN sources s ON s.scope_id=ms.scope_id AND s.id=ms.source_id "
        "WHERE ms.scope_id=? AND ms.memory_id=? ORDER BY s.id",
        (scope_id, record_id))
    return [{"id": r[0], "type": r[1]} for r in rows]


def record_parents(db, scope_id, record_id):
    return [r[0] for r in db.execute(
        "SELECT parent_id FROM dependencies WHERE scope_id=? AND child_id=? ORDER BY parent_id",
        (scope_id, record_id))]


def dump_record(db, row):
    value = dict(row)
    scope_id, rid = value["scope_id"], value["id"]
    value["scope"] = value.pop("scope_id")
    value["value"] = json.loads(value.pop("value_json"))
    value["source_refs"] = record_sources(db, scope_id, rid)
    value["parent_ids"] = record_parents(db, scope_id, rid)
    value["supersedes_ids"] = [r[0] for r in db.execute(
        "SELECT old_id FROM replacements WHERE scope_id=? AND new_id=? ORDER BY old_id",
        (scope_id, rid))]
    return value


def upsert_sources(db, scope_id, sources):
    """Insert sources idempotently — unless the id already exists with a
    different type or content digest (P1-c). Same id + same type + same
    digest stays a no-op, so re-import and repeated remember never error;
    a colliding identity is a hard error, never a silent first-wins keep."""
    for s in sources:
        digest = _digest(s)
        existing = db.execute(
            "SELECT source_type, content_digest FROM sources WHERE scope_id=? AND id=?",
            (scope_id, s["id"])).fetchone()
        if existing is not None:
            # P0-R10-01b: a NULL stored digest means erasure
            # neutralized it (the source id was an erased payload, and
            # the digest — a plain SHA-256 of the source dict — is
            # enumerable for it). The type is still checked, but the
            # digest cannot be re-verified, and it is NOT restored:
            # once neutralized it stays neutralized, so the row's
            # content is no longer digest-verified. A live link to
            # the same source id keeps working; only the collision
            # check is weakened for that row, by design.
            if existing[0] != s["type"] or (
                    existing[1] is not None and existing[1] != digest):
                raise TurnkeepError(
                    "INVALID_SOURCE",
                    "Source id already exists with a different type or content digest; "
                    "source identity must not be silently reused",
                    source_id=s["id"], existing_type=existing[0],
                    incoming_type=s["type"])
            continue
        db.execute(
            "INSERT OR IGNORE INTO sources(id, scope_id, source_type, source_ref, content_digest, observed_at) "
            "VALUES (?,?,?,?,?,?)",
            (s["id"], scope_id, s["type"], s["id"], digest, _now()))


def link_sources(db, scope_id, record_id, sources):
    upsert_sources(db, scope_id, sources)
    for s in sources:
        db.execute("INSERT OR IGNORE INTO memory_sources(scope_id, memory_id, source_id) VALUES (?,?,?)",
                   (scope_id, record_id, s["id"]))


def record_provenance(db, scope_id, local_id, origin_id, origin_scope):
    """Record where a local record was imported from (P1-c, schema 002).
    Runs in the caller's transaction like the other helpers. Re-import of
    the same package stays a no-op: INSERT OR REPLACE overwrites the same
    (scope_id, memory_id) row instead of duplicating it."""
    db.execute("INSERT OR REPLACE INTO record_provenance(scope_id, memory_id, origin_id, origin_scope, imported_at) "
               "VALUES (?,?,?,?,?)",
               (scope_id, local_id, origin_id, origin_scope, _now()))


def get_provenance(db, scope_id, local_id):
    """One provenance row as a dict, or None when the record was never imported."""
    row = db.execute("SELECT scope_id, memory_id, origin_id, origin_scope, imported_at "
                     "FROM record_provenance WHERE scope_id=? AND memory_id=?",
                     (scope_id, local_id)).fetchone()
    return dict(row) if row is not None else None


def usable(db, scope_id, record_id):
    """True iff record and all its ancestors are active (fails closed)."""
    pending, visiting, done = [(record_id, False)], set(), set()
    while pending:
        current, leaving = pending.pop()
        if leaving:
            visiting.discard(current); done.add(current); continue
        if current in done:
            continue
        if current in visiting:
            return False
        row = db.execute("SELECT state FROM memories WHERE scope_id=? AND id=?",
                         (scope_id, current)).fetchone()
        if row is None or row[0] != "active":
            return False
        visiting.add(current)
        pending.append((current, True))
        pending.extend((p, False) for p in record_parents(db, scope_id, current))
    return True


def validate_parents(db, scope_id, parents, *, child_id=None):
    for parent in parents:
        if child_id:
            pending, seen = [parent], set()
            while pending:
                current = pending.pop()
                if current == child_id:
                    raise TurnkeepError("CYCLE_DETECTED", "Dependency would create a cycle")
                if current not in seen:
                    seen.add(current)
                    pending.extend(record_parents(db, scope_id, current))
        if not usable(db, scope_id, parent):
            raise TurnkeepError("INVALID_PARENT",
                                "Parent is missing, unavailable, or outside this scope",
                                parent_id=parent)


def check_revision(row, expected):
    if expected is None:
        raise TurnkeepError("VERSION_REQUIRED", "Mutation requires expected_revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise TurnkeepError("INVALID_INPUT", "expected_revision must be a positive integer")
    if row["revision"] != expected:
        raise TurnkeepError("VERSION_CONFLICT", "Record changed since it was read",
                            expected_revision=expected, current_revision=row["revision"])


def _before(db, scope_id, record_id, changes):
    if record_id not in changes:
        changes[record_id] = dump_record(db, row_or_404(db, scope_id, record_id))


def set_state(db, scope_id, record_id, state, changes, *, cause_event_id=None):
    row = row_or_404(db, scope_id, record_id)
    if row["state"] != state:
        _before(db, scope_id, record_id, changes)
        db.execute("UPDATE memories SET state=?, revision=revision+1, updated_at=?, cause_event_id=? "
                   "WHERE scope_id=? AND id=?",
                   (state, _now(), cause_event_id, scope_id, record_id))


def taint_descendants(db, scope_id, root_ids, changes, *, cause_event_id=None):
    from collections import deque
    pending, visited = deque(root_ids), set(root_ids)
    while pending:
        parent = pending.popleft()
        for row in db.execute("SELECT child_id FROM dependencies WHERE scope_id=? AND parent_id=?",
                              (scope_id, parent)):
            child = row[0]
            if child in visited:
                continue
            visited.add(child)
            pending.append(child)
            state = row_or_404(db, scope_id, child)["state"]
            if state in ("active", "conflicted"):
                set_state(db, scope_id, child, "tainted", changes,
                          cause_event_id=cause_event_id)


def insert_record(db, scope_id, key, encoded, kind, sources, parents, changes,
                  *, state="active", replacements=(), cause_event_id=None, record_id=None):
    # record_id is caller-supplied only for tests that need a pinned id
    # (the R8 short-needle loop evidence: pinning the id removes one
    # source of randomness from the receipt chain); production callers
    # leave it None and the id is a fresh uuid4 hex, as before. A
    # duplicate pinned id is a clean INVALID_INPUT, never a raw
    # IntegrityError from the INSERT below.
    if record_id is not None:
        record_id = _text(record_id, "record_id")
        if db.execute("SELECT 1 FROM memories WHERE id=?", (record_id,)).fetchone() is not None:
            raise TurnkeepError("INVALID_INPUT", f"record id already exists: {record_id!r}")
    else:
        record_id = _id()
    now = _now()
    validate_parents(db, scope_id, parents, child_id=record_id)
    db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at, cause_event_id) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (record_id, scope_id, key, encoded, kind, state, 1, now, now, cause_event_id))
    link_sources(db, scope_id, record_id, sources)
    for parent in parents:
        prev = row_or_404(db, scope_id, parent)["revision"]
        db.execute("INSERT INTO dependencies(scope_id, child_id, parent_id, parent_revision_at_use) VALUES (?,?,?,?)",
                   (scope_id, record_id, parent, prev))
    for old in replacements:
        db.execute("INSERT INTO replacements(scope_id, old_id, new_id, reason) VALUES (?,?,?,?)",
                   (scope_id, old, record_id, "replacement"))
    changes[record_id] = None
    return record_id


def publish_event(db, scope_id, event_type, actor_binding, changes, *, metadata=None,
                  event_id=None):
    """Bump scope epoch once per change-set and write one event row, in-tx.
    metadata must NOT contain record bodies (refs only). When event_id is
    given (P1-g: pre-generated by the caller so state changes can cite it
    as their cause_event_id), the event row uses that id; else one is
    generated. Returns the new epoch, unchanged."""
    if not changes:
        return scope_epoch(db, scope_id)
    epoch = scope_epoch(db, scope_id) + 1
    db.execute("UPDATE scopes SET epoch=? WHERE id=?", (epoch, scope_id))
    affected = sorted(changes.keys())
    db.execute("INSERT INTO events(id, scope_id, scope_epoch, event_type, actor_binding, affected_ids_json, metadata_json, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (event_id if event_id is not None else _id(), scope_id, epoch, event_type, actor_binding, _json(affected),
                _json(metadata or {}), _now()))
    return epoch
