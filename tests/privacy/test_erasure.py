"""T038 erasure: payload gone from every read path, receipt clean, tombstone kept."""
import json

import pytest

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retrieval import recall, search_by_key
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record

SECRET = "SYNTHETIC-SECRET-erasure-test"
SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "e.sqlite3")
    register_scope(db, "e")
    return db


def _seed_with_receipt(db):
    run = begin(db, "e", "write the secret down")
    done = finish(db, "e", run["run_id"], {"key": "ci.api_key", "value": SECRET},
                  idempotency_key="e-1")
    return search_by_key(db, "e", "ci.api_key")[0]


def test_erase_parent_taints_child_and_recall_hides_it(tmp_path):
    # P0-1 repro: erasing a parent must taint its descendants (CORE_CONTRACT
    # §41/§47) and the tainted child must no longer come back as active.
    db = _db(tmp_path)
    parent = remember(db, "e", "parent.key", "parent value", kind="fact", source_refs=SRC)
    child = remember(db, "e", "child.key", "child value", kind="fact",
                     source_refs=SRC, parent_ids=[parent["id"]])
    out = erase_record(db, "e", parent["id"], parent["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    row = db.execute("SELECT state FROM memories WHERE id=?", (child["id"],)).fetchone()
    assert row[0] == "tainted"
    assert recall(db, "e", "child value") == []
    # History recall still shows the child, but with its tainted label,
    # never as active (the erased parent may also match on "value").
    hits = recall(db, "e", "child value", include_history=True)
    assert not any(h["_label"] == "active" for h in hits)
    child_hit = next(h for h in hits if h["id"] == child["id"])
    assert child_hit["_label"] == "tainted"
    # The erase event's affected ids include the tainted descendant.
    ev = db.execute("SELECT affected_ids_json FROM events WHERE event_type='erase'").fetchone()
    assert child["id"] in json.loads(ev[0]) and parent["id"] in json.loads(ev[0])
    db.close()


def test_erase_nested_secret_dict_is_complete(tmp_path):
    # P0-3 repro: a secret nested inside a structured value must be found
    # via the recursively expanded string leaves, not just the top level.
    from turnkeep.erasure import _scan_payload, residual_report
    db = _db(tmp_path)
    nested = {"api_key": "sk-nested-SECRET-123", "other": "x"}
    rec = remember(db, "e", "nested.key", nested, kind="fact", source_refs=SRC)
    out = erase_record(db, "e", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    assert not _scan_payload(db, "sk-nested-SECRET-123", "e")
    assert recall(db, "e", "sk-nested-SECRET-123") == []
    # History recall may still show the tombstoned record (label "erased"),
    # but it must never carry the secret leaf.
    assert not any("sk-nested-SECRET-123" in str(r.get("value", ""))
                   for r in recall(db, "e", "sk-nested-SECRET-123", include_history=True))
    rep = residual_report(db, "e")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_erase_removes_payload_everywhere(tmp_path):
    db = _db(tmp_path)
    rec = _seed_with_receipt(db)
    receipt = erase_record(db, "e", rec["id"], rec["revision"], "rotation")
    assert receipt["status"] == "COMPLETE" and receipt["erased"] is True
    assert not any(SECRET in str(r.get("value", "")) for r in recall(db, "e", "api_key"))
    assert not any(SECRET in str(r.get("value", "")) for r in recall(db, "e", "api_key", include_history=True))
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?", (rec["id"],)).fetchone()
    assert json.loads(row[0]) == {"_erased": True} and row[1] == "erased"
    # Derived receipts were scrubbed, not deleted: the pointer stays valid.
    run_row = db.execute("SELECT receipt_json FROM runs").fetchone()
    assert SECRET not in run_row[0]
    assert json.loads(run_row[0])["record_id"] == rec["id"]
    assert SECRET not in json.dumps(receipt)  # receipt never copies the payload
    db.close()


def test_erase_is_cas_and_idempotent_guard(tmp_path):
    # CAS still guards: erasing with a stale revision conflicts, before
    # and after the erase.
    db = _db(tmp_path)
    rec = _seed_with_receipt(db)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "e", rec["id"], rec["revision"] + 1, "rotation")
    assert ei.value.code == "VERSION_CONFLICT"
    out = erase_record(db, "e", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    # P0-R3-01 repair: a second erase of an already-erased record no
    # longer raises INVALID_INPUT ("already erased") — it re-runs the
    # scrub + scan for that record (here a clean no-op), without
    # resurrecting it and without bumping the revision a second time.
    # (This expectation changed with the R3 rework; the old behaviour
    # made a PARTIAL erase uncompletable.)
    out2 = erase_record(db, "e", rec["id"], out["revision"], "rotation")
    assert out2["status"] == "COMPLETE" and out2["erased"] is True
    assert out2["revision"] == out["revision"]
    assert out2.get("repair") is True
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?", (rec["id"],)).fetchone()
    assert json.loads(row[0]) == {"_erased": True} and row[1] == "erased"
    # ... but a second erase with a WRONG revision still conflicts.
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "e", rec["id"], out["revision"] + 1, "rotation")
    assert ei.value.code == "VERSION_CONFLICT"
    db.close()


def test_erase_does_not_rewrite_append_only_history(tmp_path):
    # If the payload somehow also lives in events (a writer bug), erasure
    # refuses with PARTIAL and leaves the record untouched.
    db = _db(tmp_path)
    rec = _seed_with_receipt(db)
    from turnkeep.storage import publish_event
    publish_event(db, "e", "note", "local", {rec["id"]: None},
                  metadata={"leaked": SECRET})
    out = erase_record(db, "e", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is False
    row = db.execute("SELECT value_json FROM memories WHERE id=?", (rec["id"],)).fetchone()
    assert SECRET in row[0]
    db.close()
