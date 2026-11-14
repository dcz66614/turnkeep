"""Resume, diff and bounded rebase (T015).

- resume(): returns the old snapshot -> current diff (changed_keys,
  changed_record_ids, per-record state changes, taint paths) plus a NEW
  candidate snapshot. It NEVER turns the old result valid again.
- rebase(): re-read + recompute is the caller's job; this helper records a
  rebase attempt (max 2 automatic attempts per run, tracked in
  runs.snapshot_json metadata via a rebase_count in the receipt area —
  stored in the runs row's metadata through a dedicated column is not
  available, so attempts are counted from events with metadata.run_id).
  When attempts are exhausted, the candidate result is quarantined:
  recorded as a tainted handoff record that default recall will not return,
  and the caller is told to open an independent task.
  The candidate result is the caller's own recompute: after a rebase the
  caller works in the new run, re-reads and recomputes, and hands the
  resulting {"key": ..., "value": ...} (the result it would finish with)
  back to the NEXT rebase() call as candidate_result=. A candidate passed
  on a non-exhausted call is validated but NOT persisted — it is still
  live work, superseded by the recompute in the new run; only the
  exhausted call quarantines it. The independent task is a real follow-up
  run (begin(), task "quarantine-followup-<run_id>"), opened once on
  first exhaustion and recorded in the quarantine value, so the operator
  has a concrete run to continue in instead of the exhausted one. It is
  opened AFTER the rebase_exhausted event is published (P1-R4-03): the
  run is born at the post-event epoch, so finish() on it succeeds with
  no other writes. If begin() fails after the quarantine transaction
  commits, the quarantine names a run id that does not exist yet; one
  retry is attempted, and a later exhausted call re-reads the stored
  followup_run_id and retries the begin then.
  get_quarantine() is the read path for the quarantined candidate +
  next action after exhaustion (T015 acceptance invariant).
- A rebase never just edits the old run's epoch.
- Epoch bump on rebase is BY DESIGN, not pollution: rebase_attempts()
  counts 'rebase' events, so a rebase that published no event could not
  be counted. Every rebase event — and the single rebase_exhausted event
  published on the FIRST exhaustion — bumps the scope epoch once via
  publish_event(), so after any rebase the old run's snapshot epoch is
  stale and its finish() raises the stale-snapshot error — the caller
  must work from the new run instead. Do not remove the event or the
  bump to "fix" the epoch. A REPEATED exhaustion is different, and the
  repeat kind matters (P1-R6-02, CORE_CONTRACT lines 39 and 57):
  an IDENTICAL repeat — the same candidate by canonical _json equality,
  or no candidate when one is already stored (or none was ever stored)
  — is a true no-op: it touches nothing (no UPDATE, no revision bump,
  no updated_at change, no event, no epoch bump), so the follow-up run
  born at the post-first-event epoch stays finishable after any number
  of identical repeats. A CANDIDATE UPDATE — a candidate that differs
  from the stored one — is a memory content change: it updates the
  candidate fields AND publishes one new 'quarantine_updated' event
  (metadata run_id + candidate revision), which bumps the scope epoch
  once via publish_event(). It publishes NO second rebase_exhausted
  event (exactly-one invariant). The follow-up run therefore goes stale
  after a candidate update, BY DESIGN like any scope change: the
  operator rebases the follow-up run and finishes there.
- to_epoch TRACKS the latest quarantine write: the scope epoch after
  the most recent candidate update — the first exhaustion's
  post-rebase_exhausted epoch, or the post-quarantine_updated epoch
  after a candidate update. It names the epoch the quarantine state
  (candidate) is current as of, so a reader comparing it with the
  current scope epoch can tell whether the candidate is fresh. An
  identical repeat leaves to_epoch untouched (nothing was written).
- No expected-candidate-revision layer on rebase(): the epoch bump on
  a candidate update is the protection against a stale worker
  finishing an old candidate (a worker that read v1 finishes against
  the follow-up run's snapshot epoch, which the bump makes stale).
  An expected-revision check on the rebase() call alone would NOT
  close that repro — worker A never calls rebase() again — so it is
  deliberately not added; the API surface is unchanged.
- When attempts are exhausted, exactly ONE quarantine record exists per
  run (upsert by key rebase-quarantine-<run_id>, with the lookup inside
  the write transaction so concurrent exhaustions serialise on it —
  P1-R4-05), exactly ONE rebase_exhausted event exists per run
  (published on first exhaustion only; repeats publish none), and
  exactly ONE follow-up run exists per run (created on first exhaustion
  only, under idempotency key quarantine-followup-<run_id>; repeated
  exhaustion reuses it). When a candidate result was supplied, the
  quarantine value holds it verbatim with candidate_result_saved=True;
  when none was ever supplied, the value says so explicitly instead of
  claiming a save: candidate_result_saved=False and candidate_result=None.
  A later exhausted call with a NEWER candidate replaces the stored one
  and publishes one 'quarantine_updated' event (epoch +1, to_epoch
  tracks the update); a later exhausted call with NO candidate preserves
  the already-saved candidate (it must never clobber it back to None)
  and — the candidate being unchanged — is a true no-op that touches
  nothing. A candidate update changes ONLY the candidate fields
  (candidate_result, candidate_result_saved, candidate_result_note,
  next_action) plus to_epoch: it never touches cause_event_id (still
  the first event's id, per P1-g and test_cause_event_id.py:170),
  from_epoch, attempts, task or followup_run_id. An identical repeat
  updates NOTHING at all — not even those candidate fields.
"""
from __future__ import annotations
import json
from .errors import TurnkeepError
from .storage import scope_epoch, row_or_404, dump_record, _text
from .runs import get_run, begin
from .memory import active_records, list_conflicts
from .lineage import impact


