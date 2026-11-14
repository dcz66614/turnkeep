"""Explicit-key memory writes (T010). Port of the starter Store.remember
semantics onto the target schema (sources in sources/memory_sources,
parents in dependencies, events via publish_event).

- Same scope/key/normalised value on an active record: reuse; appending a
  source or parent is a content change -> requires expected_revision (CAS),
  else VERSION_REQUIRED; no-op if nothing changed.
- Different value, no correction relation: BOTH records become conflicted
  and leave the default active view; a conflict group is retained.
- Same value on a conflicted record: CONFLICT_REQUIRES_RESOLVE (cannot be
  reused/merged around the resolve flow).
- Only inactive history left for the key: new value needs explicit
  replaces_ids + new source, else REPLACEMENT_REQUIRED. This prevents
  accidental resurrection; it does NOT stop re-keying / rewording whitewash
  (contract §4 — recorded, not claimed).
- Imported/agent-inference sources keep their type; they never become
  user_statement and never confer authority.
- Collection values need an explicitly declared set schema; a plain slot is
  single-valued and cannot silently escape conflict via multi-values.
"""
from __future__ import annotations
import json
from .errors import TurnkeepError
from .idempotency import (request_digest, check as idem_check,
                          store as idem_store)
from .storage import (transaction, ensure_scope, scope_epoch, row_or_404,
                      dump_record, insert_record, set_state, taint_descendants,
                      publish_event, _id, _json, _text, _ids, _sources, _normal, _now,
                      check_revision, validate_parents, upsert_sources)


