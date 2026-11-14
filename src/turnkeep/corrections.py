"""CAS corrections, retract and full-candidate conflict resolution (T011).

- correct(): BEGIN IMMEDIATE, check scope + expected_revision + old state
  (active only; conflicted -> CONFLICT_REQUIRES_RESOLVE). Success: old
  becomes superseded, new record created with a supersedes link, descendants
  of the old record are tainted. One epoch bump per scope per change-set.
  Two concurrent corrections on the same old revision: only one wins
  (BEGIN IMMEDIATE serialises writers; the loser sees VERSION_CONFLICT).
- retract(): history kept + reason recorded; record leaves default recall.
  The reason is one of the controlled labels RETRACT_REASONS
  (P0-R6-01) — no free text is ever persisted in the retract event.
  Logical retract is NOT payload erasure (erasure is T038, product-only).
  Revision is never rolled back / reused.
- resolve_conflict(): the FULL current candidate set (ids + revisions) must
  be supplied; any missing/changed candidate rejects the whole transaction
  (CANDIDATE_SET_CHANGED). Success creates one new active record and marks
  all candidates superseded, in one transaction. No model vote substitutes
  for external evidence: without sufficient basis, keep the conflict.
"""
from __future__ import annotations
from .errors import TurnkeepError
from .erasure import (_check_controlled_reason,
                      _check_reason_against_needles, _needles_for_payload,
                      RETRACT_REASONS)
from .idempotency import (request_digest, check as idem_check,
                          store as idem_store)
from .storage import (transaction, ensure_scope, row_or_404, dump_record,
                      insert_record, set_state, taint_descendants,
                      publish_event, _id, _json, _text, _ids, _sources,
                      check_revision, _now)


def correct(db, scope_id, old_id, expected_revision, new_value, *, source_refs,
            parent_ids=(), actor_binding="local", idempotency_key=""):
    """idempotency_key: empty means no idempotency. A non-empty key replays
    the stored response for the same request digest (record, revision,
    new value, sources, parents) and raises IDEMPOTENCY_MISMATCH for the
    same key with a different request — same semantics as finish. The
    check runs first inside the write tx, before the revision check, so
    a replay still returns the original new record after the old record
    was superseded; the store runs in the same tx as the write.
    Erasure ends the replay promise (P0-R10-01): erasing the new
    record neutralizes its request digest, so a replay then raises
    IDEMPOTENCY_MISMATCH instead of returning the stored record."""
    scope_id = _text(scope_id, "scope")
    sources = _sources(source_refs)
    parents = _ids(parent_ids)
    digest = request_digest("correct", scope_id, actor_binding,
                            {"record_id": old_id,
                             "expected_revision": expected_revision,
                             "new_value": new_value, "sources": sources,
                             "parent_ids": parents}) if idempotency_key else None
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        if idempotency_key:
            prior = idem_check(db, scope_id, actor_binding, "correct",
                               idempotency_key, digest)
            if prior is not None:
                return prior
        old = row_or_404(db, scope_id, old_id)
        # Revision FIRST (starter order): a concurrent loser whose record was
        # already superseded gets VERSION_CONFLICT, not INVALID_INPUT.
        check_revision(old, expected_revision)
        if old["state"] == "conflicted":
            raise TurnkeepError("CONFLICT_REQUIRES_RESOLVE",
                                "Correct a conflicted record only via resolve_conflict")
        if old["state"] != "active":
            raise TurnkeepError("INVALID_INPUT",
                                f"Only active records can be corrected (state={old['state']})")
        changes = {}
        # P1-g: ONE event id for the whole change-set; every state change
        # and the new record cite it as their cause.
        ev = _id()
        set_state(db, scope_id, old_id, "superseded", changes, cause_event_id=ev)
        taint_descendants(db, scope_id, [old_id], changes, cause_event_id=ev)
        new_id = insert_record(db, scope_id, old["key"], _json(new_value), old["kind"],
                               sources, parents, changes, state="active",
                               cause_event_id=ev)
        db.execute("INSERT INTO replacements(scope_id, old_id, new_id, reason) VALUES (?,?,?,?)",
                   (scope_id, old_id, new_id, "correct"))
        publish_event(db, scope_id, "correct", actor_binding, changes,
                      metadata={"key": old["key"]}, event_id=ev)
        result = dump_record(db, row_or_404(db, scope_id, new_id))
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "correct",
                       idempotency_key, digest, result)
        return result