def diff_snapshot(db, scope_id, run_id):
    run = get_run(db, scope_id, run_id)
    snap = run["snapshot"]
    current = active_records(db, scope_id, include_history=True)
    cur_map = {r["id"]: r for r in current}
    changed, added, removed = [], [], []
    for s in snap["records"]:
        c = cur_map.get(s["id"])
        if c is None:
            removed.append(s["id"])
        elif c["revision"] != s["revision"] or c["state"] != s["state"]:
            changed.append({"id": s["id"], "key": s["key"],
                            "from_state": s["state"], "to_state": c["state"],
                            "from_revision": s["revision"], "to_revision": c["revision"]})
    snap_ids = {s["id"] for s in snap["records"]}
    for c in current:
        if c["id"] not in snap_ids and c["state"] == "active":
            added.append({"id": c["id"], "key": c["key"]})
    changed_keys = sorted({x["key"] for x in changed} | {x["key"] for x in added})
    return {"run_id": run_id, "scope": scope_id,
            "from_epoch": run["snapshot_epoch"], "to_epoch": scope_epoch(db, scope_id),
            "changed": changed, "added": added, "removed": removed,
            "changed_keys": changed_keys,
            "changed_record_ids": sorted({x["id"] for x in changed} | {x["id"] for x in added} | set(removed))}


def resume(db, scope_id, run_id):
    """View-only diff + a fresh candidate snapshot. Old result stays as-is."""
    d = diff_snapshot(db, scope_id, run_id)
    candidate = begin(db, scope_id, f"resume-of-{run_id}")
    d["candidate_snapshot"] = {"run_id": candidate["run_id"],
                               "read_epoch": candidate["read_epoch"],
                               "record_ids": [r["id"] for r in candidate["records"]]}
    d["old_result_still_valid"] = False
    return d


def rebase_attempts(db, scope_id, run_id):
    rows = db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id=? AND event_type='rebase' "
        "AND json_extract(metadata_json, '$.run_id')=?", (scope_id, run_id)).fetchone()
    return rows[0]


