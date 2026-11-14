"""Atomic finish + stale-snapshot rejection (T014). Contract §6, five steps,
in ONE BEGIN IMMEDIATE transaction:

1. Idempotency FIRST: if this run already succeeded, same request digest ->
   return the original receipt; different digest -> IDEMPOTENCY_MISMATCH.
   (Replay precedence over closed-state and epoch checks.) Erasure ends
   the replay promise (P0-R10-01): erasing the finished record
   neutralizes its request digest (and the run row's copy), so a replay
   then raises IDEMPOTENCY_MISMATCH instead of returning the receipt.
2. Read the server-bound run + scope; confirm it can still commit.
3. Check current epoch == run's read epoch, else STALE_SNAPSHOT with
   changed_keys / changed_record_ids / why (coarse-grain: epoch diff).
4. Confirm all declared parents are still usable and in-scope.
5. Write result + event + idempotency row, bump epoch, close run, store receipt.

- The server merges its snapshot read-set into the result's parent_ids
  (union with client-declared parents): omitting parents cannot dodge later
  taint. An empty client parent list still inherits the snapshot set.
- No LLM / network call inside the write transaction.
- One run yields at most one result; external actions are NOT atomic with
  the receipt (contract §7 — never claimed).
"""
from __future__ import annotations
import json
from .errors import TurnkeepError
from .storage import (transaction, ensure_scope, scope_epoch, row_or_404,
                      dump_record, insert_record, publish_event, _id, _json,
                      _text, _ids, _sources, _now, usable)
from .idempotency import (request_digest, legacy_request_digest,
                          check_any as idem_check_any, store as idem_store)
from .runs import get_run
from .memory import active_records


def _changed_since(db, scope_id, run):
    snap_ids = {r["id"] for r in run["snapshot"]["records"]}
    now = db.execute("SELECT id, key, state, revision FROM memories WHERE scope_id=?",
                     (scope_id,)).fetchall()
    changed_ids, changed_keys = [], []
    now_map = {r["id"]: r for r in now}
    for r in now:
        old = next((s for s in run["snapshot"]["records"] if s["id"] == r["id"]), None)
        if old is None or old["revision"] != r["revision"] or old["state"] != r["state"]:
            changed_ids.append(r["id"])
            if r["key"] not in changed_keys:
                changed_keys.append(r["key"])
    for s in run["snapshot"]["records"]:
        if s["id"] not in now_map:
            changed_ids.append(s["id"])
    return sorted(set(changed_ids)), sorted(changed_keys)


def finish(db, scope_id, run_id, result, *, parent_ids=(), idempotency_key="",
           actor_binding="local"):
    scope_id = _text(scope_id, "scope")
    if not isinstance(result, dict) or "key" not in result or "value" not in result:
        raise TurnkeepError("INVALID_INPUT", "finish result needs {key, value}")
    parents = _ids(parent_ids)
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        run = get_run(db, scope_id, run_id)
        # Server read-set is merged in BEFORE the digest: the client cannot
        # omit snapshot parents, and the digest covers the merged parent set
        # (sorted), so a replay with a different parent set is a different
        # request. (Usability checks stay in step 4 below.)
        snapshot_ids = [r["id"] for r in run["snapshot"]["records"]]
        all_parents = sorted(set(parents) | set(snapshot_ids))
        digest = request_digest("finish", scope_id, actor_binding,
                                {"run_id": run_id, "result": result,
                                 "parent_ids": all_parents})
        # Legacy v1 digest (run_id + result only, no parent_ids) for runs
        # accepted before the v2 change: replay accepts either digest.
        legacy_digest = legacy_request_digest(
            "finish", scope_id, actor_binding, {"run_id": run_id, "result": result})
        # Same key, different request -> refuse before any work, even on a
        # run that has not succeeded yet.
        if idempotency_key:
            prior = idem_check_any(db, scope_id, actor_binding, "finish",
                                   idempotency_key, (digest, legacy_digest))
            if prior is not None:
                return prior
        # 1. Idempotent replay precedes closed/epoch checks.
        if run["status"] == "accepted" and run.get("receipt"):
            stored = idem_check_any(db, scope_id, actor_binding, "finish",
                                    idempotency_key or run_id, (digest, legacy_digest))
            if stored is not None:
                return stored
            # accepted but no idempotency row under this key: same digest via
            # run-bound digest check (v2 or legacy v1)
            if run.get("request_digest") in (digest, legacy_digest):
                return run["receipt"]
            raise TurnkeepError("IDEMPOTENCY_MISMATCH",
                                "Run already accepted with a different request")
        if run["status"] != "open":
            raise TurnkeepError("INVALID_INPUT", f"Run is {run['status']}, cannot finish")
        # 3. Epoch check (before any write).
        current_epoch = scope_epoch(db, scope_id)
        if current_epoch != run["snapshot_epoch"]:
            changed_ids, changed_keys = _changed_since(db, scope_id, run)
            raise TurnkeepError("STALE_SNAPSHOT",
                                "Project memory changed after this run read its snapshot; reread changes and recompute affected work.",
                                changed_record_ids=changed_ids, changed_keys=changed_keys,
                                from_epoch=run["snapshot_epoch"], to_epoch=current_epoch)
        # 4. Declared parents must still be usable + in scope.
        for p in parents:
            if not usable(db, scope_id, p):
                raise TurnkeepError("INVALID_PARENT",
                                    "Declared parent is missing or unavailable", parent_id=p)
        # Snapshot parents must still be usable too (all_parents was merged
        # in before the digest, above).
        for p in all_parents:
            if not usable(db, scope_id, p):
                raise TurnkeepError("INVALID_PARENT",
                                    "Snapshot parent is no longer usable", parent_id=p)
        # 5. Write result + event + receipt + idempotency, in this tx.
        changes = {}
        # P1-g: ONE event id for the whole finish; the result record cites it.
        ev = _id()
        sources = [{"id": f"run:{run_id}", "type": "agent_inference"}]
        result_id = insert_record(db, scope_id, result["key"], _json(result["value"]),
                                  result.get("kind", "handoff"), sources, all_parents,
                                  changes, state="active", cause_event_id=ev)
        receipt = {"run_id": run_id, "scope": scope_id, "record_id": result_id,
                   "request_digest": digest, "read_epoch": run["snapshot_epoch"],
                   "commit_epoch": current_epoch + 1, "parent_ids": all_parents,
                   "committed_at": _now(), "status_at_commit": "active",
                   "guarantee": "local_sqlite_commit_only"}
        db.execute("UPDATE runs SET status='accepted', accepted_at=?, result_memory_id=?, "
                   "request_digest=?, receipt_json=? WHERE scope_id=? AND id=?",
                   (receipt["committed_at"], result_id, digest, _json(receipt), scope_id, run_id))
        publish_event(db, scope_id, "finish", actor_binding, changes,
                      metadata={"key": result["key"], "run_id": run_id}, event_id=ev)
        if idempotency_key:
            idem_store(db, scope_id, actor_binding, "finish", idempotency_key, digest, receipt)
        return receipt
