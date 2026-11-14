"""Transactional project memory; deliberately small, local, and conservative.

Every public ID-based method also requires a scope. This is a correctness
boundary for a trusted local user, not protection against direct database access.
No user-supplied content is evaluated as code, instructions, or permissions.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import TurnkeepError

KINDS = {"requirement", "fact", "decision", "attempt", "procedure", "handoff"}
SOURCE_TYPES = {"user_statement", "tool_observation", "agent_inference", "imported_text"}
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    scope TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0)
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL REFERENCES scopes(scope),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
        ('requirement','fact','decision','attempt','procedure','handoff')),
    state TEXT NOT NULL CHECK(state IN
        ('active','conflicted','superseded','retracted','tainted')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    UNIQUE(scope, id)
);
CREATE INDEX IF NOT EXISTS memory_slot ON memories(scope, key, state);
CREATE TABLE IF NOT EXISTS edges (
    scope TEXT NOT NULL, parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY(scope, parent_id, child_id), CHECK(parent_id <> child_id),
    FOREIGN KEY(scope, parent_id) REFERENCES memories(scope, id),
    FOREIGN KEY(scope, child_id) REFERENCES memories(scope, id)
);
CREATE INDEX IF NOT EXISTS edge_child ON edges(scope, child_id);
CREATE TABLE IF NOT EXISTS replacements (
    scope TEXT NOT NULL, new_id TEXT NOT NULL, old_id TEXT NOT NULL,
    PRIMARY KEY(scope, new_id, old_id), CHECK(new_id <> old_id),
    FOREIGN KEY(scope, new_id) REFERENCES memories(scope, id),
    FOREIGN KEY(scope, old_id) REFERENCES memories(scope, id)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(scope),
    epoch INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL, changes_json TEXT NOT NULL, detail_json TEXT NOT NULL,
    UNIQUE(scope, epoch)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(scope),
    actor TEXT NOT NULL, task TEXT NOT NULL, read_epoch INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','succeeded')),
    digest TEXT, receipt_json TEXT,
    CHECK((status='open' AND digest IS NULL AND receipt_json IS NULL)
       OR (status='succeeded' AND digest IS NOT NULL AND receipt_json IS NOT NULL))
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id() -> str:
    return uuid.uuid4().hex


def _normal(value: Any) -> Any:
    """JSON normalization: Unicode NFC, object key order; no semantic rewriting."""
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


def _json(value: Any) -> str:
    return json.dumps(_normal(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurnkeepError("INVALID_INPUT", f"{field} must be a nonempty string")
    return unicodedata.normalize("NFC", value)


def _ids(values) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        raise TurnkeepError("INVALID_INPUT", "IDs must be a list, tuple, or set")
    return sorted({_text(value, "id") for value in values})


def _sources(values) -> list[dict]:
    if not isinstance(values, (list, tuple)) or not values:
        raise TurnkeepError("INVALID_SOURCE", "At least one typed source reference is required")
    unique = {}
    for source in values:
        if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
            raise TurnkeepError("INVALID_SOURCE", "Source type must describe how the information was obtained")
        _text(source.get("id"), "source id")
        encoded = _json(source)
        unique[encoded] = json.loads(encoded)
    return [unique[key] for key in sorted(unique)]


class Store:
    """One connection per Store. Create a separate instance per thread/process.

    actor is a local label from host configuration. It is not authentication.
    Long-running inference occurs outside all database transactions.
    """

    def __init__(self, path: str | Path, *, actor: str = "local", timeout: float = 10):
        self.path = str(path)
        self.actor = _text(actor, "actor")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self._db.close()
            raise TurnkeepError("SCHEMA_VERSION", "Unsupported database schema", found=version)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _tx(self, *, write=False):
        self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self._db
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @staticmethod
    def _scope(db, scope):
        db.execute("INSERT OR IGNORE INTO scopes(scope) VALUES (?)", (scope,))

    @staticmethod
    def _epoch(db, scope):
        row = db.execute("SELECT epoch FROM scopes WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row(db, scope, record_id):
        row = db.execute("SELECT * FROM memories WHERE scope=? AND id=?", (scope, record_id)).fetchone()
        if row is None:
            raise TurnkeepError("NOT_FOUND", "Record does not exist in this scope", record_id=record_id)
        return row

    @staticmethod
    def _parents(db, scope, record_id):
        return [row[0] for row in db.execute(
            "SELECT parent_id FROM edges WHERE scope=? AND child_id=? ORDER BY parent_id",
            (scope, record_id))]

    def _dump(self, db, row):
        value = dict(row)
        value["value"] = json.loads(value.pop("value_json"))
        value["source_refs"] = json.loads(value.pop("source_refs_json"))
        value["parent_ids"] = self._parents(db, value["scope"], value["id"])
        value["supersedes_ids"] = [r[0] for r in db.execute(
            "SELECT old_id FROM replacements WHERE scope=? AND new_id=? ORDER BY old_id",
            (value["scope"], value["id"]))]
        return value

    def _usable(self, db, scope, record_id):
        # Iterative DFS fails closed on inactive/missing ancestors or corrupt cycles.
        pending, visiting, done = [(record_id, False)], set(), set()
        while pending:
            current, leaving = pending.pop()
            if leaving:
                visiting.discard(current)
                done.add(current)
                continue
            if current in done:
                continue
            if current in visiting:
                return False
            row = db.execute("SELECT state FROM memories WHERE scope=? AND id=?", (scope, current)).fetchone()
            if row is None or row[0] != "active":
                return False
            visiting.add(current)
            pending.append((current, True))
            pending.extend((parent, False) for parent in self._parents(db, scope, current))
        return True

    def _validate_parents(self, db, scope, parents, *, child_id=None):
        for parent in parents:
            if child_id:
                pending, seen = [parent], set()
                while pending:
                    current = pending.pop()
                    if current == child_id:
                        raise TurnkeepError("CYCLIC_DEPENDENCY", "Dependency would create a cycle")
                    if current not in seen:
                        seen.add(current)
                        pending.extend(self._parents(db, scope, current))
            if not self._usable(db, scope, parent):
                raise TurnkeepError("INVALID_PARENT", "Parent is missing, unavailable, or outside this scope",
                                    parent_id=parent)

    @staticmethod
    def _revision(row, expected):
        if expected is None:
            raise TurnkeepError("VERSION_REQUIRED", "Mutation requires expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise TurnkeepError("INVALID_INPUT", "expected_revision must be a positive integer")
        if row["revision"] != expected:
            raise TurnkeepError("VERSION_CONFLICT", "Record changed since it was read",
                                expected_revision=expected, current_revision=row["revision"])

    def _before(self, db, scope, record_id, changes):
        if record_id not in changes:
            changes[record_id] = self._dump(db, self._row(db, scope, record_id))

    def _state(self, db, scope, record_id, state, changes):
        row = self._row(db, scope, record_id)
        if row["state"] != state:
            self._before(db, scope, record_id, changes)
            db.execute("UPDATE memories SET state=?, revision=revision+1, updated_at=? WHERE scope=? AND id=?",
                       (state, _now(), scope, record_id))

    def _taint(self, db, scope, root_ids, changes):
        pending, visited = deque(root_ids), set(root_ids)
        while pending:
            parent = pending.popleft()
            for row in db.execute("SELECT child_id FROM edges WHERE scope=? AND parent_id=?", (scope, parent)):
                child = row[0]
                if child in visited:
                    continue
                visited.add(child)
                pending.append(child)
                state = self._row(db, scope, child)["state"]
                if state in ("active", "conflicted"):
                    self._state(db, scope, child, "tainted", changes)

    def _insert(self, db, scope, key, encoded, kind, sources, parents, changes,
                *, state="active", replacements=()):
        record_id, now = _id(), _now()
        self._validate_parents(db, scope, parents, child_id=record_id)
        db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (record_id, scope, key, encoded, kind, state, 1, now, now, _json(sources)))
        db.executemany("INSERT INTO edges VALUES (?,?,?)", [(scope, parent, record_id) for parent in parents])
        db.executemany("INSERT INTO replacements VALUES (?,?,?)", [(scope, record_id, old) for old in replacements])
        changes[record_id] = None
        return record_id

    def _publish(self, db, scope, action, changes, **detail):
        if not changes:
            return self._epoch(db, scope)
        epoch = self._epoch(db, scope) + 1
        db.execute("UPDATE scopes SET epoch=? WHERE scope=?", (epoch, scope))
        entries = [{"record_id": record_id, "before": before,
                    "after": self._dump(db, self._row(db, scope, record_id))}
                   for record_id, before in sorted(changes.items())]
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                   (_id(), scope, epoch, action, self.actor, _now(), _json(entries), _json(detail)))
        return epoch

    def remember(self, scope, key, value, *, source_refs, kind="fact", parent_ids=(),
                 expected_revision=None, replaces_ids=()):
        scope, key = _text(scope, "scope"), _text(key, "key")
        if kind not in KINDS:
            raise TurnkeepError("INVALID_INPUT", "Unknown memory kind")
        encoded, sources = _json(value), _sources(source_refs)
        parents, replaces = _ids(parent_ids), _ids(replaces_ids)
        with self._tx(write=True) as db:
            self._scope(db, scope)
            candidates = list(db.execute("SELECT * FROM memories WHERE scope=? AND key=? "
                                        "AND state IN ('active','conflicted') ORDER BY id", (scope, key)))
            if any(row["kind"] != kind for row in candidates):
                raise TurnkeepError("KIND_MISMATCH", "One key must retain one declared kind")
            same = next((row for row in candidates if row["value_json"] == encoded), None)
            changes = {}
            if same:
                if same["state"] == "conflicted":
                    raise TurnkeepError("CONFLICT_REQUIRES_RESOLVE", "A matching conflicted value cannot be reused or merged")
                if replaces:
                    raise TurnkeepError("INVALID_INPUT", "replaces_ids is only valid when creating a new record")
                old = self._dump(db, same)
                combined_sources = _sources(old["source_refs"] + sources)
                combined_parents = sorted(set(old["parent_ids"]) | set(parents))
                if expected_revision is not None:
                    self._revision(same, expected_revision)
                changed = combined_sources != old["source_refs"] or combined_parents != old["parent_ids"]
                if changed:
                    self._revision(same, expected_revision)
                    self._validate_parents(db, scope, combined_parents, child_id=same["id"])
                    changes[same["id"]] = old
                    db.execute("UPDATE memories SET source_refs_json=?, revision=revision+1, updated_at=? WHERE id=?",
                               (_json(combined_sources), _now(), same["id"]))
                    db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?)",
                                   [(scope, parent, same["id"]) for parent in combined_parents])
                    self._publish(db, scope, "merge_provenance", changes)
                return self._dump(db, self._row(db, scope, same["id"]))
            historical = list(db.execute("SELECT id,state FROM memories WHERE scope=? AND key=?", (scope, key)))
            if candidates and replaces:
                raise TurnkeepError("INVALID_INPUT", "Use correct or resolve_conflict for current candidates")
            if not candidates and historical and not replaces:
                raise TurnkeepError("REPLACEMENT_REQUIRED", "This key has inactive history; explicitly identify replacements")
            for old_id in replaces:
                old = self._row(db, scope, old_id)
                if old["key"] != key or old["state"] in ("active", "conflicted"):
                    raise TurnkeepError("INVALID_REPLACEMENT", "Replacement must identify inactive history of the same key")
            self._validate_parents(db, scope, parents)
            if candidates:
                for row in candidates:
                    self._state(db, scope, row["id"], "conflicted", changes)
                self._taint(db, scope, [row["id"] for row in candidates], changes)
            record_id = self._insert(db, scope, key, encoded, kind, sources, parents, changes,
                                     state="conflicted" if candidates else "active", replacements=replaces)
            self._publish(db, scope, "remember_conflict" if candidates else "remember", changes)
            return self._dump(db, self._row(db, scope, record_id))

    def correct(self, scope, old_id, expected_revision, new_value, *, source_refs, parent_ids=None):
        scope, old_id = _text(scope, "scope"), _text(old_id, "record id")
        encoded, sources = _json(new_value), _sources(source_refs)
        with self._tx(write=True) as db:
            old = self._row(db, scope, old_id)
            self._revision(old, expected_revision)
            if old["state"] == "conflicted":
                raise TurnkeepError("CONFLICT_REQUIRES_RESOLVE", "Resolve the complete candidate set")
            if old["state"] != "active" or not self._usable(db, scope, old_id):
                raise TurnkeepError("INVALID_STATE", "Only a usable active record can be corrected")
            parents = self._parents(db, scope, old_id) if parent_ids is None else _ids(parent_ids)
            changes = {}
            self._state(db, scope, old_id, "superseded", changes)
            self._taint(db, scope, [old_id], changes)
            new_id = self._insert(db, scope, old["key"], encoded, old["kind"], sources, parents,
                                  changes, replacements=[old_id])
            self._publish(db, scope, "correct", changes, superseded_id=old_id)
            return self._dump(db, self._row(db, scope, new_id))

    def resolve_conflict(self, scope, key, expected_candidates, new_value, *, source_refs, parent_ids=()):
        scope, key = _text(scope, "scope"), _text(key, "key")
        if not isinstance(expected_candidates, dict) or any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 1
            for k, v in expected_candidates.items()
        ):
            raise TurnkeepError("INVALID_INPUT", "expected_candidates must map record IDs to positive revisions")
        encoded, sources, parents = _json(new_value), _sources(source_refs), _ids(parent_ids)
        with self._tx(write=True) as db:
            rows = list(db.execute("SELECT * FROM memories WHERE scope=? AND key=? "
                                   "AND state IN ('active','conflicted') ORDER BY id", (scope, key)))
            actual = {row["id"]: row["revision"] for row in rows}
            if actual != expected_candidates:
                raise TurnkeepError("CANDIDATE_SET_CHANGED", "Candidate set or revisions changed", current_candidates=actual)
            if not any(row["state"] == "conflicted" for row in rows):
                raise TurnkeepError("NO_CONFLICT", "There is no unresolved conflict for this key")
            changes = {}
            for row in rows:
                self._state(db, scope, row["id"], "superseded", changes)
            self._taint(db, scope, list(actual), changes)
            new_id = self._insert(db, scope, key, encoded, rows[0]["kind"], sources, parents,
                                  changes, replacements=sorted(actual))
            self._publish(db, scope, "resolve_conflict", changes)
            return self._dump(db, self._row(db, scope, new_id))

    def retract(self, scope, record_id, expected_revision, reason):
        scope, record_id, reason = _text(scope, "scope"), _text(record_id, "record id"), _text(reason, "reason")
        with self._tx(write=True) as db:
            row = self._row(db, scope, record_id)
            self._revision(row, expected_revision)
            if row["state"] not in ("active", "conflicted", "tainted"):
                raise TurnkeepError("INVALID_STATE", "Record is already retired")
            changes = {}
            self._state(db, scope, record_id, "retracted", changes)
            self._taint(db, scope, [record_id], changes)
            self._publish(db, scope, "retract", changes, reason=reason)
            return self._dump(db, self._row(db, scope, record_id))

    def _records(self, db, scope, include_history=False):
        rows = db.execute("SELECT * FROM memories WHERE scope=? ORDER BY key,created_at,id", (scope,)).fetchall()
        records = []
        for row in rows:
            usable = self._usable(db, scope, row["id"])
            if include_history or usable:
                record = self._dump(db, row)
                record["usable"] = usable
                records.append(record)
        return records

    def records(self, scope, *, include_history=False):
        scope = _text(scope, "scope")
        with self._tx() as db:
            return self._records(db, scope, include_history)

    def inspect(self, scope, record_id, *, include_history=False):
        scope, record_id = _text(scope, "scope"), _text(record_id, "record id")
        with self._tx() as db:
            row = self._row(db, scope, record_id)
            usable = self._usable(db, scope, record_id)
            if not include_history and not usable:
                raise TurnkeepError("NOT_CURRENT", "Record is unavailable; explicit history access is required")
            result = self._dump(db, row)
            result["usable"] = usable
            return result

    def _snapshot(self, db, scope):
        epoch = self._epoch(db, scope)  # First read establishes the SQLite snapshot.
        memories = self._records(db, scope)
        conflicts = [self._dump(db, row) for row in db.execute(
            "SELECT * FROM memories WHERE scope=? AND state='conflicted' ORDER BY key,id", (scope,))]
        return {"scope": scope, "epoch": epoch, "memories": memories, "conflicts": conflicts}

    def begin(self, scope, task):
        scope, task = _text(scope, "scope"), _text(task, "task")
        with self._tx() as db:
            snapshot = self._snapshot(db, scope)
        # Persist exactly that snapshot. An intervening write may make it stale,
        # but cannot mix epochs with data; finish will reject a stale run.
        run_id, now = _id(), _now()
        with self._tx(write=True) as db:
            self._scope(db, scope)
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (run_id, scope, self.actor, task, snapshot["epoch"], _json(snapshot), now, "open", None, None))
        return {"run_id": run_id, "actor": self.actor, "task": task, "created_at": now, "snapshot": snapshot}

    @staticmethod
    def _run(db, scope, run_id):
        row = db.execute("SELECT * FROM runs WHERE scope=? AND id=?", (scope, run_id)).fetchone()
        if row is None:
            raise TurnkeepError("NOT_FOUND", "Run does not exist in this scope")
        return row

    def _diff(self, db, scope, epoch):
        events = list(db.execute("SELECT epoch,action,changes_json FROM events WHERE scope=? AND epoch>? ORDER BY epoch",
                                (scope, epoch)))
        ids, keys, reasons = set(), set(), []
        for event in events:
            for change in json.loads(event["changes_json"]):
                ids.add(change["record_id"])
                keys.add(change["after"]["key"])
            reasons.append({"epoch": event["epoch"], "action": event["action"]})
        return {"from_epoch": epoch, "to_epoch": self._epoch(db, scope), "changed_record_ids": sorted(ids),
                "changed_keys": sorted(keys), "changes": reasons,
                "why": "Project memory changed after this run read its snapshot; reread changes and recompute affected work."}

    def resume(self, scope, run_id):
        scope, run_id = _text(scope, "scope"), _text(run_id, "run id")
        with self._tx() as db:
            run = self._run(db, scope, run_id)
            return {"previous_run_id": run_id, "diff": self._diff(db, scope, run["read_epoch"]),
                    "candidate_snapshot": self._snapshot(db, scope),
                    "next_step": "Call begin for a new run and recompute; the old run remains bound to its old snapshot."}

    def finish(self, scope, run_id, result, *, parent_ids=()):
        scope, run_id = _text(scope, "scope"), _text(run_id, "run id")
        encoded, declared = _json(result), _ids(parent_ids)
        digest = hashlib.sha256(_json({"result": json.loads(encoded), "parent_ids": declared}).encode()).hexdigest()
        with self._tx(write=True) as db:
            run = self._run(db, scope, run_id)
            if run["actor"] != self.actor:
                raise TurnkeepError("ACTOR_MISMATCH", "Only the host actor label that began this run may finish it")
            if run["status"] == "succeeded":
                if run["digest"] != digest:
                    raise TurnkeepError("IDEMPOTENCY_MISMATCH", "This run already committed a different request")
                return json.loads(run["receipt_json"])
            if self._epoch(db, scope) != run["read_epoch"]:
                raise TurnkeepError("STALE_SNAPSHOT", "Snapshot changed; no result was published",
                                    **self._diff(db, scope, run["read_epoch"]))
            snapshot = json.loads(run["snapshot_json"])
            parents = sorted(set(declared) | {record["id"] for record in snapshot["memories"]})
            self._validate_parents(db, scope, parents)
            changes = {}
            source = [{"id": f"run:{run_id}", "type": "agent_inference", "actor_label": run["actor"]}]
            record_id = self._insert(db, scope, f"run.{run_id}.result", encoded, "handoff", source, parents, changes)
            epoch = self._publish(db, scope, "finish", changes, run_id=run_id, request_digest=digest)
            receipt = {"run_id": run_id, "scope": scope, "record_id": record_id, "request_digest": digest,
                       "read_epoch": run["read_epoch"], "commit_epoch": epoch, "parent_ids": parents,
                       "committed_at": _now(), "status_at_commit": "active",
                       "guarantee": "local_sqlite_commit_only"}
            receipt["integrity_hash"] = hashlib.sha256(_json(receipt).encode()).hexdigest()
            db.execute("UPDATE runs SET status='succeeded',digest=?,receipt_json=? WHERE scope=? AND id=?",
                       (digest, _json(receipt), scope, run_id))
            return receipt

    def history(self, scope, record_id=None):
        scope = _text(scope, "scope")
        with self._tx() as db:
            if record_id is not None:
                self._row(db, scope, _text(record_id, "record id"))
            output = []
            for row in db.execute("SELECT * FROM events WHERE scope=? ORDER BY epoch", (scope,)):
                event = dict(row)
                event["changes"] = json.loads(event.pop("changes_json"))
                event["detail"] = json.loads(event.pop("detail_json"))
                if record_id is None or any(change["record_id"] == record_id for change in event["changes"]):
                    output.append(event)
            return output

    def impact(self, scope, record_id):
        """Return one shortest dependency path per descendant, including history."""
        scope, record_id = _text(scope, "scope"), _text(record_id, "record id")
        with self._tx() as db:
            self._row(db, scope, record_id)
            pending, seen, output = deque([(record_id, [record_id])]), {record_id}, []
            while pending:
                parent, path = pending.popleft()
                for row in db.execute("SELECT child_id FROM edges WHERE scope=? AND parent_id=? ORDER BY child_id",
                                      (scope, parent)):
                    child = row[0]
                    if child not in seen:
                        seen.add(child)
                        child_path = path + [child]
                        item = self._dump(db, self._row(db, scope, child))
                        output.append({"record_id": child, "key": item["key"], "state": item["state"],
                                       "dependency_path": child_path})
                        pending.append((child, child_path))
            return output

    def backup(self, destination: str | Path):
        """Consistent SQLite backup; no encryption or logical erasure is implied."""
        if self.path != ":memory:" and Path(destination).resolve() == Path(self.path).resolve():
            raise TurnkeepError("INVALID_INPUT", "Backup destination must differ from the database")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(target)) as other:
            self._db.backup(other)