def _validate_candidate(candidate_result):
    """A candidate result is the caller's recompute in finish() shape:
    {"key": <nonempty str>, "value": <any JSON value>}. Returns the
    candidate unchanged, or None. Raises INVALID_INPUT on any other
    shape — a malformed candidate must fail loudly, never be stored
    half-understood in the quarantine record."""
    if candidate_result is None:
        return None
    if not isinstance(candidate_result, dict):
        raise TurnkeepError("INVALID_INPUT",
                            "candidate_result must be a {\"key\": ..., \"value\": ...} result dict")
    if "value" not in candidate_result:
        raise TurnkeepError("INVALID_INPUT",
                            "candidate_result must carry a \"value\" (the recomputed result value)")
    _text(candidate_result.get("key"), "candidate_result.key")
    return candidate_result


def get_quarantine(db, scope_id, run_id):
    """Read path for the T015 acceptance invariant: after rebase is
    exhausted, return the quarantined candidate + next action for the
    run, or None when nothing was ever quarantined. The record is
    state='tainted', so default recall() / search_by_key() never return
    it — this explicit read is how it is checked."""
    from .storage import scope_epoch
    scope_epoch(db, scope_id)  # NOT_FOUND for an unknown scope
    row = db.execute(
        "SELECT id, key, kind, state, value_json FROM memories "
        "WHERE scope_id=? AND key=?",
        (scope_id, f"rebase-quarantine-{run_id}")).fetchone()
    if row is None:
        return None
    value = json.loads(row["value_json"])
    return {"record_id": row["id"], "key": row["key"], "kind": row["kind"],
            "state": row["state"], "run_id": value.get("run_id"),
            "reason": value.get("reason"), "attempts": value.get("attempts"),
            "from_epoch": value.get("from_epoch"),
            "to_epoch": value.get("to_epoch"),
            "candidate_result": value.get("candidate_result"),
            "candidate_result_saved": value.get("candidate_result_saved"),
            "candidate_result_note": value.get("candidate_result_note"),
            "task": value.get("task"),
            "followup_run_id": value.get("followup_run_id"),
            "next_action": value.get("next_action")}