def remember(db, scope_id, key, value, *, source_refs, kind="fact",
             parent_ids=(), expected_revision=None, replaces_ids=(),
             actor_binding="local", idempotency_key="", record_id=None):
    """idempotency_key: empty means no idempotency (legacy callers). A
    non-empty key replays the stored response for the same request digest
    (same key/value/kind/sources/parents/revisions) and raises
    IDEMPOTENCY_MISMATCH for the same key with a different request —
    same store/lookup semantics as finish (commits.py). The check runs
    first inside the write tx, so a replay returns the original record
    even after the record was later corrected/retracted; the store runs
    in the same tx as the write, so a failed request stores nothing.
    Erasure ends the replay promise (P0-R10-01): erasing the record
    neutralizes its request digest, so a replay then raises
    IDEMPOTENCY_MISMATCH instead of returning the original record."""
    scope_id, key = _text(scope_id, "scope"), _text(key, "key")
    if kind not in ("requirement", "fact", "decision", "attempt", "procedure", "handoff"):
        raise TurnkeepError("INVALID_INPUT", "Unknown memory kind")
    encoded = _json(value)
    sources = _sources(source_refs)
    parents = _ids(parent_ids)
    replaces = _ids(replaces_ids)
    digest = request_digest("remember", scope_id, actor_binding,
                            {"key": key, "value": value, "kind": kind,
                             "sources": sources, "parent_ids": parents,
                             "expected_revision": expected_revision,
                             "replaces_ids": replaces}) if idempotency_key else None
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        if idempotency_key:
            prior = idem_check(db, scope_id, actor_binding, "remember",
                               idempotency_key, digest)
            if prior is not None:
                return prior
        candidates = db.execute(
            "SELECT * FROM memories WHERE scope_id=? AND key=? AND state IN ('active','conflicted') ORDER BY id",
            (scope_id, key)).fetchall()
        if any(r["kind"] != kind for r in candidates):
            raise TurnkeepError("KIND_MISMATCH", "One key must retain one declared kind")
        same = next((r for r in candidates if r["value_json"] == encoded), None)
        changes = {}
        # P1-g: ONE event id for the whole change-set, generated up front
        # so every path below (merge, conflict, plain remember) cites it.
        ev = _id()
        if same is not None:
            if same["state"] == "conflicted":
                raise TurnkeepError("CONFLICT_REQUIRES_RESOLVE",
                                    "A matching conflicted value cannot be reused or merged")
            if replaces:
                raise TurnkeepError("INVALID_INPUT", "replaces_ids is only valid when creating a new record")
            old = dump_record(db, same)
            combined_sources = _sources(old["source_refs"] + sources)
            combined_parents = sorted(set(old["parent_ids"]) | set(parents))
            changed = combined_sources != old["source_refs"] or combined_parents != old["parent_ids"]
            if changed:
                check_revision(same, expected_revision)
                validate_parents(db, scope_id, combined_parents, child_id=same["id"])
                _before(db, scope_id, same["id"], changes)
                upsert_sources(db, scope_id, combined_sources)
                for s in combined_sources:
                    db.execute("INSERT OR IGNORE INTO memory_sources(scope_id, memory_id, source_id) VALUES (?,?,?)",
                               (scope_id, same["id"], s["id"]))
                for parent in combined_parents:
                    prev = row_or_404(db, scope_id, parent)["revision"]
                    db.execute("INSERT OR IGNORE INTO dependencies(scope_id, child_id, parent_id, parent_revision_at_use) VALUES (?,?,?,?)",
                               (scope_id, same["id"], parent, prev))
                db.execute("UPDATE memories SET revision=revision+1, updated_at=?, cause_event_id=? "
                           "WHERE scope_id=? AND id=?",
                           (_now(), ev, scope_id, same["id"]))
                publish_event(db, scope_id, "merge_provenance", actor_binding, changes,
                              metadata={"key": key}, event_id=ev)
            result = dump_record(db, row_or_404(db, scope_id, same["id"]))
            if idempotency_key:
                idem_store(db, scope_id, actor_binding, "remember",
                           idempotency_key, digest, result)
            return result
        historical = db.execute("SELECT id, state FROM memories WHERE scope_id=? AND key=?",
                                (scope_id, key)).fetchall()
        if candidates and replaces:
            raise TurnkeepError("INVALID_INPUT", "Use correct or resolve_conflict for current candidates")
        if not candidates and historical and not replaces:
            raise TurnkeepError("REPLACEMENT_REQUIRED",
                                "This key has inactive history; explicitly identify replacements")
        for old_id in replaces:
            old = row_or_404(db, scope_id, old_id)
            if old["key"] != key or old["state"] in ("active", "conflicted"):
                raise TurnkeepError("INVALID_REPLACEMENT",
                                    "Replacement must identify inactive history of the same key")
        if candidates:
            for row in candidates:
                set_state(db, scope_id, row["id"], "conflicted", changes,
                          cause_event_id=ev)
            taint_descendants(db, scope_id, [r["id"] for r in candidates], changes,
                              cause_event_id=ev)
        # record_id pins the new record's id (tests only: the R8
        # short-needle loop evidence); production callers leave it None.
        record_id = insert_record(db, scope_id, key, encoded, kind, sources, parents, changes,
                                  state="conflicted" if candidates else "active",
                                  replacements=replaces, cause_event_id=ev,
                                  record_id=record_id)
        publish_event(db, scope_id, "remember_conflict" if candidates else "remember",
                      actor_binding, changes, metadata={"key": key}, event_id=ev)
        result = dump_record(db, row_or_404(db, scope_id, record_id))
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "remember",
                       idempotency_key, digest, result)
        return result


def _before(db, scope_id, record_id, changes):
    if record_id not in changes:
        changes[record_id] = dump_record(db, row_or_404(db, scope_id, record_id))


def list_conflicts(db, scope_id, key=None):
    q = ("SELECT * FROM memories WHERE scope_id=? AND state='conflicted'")
    args = [scope_id]
    if key:
        q += " AND key=?"; args.append(key)
    q += " ORDER BY key, created_at"
    return [dump_record(db, r) for r in db.execute(q, args)]


def active_records(db, scope_id, *, include_history=False):
    q = "SELECT * FROM memories WHERE scope_id=?"
    args = [scope_id]
    if not include_history:
        q += " AND state='active'"
    q += " ORDER BY key, created_at"
    return [dump_record(db, r) for r in db.execute(q, args)]
