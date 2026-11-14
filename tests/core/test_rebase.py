"""T015 rebase tests (separate file per backlog owned_paths)."""
import json

import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin, get_run
from turnkeep.recovery import rebase, rebase_attempts, get_quarantine
from turnkeep.retrieval import recall, search_by_key
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_rebase_never_edits_old_epoch(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    old_epoch = run["read_epoch"]
    out = rebase(db, "s1", run["run_id"])
    from turnkeep.runs import get_run
    assert get_run(db, "s1", run["run_id"])["snapshot_epoch"] == old_epoch
    assert out["run_id"] != run["run_id"]
    assert rebase_attempts(db, "s1", run["run_id"]) == 1
    db.close()


def test_rebase_exhaustion_persists_quarantine(tmp_path):
    # P1-b: the third rebase call (attempts already 2) persists the
    # quarantine instead of only returning a dict: a tainted handoff record
    # exists in the DB, a rebase_exhausted event exists, and the 'rebase'
    # event count stays at 2.
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    out = rebase(db, "s1", run["run_id"])
    assert out["status"] == "quarantined"
    assert out["run_id"] == run["run_id"]
    assert out["attempts"] == 2
    assert out["record_id"]
    row = db.execute(
        "SELECT key, kind, state, value_json FROM memories WHERE scope_id='s1' AND id=?",
        (out["record_id"],)).fetchone()
    assert row is not None
    assert row["kind"] == "handoff"
    assert row["state"] == "tainted"
    value = json.loads(row["value_json"])
    assert value["run_id"] == run["run_id"]
    assert value["reason"] == "rebase attempts exhausted"
    assert value["attempts"] == 2
    assert value["from_epoch"] == run["read_epoch"]
    assert "to_epoch" in value
    assert db.execute(
        "SELECT COUNT(*) FROM dependencies WHERE scope_id='s1' AND child_id=?",
        (out["record_id"],)).fetchone()[0] == 0
    ev = db.execute(
        "SELECT event_type, metadata_json FROM events WHERE scope_id='s1' "
        "AND event_type='rebase_exhausted'").fetchone()
    assert ev is not None
    meta = json.loads(ev["metadata_json"])
    assert meta["run_id"] == run["run_id"]
    assert meta["attempts"] == 2
    # No extra 'rebase' event was published on the exhausted path.
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    db.close()


def test_rebase_exhaustion_quarantine_is_singular_and_truthful(tmp_path):
    # P1-R2-01 / P1-b: repeated exhaustion must upsert ONE quarantine
    # record (same key), whose value states explicitly that no candidate
    # result was produced or saved, and whose return message does not
    # claim a save that never happened.
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    first = rebase(db, "s1", run["run_id"])
    second = rebase(db, "s1", run["run_id"])
    assert first["status"] == "quarantined"
    assert second["status"] == "quarantined"
    assert second["record_id"] == first["record_id"]
    rows = db.execute(
        "SELECT id, value_json FROM memories WHERE scope_id='s1' "
        "AND key=?", (f"rebase-quarantine-{run['run_id']}",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first["record_id"]
    value = json.loads(rows[0]["value_json"])
    assert value["candidate_result"] is None
    assert value["candidate_result_saved"] is False
    assert "no candidate result was produced" in value.get("candidate_result_note", "").lower()
    assert "Candidate result saved" not in second["reason"]
    assert "No candidate result was produced or saved" in second["reason"]
    # Still exactly 2 'rebase' events, and the record stays tainted.
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    assert db.execute(
        "SELECT state FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0] == "tainted"
    db.close()


def _exhaust(db, run_id, *, candidate_result=None):
    assert rebase(db, "s1", run_id)["status"] == "rebased"
    assert rebase(db, "s1", run_id)["status"] == "rebased"
    return rebase(db, "s1", run_id, candidate_result=candidate_result)


def test_rebase_exhaustion_saves_candidate_not_default_recallable(tmp_path):
    # T015: exhaust rebase twice WITH a candidate -> the quarantine holds
    # the candidate, default recall does not return it, and the next
    # action is readable via get_quarantine().
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert get_quarantine(db, "s1", run["run_id"]) is None
    candidate = {"key": "out", "value": "done-v2"}
    out = _exhaust(db, run["run_id"], candidate_result=candidate)
    assert out["status"] == "quarantined"
    assert out["candidate_result_saved"] is True
    assert out["followup_run_id"]
    assert out["task"] == f"quarantine-followup-{run['run_id']}"
    assert "quarantined candidate" in out["next_action"].lower() or "candidate" in out["next_action"]
    assert out["followup_run_id"] in out["next_action"]
    row = db.execute(
        "SELECT key, kind, state, value_json FROM memories WHERE scope_id='s1' AND id=?",
        (out["record_id"],)).fetchone()
    assert row["kind"] == "handoff" and row["state"] == "tainted"
    value = json.loads(row["value_json"])
    assert value["candidate_result"] == candidate
    assert value["candidate_result_saved"] is True
    assert value["followup_run_id"] == out["followup_run_id"]
    assert value["next_action"] == out["next_action"]
    # The quarantined candidate is NOT default-recallable: default
    # recall() and default search_by_key() never surface it, even for a
    # query matching the candidate key/value.
    assert all(r["id"] != out["record_id"] for r in recall(db, "s1", "out"))
    assert all(r["id"] != out["record_id"] for r in recall(db, "s1", "done-v2"))
    assert search_by_key(db, "s1", f"rebase-quarantine-{run['run_id']}") == []
    # ... but the explicit read path returns it, with the next action.
    q = get_quarantine(db, "s1", run["run_id"])
    assert q is not None and q["record_id"] == out["record_id"]
    assert q["state"] == "tainted"
    assert q["candidate_result"] == candidate
    assert q["candidate_result_saved"] is True
    assert q["followup_run_id"] == out["followup_run_id"]
    assert q["next_action"] == out["next_action"]
    # The independent task is a real, open run the operator can finish in.
    followup = get_run(db, "s1", out["followup_run_id"])
    assert followup["task"] == f"quarantine-followup-{run['run_id']}"
    assert followup["snapshot_epoch"] > run["read_epoch"]
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    # P1-R4-03: the follow-up run is born at the CURRENT epoch — its
    # snapshot epoch equals the scope epoch, so finish() succeeds on it
    # with no other writes (the gap GPT found: this test used to check
    # only existence/openness, never finishability).
    from turnkeep.commits import finish
    from turnkeep.storage import scope_epoch
    assert followup["snapshot_epoch"] == scope_epoch(db, "s1")
    receipt = finish(db, "s1", out["followup_run_id"],
                     {"key": "out2", "value": "done"})
    assert receipt["record_id"] and receipt["commit_epoch"] is not None
    db.close()


def test_repeated_exhaustion_same_candidate_followup_stays_finishable(tmp_path):
    # P1-R5-02: GPT's exact sequence — the original run rebases twice,
    # the first exhaustion saves the candidate and creates the follow-up
    # run, and a second exhaustion with the SAME candidate is a replay:
    # no new rebase_exhausted event, no epoch bump, cause_event_id and
    # to_epoch keep the first event's values — so finish() on the
    # follow-up run succeeds with no other writes.
    from turnkeep.commits import finish
    from turnkeep.storage import scope_epoch
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    candidate = {"key": "out", "value": "done-v2"}
    first = rebase(db, "s1", run["run_id"], candidate_result=candidate)
    assert first["status"] == "quarantined"
    assert first["candidate_result_saved"] is True
    epoch_after_first = scope_epoch(db, "s1")
    to_epoch_first = get_quarantine(db, "s1", run["run_id"])["to_epoch"]
    cause_after_first = db.execute(
        "SELECT cause_event_id FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0]
    event_after_first = db.execute(
        "SELECT id FROM events WHERE scope_id='s1' AND event_type='rebase_exhausted'"
        ).fetchone()[0]
    assert cause_after_first == event_after_first
    # P1-R6-02: an identical repeat is a TRUE no-op — revision and
    # updated_at are untouched too, not just the epoch/event counts.
    rev_after_first, updated_after_first = db.execute(
        "SELECT revision, updated_at FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()
    second = rebase(db, "s1", run["run_id"], candidate_result=candidate)
    assert second["status"] == "quarantined"
    assert second["record_id"] == first["record_id"]
    assert second["followup_run_id"] == first["followup_run_id"]
    # The repeat published nothing and bumped nothing.
    assert scope_epoch(db, "s1") == epoch_after_first
    assert tuple(db.execute(
        "SELECT revision, updated_at FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()) == (rev_after_first, updated_after_first)
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type='rebase_exhausted'"
        ).fetchone()[0] == 1
    assert db.execute(
        "SELECT cause_event_id FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0] == cause_after_first
    assert get_quarantine(db, "s1", run["run_id"])["to_epoch"] == to_epoch_first
    assert get_quarantine(db, "s1", run["run_id"])["candidate_result"] == candidate
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    # The follow-up run is still finishable with no other writes.
    followup = get_run(db, "s1", first["followup_run_id"])
    assert followup["snapshot_epoch"] == scope_epoch(db, "s1")
    receipt = finish(db, "s1", first["followup_run_id"],
                     {"key": "out2", "value": "done"})
    assert receipt["record_id"] and receipt["commit_epoch"] is not None
    db.close()


def test_concurrent_exhaustion_creates_one_quarantine_and_one_followup(tmp_path):
    # P1-R4-05: GPT's exact repro — two connections + a barrier
    # synchronising the interleaving -> exactly 1 quarantine record +
    # 1 follow-up run, both callers naming the same follow-up run.
    # P1-R5-02: the second caller is a replay — it publishes NO new
    # rebase_exhausted event and bumps nothing, so the follow-up run
    # stays finishable. (Before R5 the second event bumped the epoch
    # again after the run was created, leaving it stale; the old
    # comment here said finishability was NOT asserted for that reason.)
    import threading
    path = tmp_path / "r.sqlite3"
    db = open_db(path)
    register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    db.close()
    barrier = threading.Barrier(2)
    results, errors = {}, []

    def _exhaust(name):
        try:
            conn = open_db(path)
            barrier.wait(timeout=30)
            results[name] = rebase(conn, "s1", run["run_id"])
            conn.close()
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    t1 = threading.Thread(target=_exhaust, args=("a",))
    t2 = threading.Thread(target=_exhaust, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors, errors
    assert results["a"]["status"] == "quarantined"
    assert results["b"]["status"] == "quarantined"
    assert results["a"]["followup_run_id"] == results["b"]["followup_run_id"]
    assert results["a"]["record_id"] == results["b"]["record_id"]
    db = open_db(path)
    rows = db.execute(
        "SELECT id FROM memories WHERE scope_id='s1' AND key=?",
        (f"rebase-quarantine-{run['run_id']}",)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == results["a"]["record_id"]
    runs = db.execute(
        "SELECT id FROM runs WHERE scope_id='s1' AND task=?",
        (f"quarantine-followup-{run['run_id']}",)).fetchall()
    assert len(runs) == 1
    assert runs[0][0] == results["a"]["followup_run_id"]
    # P1-R5-02: exactly one rebase_exhausted event (the first caller's),
    # no epoch bump from the replay, and the follow-up run finishes.
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type='rebase_exhausted'"
        ).fetchone()[0] == 1
    from turnkeep.commits import finish
    from turnkeep.storage import scope_epoch
    assert get_run(db, "s1", results["a"]["followup_run_id"])["snapshot_epoch"] == scope_epoch(db, "s1")
    receipt = finish(db, "s1", results["a"]["followup_run_id"],
                     {"key": "out2", "value": "done"})
    assert receipt["record_id"] and receipt["commit_epoch"] is not None
    db.close()


def test_rebase_exhaustion_candidate_upsert_newest_wins_and_none_preserves(tmp_path):
    # REVISED for P1-R6-02. JUSTIFICATION: the old expectation ("a newer
    # candidate saves without breaking finishability") was written for
    # the R5 no-bump design, in which a candidate update published no
    # event and bumped no epoch — so the follow-up run stayed finishable
    # even after its candidate changed under it. That is exactly the hole
    # GPT's R6 two-worker repro drives through: a worker that read v1
    # could still finish(v1) after another worker updated the candidate
    # to v2, because the epoch — the ONLY protection for the candidate,
    # which snapshots never contain (they hold active records only) —
    # did not move. Per CORE_CONTRACT line 57 any memory content change
    # in the scope increments the epoch, so a candidate update MUST bump
    # it, and the follow-up run goes stale BY DESIGN (like any scope
    # change); the operator rebases the follow-up run and finishes there.
    # The R5 PRIMARY acceptance is untouched: an IDENTICAL repeat is a
    # true no-op and the follow-up run stays finishable — see
    # test_repeated_exhaustion_same_candidate_followup_stays_finishable.
    # New semantics asserted here: v2 update -> exactly one
    # 'quarantine_updated' event, epoch +1, exactly one
    # rebase_exhausted, cause_event_id frozen, to_epoch tracks the
    # update; finish on the OLD follow-up run raises STALE_SNAPSHOT;
    # a later no-candidate call is a true no-op (revision/epoch/event
    # counts unchanged, v2 preserved); rebase of the follow-up run ->
    # the new run finishes; get_quarantine returns the new candidate.
    from turnkeep.commits import finish
    from turnkeep.storage import scope_epoch
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    first = _exhaust(db, run["run_id"],
                     candidate_result={"key": "out", "value": "v1"})
    epoch_after_first = scope_epoch(db, "s1")
    to_epoch_first = get_quarantine(db, "s1", run["run_id"])["to_epoch"]
    cause_after_first = db.execute(
        "SELECT cause_event_id FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0]
    assert to_epoch_first == epoch_after_first
    # A candidate update is a content change, not a replay.
    second = rebase(db, "s1", run["run_id"],
                    candidate_result={"key": "out", "value": "v2"})
    assert second["status"] == "quarantined"
    assert second["record_id"] == first["record_id"]
    assert second["followup_run_id"] == first["followup_run_id"]
    assert get_quarantine(db, "s1", run["run_id"])["candidate_result"] == {"key": "out", "value": "v2"}
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type='rebase_exhausted'"
        ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type='quarantine_updated'"
        ).fetchone()[0] == 1
    upd_meta = json.loads(db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s1' AND event_type='quarantine_updated'"
        ).fetchone()[0])
    assert upd_meta["run_id"] == run["run_id"]
    assert upd_meta["candidate_revision"] == 2
    assert scope_epoch(db, "s1") == epoch_after_first + 1
    assert get_quarantine(db, "s1", run["run_id"])["to_epoch"] == scope_epoch(db, "s1")
    assert db.execute(
        "SELECT cause_event_id FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0] == cause_after_first
    assert db.execute(
        "SELECT revision FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()[0] == 2
    # The old follow-up run is stale BY DESIGN after the update.
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", first["followup_run_id"],
               {"key": "out2", "value": "done"})
    assert ei.value.code == "STALE_SNAPSHOT"
    # A later call with NO candidate is a true no-op: v2 preserved,
    # revision / epoch / event counts untouched.
    rev_before, updated_before = db.execute(
        "SELECT revision, updated_at FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()
    third = rebase(db, "s1", run["run_id"])
    assert third["status"] == "quarantined"
    assert third["record_id"] == first["record_id"]
    assert third["followup_run_id"] == first["followup_run_id"]
    assert third["candidate_result_saved"] is True
    q = get_quarantine(db, "s1", run["run_id"])
    assert q["candidate_result"] == {"key": "out", "value": "v2"}
    assert q["candidate_result_saved"] is True
    assert q["to_epoch"] == scope_epoch(db, "s1")
    assert tuple(db.execute(
        "SELECT revision, updated_at FROM memories WHERE scope_id='s1' AND id=?",
        (first["record_id"],)).fetchone()) == (rev_before, updated_before)
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type='quarantine_updated'"
        ).fetchone()[0] == 1
    assert scope_epoch(db, "s1") == epoch_after_first + 1
    rows = db.execute(
        "SELECT id FROM memories WHERE scope_id='s1' AND key=?",
        (f"rebase-quarantine-{run['run_id']}",)).fetchall()
    assert len(rows) == 1
    runs = db.execute(
        "SELECT id FROM runs WHERE scope_id='s1' AND task=?",
        (f"quarantine-followup-{run['run_id']}",)).fetchall()
    assert len(runs) == 1 and runs[0]["id"] == first["followup_run_id"]
    assert rebase_attempts(db, "s1", run["run_id"]) == 2
    # Rebase the follow-up run and finish there; get_quarantine still
    # returns the new candidate.
    rebased = rebase(db, "s1", first["followup_run_id"])
    assert rebased["status"] == "rebased"
    assert get_run(db, "s1", rebased["run_id"])["snapshot_epoch"] == scope_epoch(db, "s1")
    receipt = finish(db, "s1", rebased["run_id"],
                     {"key": "out2", "value": "done"})
    assert receipt["record_id"] and receipt["commit_epoch"] is not None
    assert get_quarantine(db, "s1", run["run_id"])["candidate_result"] == {"key": "out", "value": "v2"}
    db.close()


def test_repeated_exhaustion_gpt_r6_sequence(tmp_path):
    # P1-R6-02: GPT's exact B3 sequence. First exhaustion saves v1 ->
    # quarantine revision 1, scope epoch 4, exactly 1 rebase_exhausted
    # event, follow-up snapshot epoch 4 with no quarantine record in it.
    # Identical repeat (same v1) and no-candidate repeat are TRUE
    # no-ops: revision, updated_at, epoch, event counts and to_epoch
    # all unchanged, and the follow-up run stays finishable (the R5
    # PRIMARY acceptance — the actual finish is asserted in
    # test_repeated_exhaustion_same_candidate_followup_stays_finishable).
    # Update to v2 -> exactly one new 'quarantine_updated' event,
    # epoch +1, exactly one rebase_exhausted, cause_event_id frozen,
    # to_epoch tracks the update, candidate is v2. Two-worker sequence:
    # a worker that read v1 cannot finish(v1) after the v2 update
    # (STALE_SNAPSHOT); rebasing the follow-up run then finishes.
    from turnkeep.commits import finish
    from turnkeep.storage import scope_epoch
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    qkey = f"rebase-quarantine-{run['run_id']}"

    def _qrow():
        return db.execute(
            "SELECT revision, updated_at, cause_event_id FROM memories "
            "WHERE scope_id='s1' AND key=?", (qkey,)).fetchone()

    def _ev_count(event_type):
        return db.execute(
            "SELECT COUNT(*) FROM events WHERE scope_id='s1' AND event_type=?",
            (event_type,)).fetchone()[0]

    v1 = {"key": "out", "value": "v1"}
    first = rebase(db, "s1", run["run_id"], candidate_result=v1)
    assert first["status"] == "quarantined"
    assert _qrow()[0] == 1
    assert scope_epoch(db, "s1") == 4
    assert _ev_count("rebase_exhausted") == 1
    assert _ev_count("quarantine_updated") == 0
    fu = get_run(db, "s1", first["followup_run_id"])
    assert fu["snapshot_epoch"] == 4
    assert all(r["key"] != qkey for r in fu["snapshot"]["records"])
    to_epoch_first = get_quarantine(db, "s1", run["run_id"])["to_epoch"]
    assert to_epoch_first == 4
    cause_first = _qrow()[2]
    # Identical repeat: true no-op.
    rev_before, updated_before = _qrow()[0], _qrow()[1]
    second = rebase(db, "s1", run["run_id"], candidate_result=v1)
    assert second["status"] == "quarantined"
    assert second["record_id"] == first["record_id"]
    assert tuple(_qrow()) == (rev_before, updated_before, cause_first)
    assert scope_epoch(db, "s1") == 4
    assert _ev_count("rebase_exhausted") == 1
    assert _ev_count("quarantine_updated") == 0
    assert get_quarantine(db, "s1", run["run_id"])["to_epoch"] == to_epoch_first
    # R5 primary acceptance still holds: the follow-up run is still
    # finishable (snapshot epoch == scope epoch) after identical repeats.
    assert get_run(db, "s1", first["followup_run_id"])["snapshot_epoch"] == scope_epoch(db, "s1")
    # No-candidate repeat: true no-op as well.
    third = rebase(db, "s1", run["run_id"])
    assert third["status"] == "quarantined"
    assert tuple(_qrow()) == (rev_before, updated_before, cause_first)
    assert scope_epoch(db, "s1") == 4
    assert _ev_count("rebase_exhausted") == 1
    assert _ev_count("quarantine_updated") == 0
    assert get_quarantine(db, "s1", run["run_id"])["candidate_result"] == v1
    # v2 update: a content change — one new event, epoch +1.
    v2 = {"key": "out", "value": "v2"}
    fourth = rebase(db, "s1", run["run_id"], candidate_result=v2)
    assert fourth["status"] == "quarantined"
    assert _qrow()[0] == 2
    assert _ev_count("quarantine_updated") == 1
    upd_meta = json.loads(db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s1' AND event_type='quarantine_updated'"
        ).fetchone()[0])
    assert upd_meta["run_id"] == run["run_id"]
    assert upd_meta["candidate_revision"] == 2
    assert scope_epoch(db, "s1") == 5
    assert _ev_count("rebase_exhausted") == 1
    assert _qrow()[2] == cause_first
    assert get_quarantine(db, "s1", run["run_id"])["to_epoch"] == 5
    assert get_quarantine(db, "s1", run["run_id"])["candidate_result"] == v2
    # Two-worker stale finish: worker A read v1, worker B updated to v2.
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", first["followup_run_id"], {"key": "out2", "value": "v1"})
    assert ei.value.code == "STALE_SNAPSHOT"
    # Rebase the follow-up run, then finish there.
    rebased = rebase(db, "s1", first["followup_run_id"])
    assert rebased["status"] == "rebased"
    assert get_run(db, "s1", rebased["run_id"])["snapshot_epoch"] == scope_epoch(db, "s1")
    receipt = finish(db, "s1", rebased["run_id"],
                     {"key": "out2", "value": "done"})
    assert receipt["record_id"] and receipt["commit_epoch"] is not None
    db.close()


def test_rebase_exhaustion_without_candidate_stays_truthful_via_read_path(tmp_path):
    # The pre-existing truthful behaviour survives: no candidate ->
    # explicit marker, and the read path still names the next action
    # (the independent task run) instead of claiming a save.
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    out = _exhaust(db, run["run_id"])
    assert out["candidate_result_saved"] is False
    assert "No candidate result was produced or saved" in out["reason"]
    q = get_quarantine(db, "s1", run["run_id"])
    assert q["candidate_result"] is None
    assert q["candidate_result_saved"] is False
    assert "no candidate result was produced" in q["candidate_result_note"].lower()
    assert q["followup_run_id"] == out["followup_run_id"]
    assert q["followup_run_id"] in q["next_action"]
    assert get_run(db, "s1", q["followup_run_id"])["task"] == f"quarantine-followup-{run['run_id']}"
    db.close()


def test_rebase_candidate_must_be_key_value_result(tmp_path):
    # A malformed candidate fails loudly (INVALID_INPUT) instead of
    # being stored half-understood — on the exhausted call and earlier.
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    for bad in ("a string", ["out"], {"value": "x"}, {"key": "out"},
                {"key": "", "value": "x"}, {"key": 7, "value": "x"}):
        with pytest.raises(TurnkeepError) as ei:
            rebase(db, "s1", run["run_id"], candidate_result=bad)
        assert ei.value.code == "INVALID_INPUT"
    # No rebase event was consumed by the rejected calls.
    assert rebase_attempts(db, "s1", run["run_id"]) == 0
    assert get_quarantine(db, "s1", run["run_id"]) is None
    # A candidate on a non-exhausted call is validated but not persisted:
    # it is still live work, superseded by the recompute in the new run.
    out = rebase(db, "s1", run["run_id"],
                 candidate_result={"key": "out", "value": "draft"})
    assert out["status"] == "rebased"
    assert get_quarantine(db, "s1", run["run_id"]) is None
    db.close()