def rebase(db, scope_id, run_id, *, actor_binding="local", candidate_result=None):
    """Record one rebase attempt (max 2 automatic). Returns the new run to
    work from, or a quarantine notice when attempts are exhausted.

    candidate_result: the caller's recompute from the latest rebased run,
    in finish() shape {"key": ..., "value": ...} — the result it would
    finish with. Validated on every call; persisted ONLY on exhaustion,
    into the quarantine record (state='tainted', never default-recalled).
    On a non-exhausted call the candidate is still the caller's live
    work and is not stored: the caller recomputes again in the new run.

    Epoch note (by design): each rebase publishes one 'rebase' event and
    publish_event() bumps the scope epoch once per event, because
    rebase_attempts() counts those events. A plain/no-op rebase therefore
    still advances the epoch and leaves the old run's snapshot stale —
    that is how attempts are accounted for, not epoch pollution.
    Likewise the FIRST exhausted call publishes one 'rebase_exhausted'
    event (so the epoch advances once). A REPEATED exhausted call is
    either a true no-op or a content change (P1-R6-02): an identical
    repeat (same candidate by canonical _json equality, or no candidate
    with one already stored) publishes NO event, bumps nothing and
    touches nothing, so the follow-up run stays finishable; a candidate
    update publishes one 'quarantine_updated' event (metadata run_id +
    candidate revision) and bumps the epoch once, so the follow-up run
    goes stale and must itself be rebased before finishing. The
    quarantine *record* is upserted by key, so there is exactly one per
    run, and exactly one rebase_exhausted event. to_epoch TRACKS the
    latest quarantine write (first exhaustion or latest candidate
    update): the scope epoch the stored candidate is current as of.
    """
    from .storage import transaction, publish_event, insert_record, _id, _json, _now
    candidate_result = _validate_candidate(candidate_result)
    run = get_run(db, scope_id, run_id)
    attempts = rebase_attempts(db, scope_id, run_id)
    if attempts >= 2:
        # P1-R2-01 / T015: persist the quarantine in ONE transaction: a
        # tainted handoff record (no snapshot parents attached — they may
        # not be usable) plus, on FIRST exhaustion only, a
        # rebase_exhausted event. No "rebase" event is published on this
        # path, so rebase_attempts() stays at 2.
        # P1-g: the handoff record's cause is the rebase_exhausted
        # event id. Repeated exhaustion upserts the same key instead of
        # inserting a new row. The follow-up run is created AFTER this
        # transaction (P1-R4-03), with the id pre-generated here, so its
        # snapshot is taken at the post-event epoch.
        # P1-R6-02 (supersedes the P1-R5-02 "repeat = replay" rule, which
        # blurred "nothing changed" with "the candidate changed"): a
        # REPEATED exhaustion is either a true no-op or a content change.
        # - Identical replay (same candidate by canonical _json equality,
        #   or no candidate with one already stored — including none ever
        #   stored): return the stored response WITHOUT touching the
        #   quarantine row (no UPDATE, no revision bump, no updated_at
        #   change), publish NO event, bump NO epoch. Per CORE_CONTRACT
        #   line 39, a content-identical idempotent replay does not
        #   increment. The note/next_action are deterministic functions
        #   of the candidate + followup_run_id, so skipping the rewrite
        #   changes nothing observable.
        # - Candidate update (a candidate that differs from the stored
        #   one): a memory content change — update the candidate fields
        #   AND publish one NEW 'quarantine_updated' event in the same
        #   transaction (publish_event is the only epoch-bump path), so
        #   the epoch advances once per CORE_CONTRACT line 57. Do NOT
        #   publish a second rebase_exhausted (exactly-one invariant),
        #   and do NOT touch cause_event_id (keeps the first event's id,
        #   test_cause_event_id.py:170). to_epoch is refreshed to the
        #   post-update epoch: it TRACKS the latest quarantine write.
        #   Consequence, by design: the follow-up run goes stale after
        #   a candidate update (like any scope change); the operator
        #   rebases the follow-up run and finishes there.
        # - No expected-candidate-revision layer is added to rebase():
        #   it alone would NOT close GPT's two-worker repro (worker A
        #   never calls rebase() again); the epoch bump does.
        # The old code updated the row unconditionally on every repeat
        # (revision 1 -> 2 -> 3 -> 4 while the epoch stayed frozen) and
        # published no event on a candidate change, so a worker that
        # read v1 could still finish(v1) after another worker updated
        # the candidate to v2. Nothing consumes rebase_exhausted events
        # except the P1-g cause check, and rebase_attempts() counts
        # 'rebase' events only, so the new event type breaks no counter.
        #
        # Candidate handling (T015): the supplied candidate — if any —
        # is the freshest recompute and replaces the stored one; a call
        # with no candidate preserves an already-saved candidate and
        # never clobbers it back to None. The independent task is a
        # follow-up run, opened once on first exhaustion and reused
        # afterwards (begin() publishes no event, so opening it does
        # not bump the epoch or touch rebase_attempts()).
        key = f"rebase-quarantine-{run_id}"
        task = f"quarantine-followup-{run_id}"
        idem_key = task
        # P1-R4-03: the follow-up run must be born at the CURRENT epoch,
        # i.e. AFTER the rebase_exhausted event bumps it. Its id is
        # pre-generated, the quarantine record + the event are written in
        # ONE transaction naming that id, and begin(run_id=...) is called
        # only after the transaction commits — so the run's snapshot is
        # taken at the post-event epoch and finish() on it succeeds with
        # no other writes. Window: if begin() fails after the transaction
        # commits, the quarantine names a run that does not exist yet.
        # Mitigation: one retry here (begin is transactional and
        # idempotent under idem_key, so a retry is safe), and a later
        # exhausted call re-reads the stored followup_run_id and retries
        # the begin then — the quarantine self-heals on the next call.
        # P1-R4-05: the quarantine lookup lives INSIDE the write
        # transaction (re-read after BEGIN IMMEDIATE acquires the lock):
        # a second writer blocks, then sees the first writer's committed
        # record and reuses its followup_run_id instead of inserting a
        # duplicate. The follow-up run itself is created with idempotency
        # key f"quarantine-followup-{run_id}", so concurrent begins with
        # the same key serialise and only one run is made.
        pre_generated_id = _id()
        followup_run_id = None
        record_id = None
        with transaction(db, write=True):
            existing = db.execute(
                "SELECT id, value_json, revision FROM memories WHERE scope_id=? AND key=?",
                (scope_id, key)).fetchone()
            prior = json.loads(existing["value_json"]) if existing is not None else {}
            saved = candidate_result if candidate_result is not None else prior.get("candidate_result")
            followup_run_id = prior.get("followup_run_id") or pre_generated_id
            saved_flag = saved is not None
            if saved_flag:
                note = ("Candidate result supplied by the caller was saved in this "
                        "quarantine record as not-default-recallable state (tainted); "
                        "review it in the independent task run before reusing it.")
                next_action = (f"Review the quarantined candidate result (key "
                               f"'{saved['key']}') in the independent task run "
                               f"{followup_run_id} ('{task}'): re-read from that run, "
                               f"recompute, and finish there. Do not call rebase() on "
                               f"run {run_id} again — automatic rebase is exhausted.")
                reason = (f"Automatic rebase attempts exhausted (2). Candidate result "
                          f"(key '{saved['key']}') saved as quarantined (not "
                          f"default-recallable); continue in independent task run "
                          f"{followup_run_id}.")
            else:
                note = ("No candidate result was produced or saved by rebase(); "
                        "re-read and recompute from the new run instead.")
                next_action = (f"No candidate result was produced or saved; re-read and "
                               f"recompute from the independent task run "
                               f"{followup_run_id} ('{task}') and finish there. Do not "
                               f"call rebase() on run {run_id} again — automatic "
                               f"rebase is exhausted.")
                reason = ("Automatic rebase attempts exhausted (2). No candidate result "
                          "was produced or saved; open an independent task to continue.")
            value = {"run_id": run_id, "reason": "rebase attempts exhausted",
                     "attempts": attempts,
                     "from_epoch": run["snapshot_epoch"],
                     # The epoch AFTER the rebase_exhausted event this
                     # transaction is about to publish on FIRST exhaustion
                     # (publish_event bumps it by exactly one). On an
                     # identical repeat there is no event and the stored
                     # to_epoch is kept as-is; on a candidate update the
                     # branch below refreshes it to the post-
                     # quarantine_updated epoch (see the branch below) and
                     # this first-exhaustion value is not rebuilt.
                     "to_epoch": scope_epoch(db, scope_id) + 1,
                     "candidate_result": saved,
                     "candidate_result_saved": saved_flag,
                     "candidate_result_note": note,
                     "task": task,
                     "followup_run_id": followup_run_id,
                     "next_action": next_action}
            changes = {}
            if existing is None:
                # FIRST exhaustion: insert the record citing the new
                # event, then publish that one event.
                ev = _id()
                record_id = insert_record(
                    db, scope_id, key, _json(value),
                    "handoff", [{"id": f"run:{run_id}", "type": "agent_inference"}],
                    (), changes, state="tainted", cause_event_id=ev)
                publish_event(db, scope_id, "rebase_exhausted", actor_binding, changes,
                              metadata={"run_id": run_id, "attempts": attempts},
                              event_id=ev)
            else:
                # REPEAT exhaustion (P1-R6-02): identical replay or
                # candidate update — never a second rebase_exhausted.
                record_id = existing[0]
                # Canonical _json equality (_json is canonical:
                # sort_keys, fixed separators — storage.py:69). `saved`
                # is the candidate that would be stored (the supplied
                # one, or the already-stored one when none is supplied),
                # so this is True exactly when the stored candidate
                # would not change — including no-candidate with none
                # ever stored.
                if _json(saved) == _json(prior.get("candidate_result")):
                    # Identical replay: TRUE NO-OP. Touch nothing — no
                    # UPDATE, no revision bump, no updated_at change, no
                    # event, no epoch bump. The stored response (reason
                    # / next_action, deterministic functions of the
                    # candidate + followup_run_id) is returned as-is
                    # below; begin()'s idempotent replay after the
                    # transaction still runs as today.
                    pass
                else:
                    # Candidate update: a content change. Update the
                    # candidate fields AND publish one new
                    # 'quarantine_updated' event in this same
                    # transaction (publish_event is the only epoch-bump
                    # path); to_epoch tracks the post-event epoch.
                    # cause_event_id is deliberately NOT touched — it
                    # stays the first rebase_exhausted event's id
                    # (test_cause_event_id.py:170). The follow-up run
                    # goes stale by design; the operator rebases it.
                    candidate_revision = existing["revision"] + 1
                    new_to_epoch = scope_epoch(db, scope_id) + 1
                    replay_value = dict(prior)
                    replay_value.update({
                        "candidate_result": saved,
                        "candidate_result_saved": saved_flag,
                        "candidate_result_note": note,
                        "next_action": next_action,
                        "to_epoch": new_to_epoch})
                    db.execute(
                        "UPDATE memories SET value_json=?, updated_at=?, revision=revision+1 "
                        "WHERE scope_id=? AND id=?",
                        (_json(replay_value), _now(), scope_id, record_id))
                    bumped = publish_event(
                        db, scope_id, "quarantine_updated", actor_binding,
                        {record_id: None},
                        metadata={"run_id": run_id,
                                  "candidate_revision": candidate_revision},
                        event_id=_id())
                    if bumped != new_to_epoch:
                        # Internal invariant: publish_event bumps the
                        # epoch by exactly one in this transaction, so
                        # the stored to_epoch must equal the new epoch.
                        raise RuntimeError(
                            f"quarantine_updated bumped epoch to {bumped}, "
                            f"expected {new_to_epoch}")
        # begin() AFTER the transaction commits, so the follow-up run's
        # snapshot epoch is the post-event epoch (P1-R4-03). One retry on
        # failure; if both attempts fail the exception propagates and the
        # quarantine names a run id that a later exhausted call will
        # create on retry (see the window note above).
        followup = None
        last_exc = None
        for _ in range(2):
            try:
                followup = begin(db, scope_id, task, actor_binding=actor_binding,
                                 idempotency_key=idem_key, run_id=followup_run_id)
                break
            except Exception as exc:  # noqa: BLE001 - one retry, then propagate
                last_exc = exc
        if followup is None:
            raise last_exc
        followup_run_id = followup["run_id"]
        return {"status": "quarantined",
                "reason": reason,
                "run_id": run_id, "attempts": attempts, "record_id": record_id,
                "candidate_result_saved": saved_flag,
                "followup_run_id": followup_run_id, "task": task,
                "next_action": next_action}
    with transaction(db, write=True):
        publish_event(db, scope_id, "rebase", actor_binding, {run_id: None},
                      metadata={"run_id": run_id, "attempt": attempts + 1},
                      event_id=_id())
    new_run = begin(db, scope_id, f"rebase-{attempts + 1}-of-{run_id}")
    return {"status": "rebased", "run_id": new_run["run_id"],
            "read_epoch": new_run["read_epoch"], "attempts": attempts + 1,
            "diff": diff_snapshot(db, scope_id, run_id)}
