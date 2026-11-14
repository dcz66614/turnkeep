"""P1-g: every state change cites the event that caused it.

After correct / retract / resolve_conflict / remember-conflict /
sweep_expired / finish / erase (and the rebase-exhausted quarantine),
each affected record's cause_event_id equals the triggering event's id,
read back from the events table. insert_record writes the initial event
id on creation; the merge path cites the merge_provenance event id.
"""
import json

from turnkeep.storage import open_db, _id, _json, insert_record, publish_event, transaction
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts
from turnkeep.corrections import correct, retract, resolve_conflict
from turnkeep.retention import set_expiry, sweep_expired
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.recovery import rebase
from turnkeep.erasure import erase_record

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path, scope="s1"):
    db = open_db(tmp_path / "c.sqlite3")
    register_scope(db, scope)
    return db


def _cause(db, record_id):
    return db.execute("SELECT cause_event_id FROM memories WHERE id=?",
                      (record_id,)).fetchone()[0]


def _event_id(db, event_type, scope="s1"):
    row = db.execute(
        "SELECT id FROM events WHERE scope_id=? AND event_type=? "
        "ORDER BY scope_epoch DESC LIMIT 1", (scope, event_type)).fetchone()
    assert row is not None, f"no {event_type} event"
    return row[0]


def _parent_child(db):
    # Distinctive values: erasure scans for every string leaf, so a
    # one-letter value would false-positive against event metadata.
    parent = remember(db, "s1", "parent.k", "parent value 7f3a", kind="fact", source_refs=SRC)
    child = remember(db, "s1", "child.k", "child value 9b2e", kind="fact", source_refs=SRC,
                     parent_ids=[parent["id"]])
    return parent, child


def test_helpers_pin_the_contract(tmp_path):
    # publish_event(event_id=...) uses the given id; insert_record writes
    # the given cause_event_id on creation.
    db = _db(tmp_path)
    ev = _id()
    with transaction(db, write=True):
        epoch = publish_event(db, "s1", "test", "local", {"x": None}, event_id=ev)
        assert epoch == 1
        rid = insert_record(db, "s1", "k", _json("v"), "fact", SRC, [], {}, cause_event_id=ev)
    assert db.execute("SELECT id FROM events WHERE event_type='test'").fetchone()[0] == ev
    assert _cause(db, rid) == ev
    db.close()


def test_remember_writes_initial_event_id(tmp_path):
    db = _db(tmp_path)
    r = remember(db, "s1", "k", "v1", source_refs=SRC)
    assert _cause(db, r["id"]) == _event_id(db, "remember")
    db.close()


def test_correct_wires_one_event_id(tmp_path):
    db = _db(tmp_path)
    parent, child = _parent_child(db)
    new = correct(db, "s1", parent["id"], 1, "p2", source_refs=SRC)
    ev = _event_id(db, "correct")
    assert _cause(db, parent["id"]) == ev  # superseded
    assert _cause(db, new["id"]) == ev     # new record
    assert _cause(db, child["id"]) == ev   # tainted descendant
    db.close()


def test_retract_wires_one_event_id(tmp_path):
    db = _db(tmp_path)
    parent, child = _parent_child(db)
    retract(db, "s1", parent["id"], 1, "correction")
    ev = _event_id(db, "retract")
    assert _cause(db, parent["id"]) == ev
    assert _cause(db, child["id"]) == ev
    db.close()


def test_resolve_conflict_wires_one_event_id(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v1", source_refs=SRC)
    remember(db, "s1", "k", "v2", source_refs=SRC)
    candidates = [{"id": c["id"], "revision": c["revision"]}
                  for c in list_conflicts(db, "s1", "k")]
    new = resolve_conflict(db, "s1", "k", candidates, "v3", source_refs=SRC)
    ev = _event_id(db, "resolve_conflict")
    for c in candidates:
        assert _cause(db, c["id"]) == ev  # superseded candidates
    assert _cause(db, new["id"]) == ev
    db.close()


def test_remember_conflict_wires_one_event_id(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    b = remember(db, "s1", "k", "v2", source_refs=SRC)
    ev = _event_id(db, "remember_conflict")
    assert _cause(db, a["id"]) == ev
    assert _cause(db, b["id"]) == ev
    db.close()


def test_remember_merge_sets_cause_to_merge_event(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    b = remember(db, "s1", "k", "v1",
                 source_refs=[{"id": "u:2", "type": "user_statement"}],
                 expected_revision=1)
    assert b["revision"] == 2
    assert _cause(db, a["id"]) == _event_id(db, "merge_provenance")
    db.close()


def test_sweep_expired_wires_one_event_id(tmp_path):
    db = _db(tmp_path)
    parent, child = _parent_child(db)
    set_expiry(db, "s1", parent["id"], "2026-01-01T00:00:00+00:00", parent["revision"])
    out = sweep_expired(db, "s1", now="2026-06-01T00:00:00+00:00")
    assert out["expired"] == [parent["id"]]
    ev = _event_id(db, "expire_sweep")
    assert _cause(db, parent["id"]) == ev
    assert _cause(db, child["id"]) == ev
    db.close()


def test_finish_wires_event_id(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    receipt = finish(db, "s1", run["run_id"], {"key": "out", "value": "done"},
                     idempotency_key="key-1234")
    assert _cause(db, receipt["record_id"]) == _event_id(db, "finish")
    db.close()


def test_erase_wires_event_id(tmp_path):
    db = _db(tmp_path)
    parent, child = _parent_child(db)
    out = erase_record(db, "s1", parent["id"], parent["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    ev = _event_id(db, "erase")
    assert _cause(db, parent["id"]) == ev  # tombstoned record
    assert _cause(db, child["id"]) == ev   # tainted descendant
    db.close()


def test_rebase_exhaustion_quarantine_wires_event_id(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    assert rebase(db, "s1", run["run_id"])["status"] == "rebased"
    out = rebase(db, "s1", run["run_id"])
    assert out["status"] == "quarantined"
    assert _cause(db, out["record_id"]) == _event_id(db, "rebase_exhausted")
    db.close()
