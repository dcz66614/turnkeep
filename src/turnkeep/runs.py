"""Consistent snapshot begin + server-side run (T013).

- begin(): ONE read transaction reads scope_epoch + current active records +
  conflict summary, then stores the run server-side (runs + observed_reads)
  and ENDS the read transaction before returning — no write lock is held
  during model inference / network calls.
- The snapshot is the server's record, not the client's claim: resume/finish
  re-read the stored run; a client-supplied scope/epoch can never change it.
- Empty reads are recorded too: a run that read nothing still stores the
  epoch, so a later new fact invalidates it (phantom protection).
- Runs survive restart (persisted in SQLite); another scope's writes do not
  affect this run's epoch.
"""
from __future__ import annotations
import json
from .errors import TurnkeepError
from .storage import (transaction, ensure_scope, scope_epoch, dump_record,
                      publish_event, _json, _text, _id, _now)
from .idempotency import (request_digest, check as idem_check,
                          store as idem_store)
from .memory import active_records, list_conflicts


def begin(db, scope_id, task, *, actor_binding="local", idempotency_key="",
          run_id=None):
    """idempotency_key: empty means no idempotency. A non-empty key replays
    the stored begin response (same run_id) for the same task digest and
    raises IDEMPOTENCY_MISMATCH for the same key with a different task —
    same store/lookup semantics as finish. The check + store both run
    inside the write tx that inserts the run, so concurrent begins with
    the same key serialise on BEGIN IMMEDIATE and only one run is made.

    run_id: when given (recovery.rebase's pre-generated follow-up id),
    the run row uses it instead of a fresh id, so a quarantine record
    written in an earlier transaction can name the run before it
    exists. A replay under the idempotency key still returns the stored
    response (same run_id) rather than inserting a second row.
    Erasure ends the replay promise (P0-R10-01): erasing any record
    in the begin receipt neutralizes its request digest, so a replay
    then raises IDEMPOTENCY_MISMATCH instead of returning the stored
    response."""
    scope_id, task = _text(scope_id, "scope"), _text(task, "task")
    digest = request_digest("begin", scope_id, actor_binding,
                            {"task": task}) if idempotency_key else None
    with transaction(db, write=False):
        ensure_scope(db, scope_id)
        epoch = scope_epoch(db, scope_id)
        records = active_records(db, scope_id)
        conflicts = list_conflicts(db, scope_id)
    run_id = _text(run_id, "run_id") if run_id is not None else _id()
    snapshot = {"records": [{"id": r["id"], "key": r["key"], "revision": r["revision"],
                             "state": r["state"]} for r in records],
                "conflicts": [{"id": r["id"], "key": r["key"], "revision": r["revision"]}
                              for r in conflicts]}
    with transaction(db, write=True):
        if idempotency_key:
            prior = idem_check(db, scope_id, actor_binding, "begin",
                               idempotency_key, digest)
            if prior is not None:
                return prior
        db.execute("INSERT INTO runs(id, scope_id, principal_binding, task, snapshot_epoch, snapshot_json, status, created_at) "
                   "VALUES (?,?,?,?,?,?, 'open', ?)",
                   (run_id, scope_id, actor_binding, task, epoch, _json(snapshot), _now()))
        for r in records:
            db.execute("INSERT INTO observed_reads(scope_id, run_id, memory_id, observed_revision) VALUES (?,?,?,?)",
                       (scope_id, run_id, r["id"], r["revision"]))
        result = {"run_id": run_id, "scope": scope_id, "task": task,
                  "read_epoch": epoch, "records": records,
                  "conflicts": conflicts, "status": "open"}
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "begin",
                       idempotency_key, digest, result)
        return result


def get_run(db, scope_id, run_id):
    row = db.execute("SELECT * FROM runs WHERE scope_id=? AND id=?", (scope_id, run_id)).fetchone()
    if row is None:
        raise TurnkeepError("NOT_FOUND", "Unknown run in this scope", run_id=run_id)
    d = dict(row)
    d["snapshot"] = json.loads(d.pop("snapshot_json"))
    d["receipt"] = json.loads(d["receipt_json"]) if d.get("receipt_json") else None
    return d