def retract(db, scope_id, record_id, expected_revision, reason, *, actor_binding="local",
            idempotency_key=""):
    """idempotency_key: as correct() — a replay returns the stored
    retracted record instead of raising INVALID_INPUT for an already
    retracted record; same key + different request raises
    IDEMPOTENCY_MISMATCH. Erasure ends the replay promise
    (P0-R10-01): erasing the record neutralizes its request digest,
    so a replay then raises IDEMPOTENCY_MISMATCH instead."""
    scope_id = _text(scope_id, "scope")
    reason = _text(reason, "reason")
    digest = request_digest("retract", scope_id, actor_binding,
                            {"record_id": record_id,
                             "expected_revision": expected_revision,
                             "reason": reason}) if idempotency_key else None
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        if idempotency_key:
            prior = idem_check(db, scope_id, actor_binding, "retract",
                               idempotency_key, digest)
            if prior is not None:
                return prior
        row = row_or_404(db, scope_id, record_id)
        check_revision(row, expected_revision)
        if row["state"] == "retracted":
            raise TurnkeepError("INVALID_INPUT", "Record is already retracted")
        # P0-R6-01: the reason is persisted verbatim into the append-only
        # retract event's metadata — the same leak as erase_record's
        # reason. No free text is accepted at all: the reason must be one
        # of the controlled labels RETRACT_REASONS (allowlist first), and
        # the label is then vetted against this record's payload needles
        # as a second layer (a label that IS the payload is rejected).
        # A leaked reason would also make a later erase refuse: the
        # payload would sit in the append-only events table, so step 1
        # of erase_record could never pass.
        _check_controlled_reason(reason, RETRACT_REASONS, "retract")
        _check_reason_against_needles(reason, _needles_for_payload(row["value_json"]))
        changes = {}
        ev = _id()
        set_state(db, scope_id, record_id, "retracted", changes, cause_event_id=ev)
        taint_descendants(db, scope_id, [record_id], changes, cause_event_id=ev)
        publish_event(db, scope_id, "retract", actor_binding, changes,
                      metadata={"key": row["key"], "reason": reason}, event_id=ev)
        result = dump_record(db, row_or_404(db, scope_id, record_id))
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "retract",
                       idempotency_key, digest, result)
        return result


def resolve_conflict(db, scope_id, key, expected_candidates, new_value, *, source_refs,
                     parent_ids=(), actor_binding="local", idempotency_key=""):
    """expected_candidates: [{id, revision}, ...] — must be the COMPLETE
    current conflicted set for (scope, key).

    idempotency_key: as correct() — a replay returns the stored resolved
    record instead of raising CANDIDATE_SET_CHANGED (the candidates are
    superseded by then); same key + different request raises
    IDEMPOTENCY_MISMATCH. Erasure ends the replay promise
    (P0-R10-01): erasing the resolved record neutralizes its request
    digest, so a replay then raises IDEMPOTENCY_MISMATCH instead."""
    scope_id, key = _text(scope_id, "scope"), _text(key, "key")
    sources = _sources(source_refs)
    parents = _ids(parent_ids)
    if not isinstance(expected_candidates, (list, tuple)) or not expected_candidates:
        raise TurnkeepError("INVALID_INPUT", "resolve_conflict requires the full candidate set")
    supplied = {c["id"]: c["revision"] for c in expected_candidates}
    if len(supplied) != len(expected_candidates):
        raise TurnkeepError("INVALID_INPUT", "duplicate candidate ids")
    digest = request_digest("resolve_conflict", scope_id, actor_binding,
                            {"key": key,
                             "expected_candidates": [{"id": i, "revision": supplied[i]}
                                                     for i in sorted(supplied)],
                             "new_value": new_value, "sources": sources,
                             "parent_ids": parents}) if idempotency_key else None
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        if idempotency_key:
            prior = idem_check(db, scope_id, actor_binding, "resolve_conflict",
                               idempotency_key, digest)
            if prior is not None:
                return prior
        current = db.execute(
            "SELECT * FROM memories WHERE scope_id=? AND key=? AND state='conflicted' ORDER BY id",
            (scope_id, key)).fetchall()
        current_map = {r["id"]: r["revision"] for r in current}
        if set(current_map) != set(supplied) or any(current_map[i] != supplied[i] for i in supplied):
            raise TurnkeepError("CANDIDATE_SET_CHANGED",
                                "Candidate set changed since it was read; re-read and retry")
        if not current:
            raise TurnkeepError("INVALID_INPUT", "No conflict to resolve for this key")
        kind = current[0]["kind"]
        changes = {}
        ev = _id()
        for row in current:
            set_state(db, scope_id, row["id"], "superseded", changes, cause_event_id=ev)
        # Superseding the candidates taints their descendants, as correct()
        # does; in practice they are already tainted from conflict
        # formation, so this is normally a no-op — kept explicit so a
        # resolution can never leave a usable descendant behind.
        taint_descendants(db, scope_id, [r["id"] for r in current], changes,
                          cause_event_id=ev)
        new_id = insert_record(db, scope_id, key, _json(new_value), kind,
                               sources, parents, changes, state="active",
                               cause_event_id=ev)
        for row in current:
            db.execute("INSERT INTO replacements(scope_id, old_id, new_id, reason) VALUES (?,?,?,?)",
                       (scope_id, row["id"], new_id, "resolve_conflict"))
        publish_event(db, scope_id, "resolve_conflict", actor_binding, changes,
                      metadata={"key": key}, event_id=ev)
        result = dump_record(db, row_or_404(db, scope_id, new_id))
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "resolve_conflict",
                       idempotency_key, digest, result)
        return result
