"""T012 lineage tests: chain, diamond, cycle, cross-scope, missing parent."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import correct, retract
from turnkeep.lineage import impact, check_usable
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "l.sqlite3"); register_scope(db, "s1"); register_scope(db, "s2"); return db


def test_chain_taint_propagates(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "a", "1", source_refs=SRC)
    b = remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    c = remember(db, "s1", "c", "3", source_refs=SRC, parent_ids=[b["id"]])
    correct(db, "s1", a["id"], 1, "1b", source_refs=SRC)
    from turnkeep.storage import row_or_404, dump_record
    assert dump_record(db, row_or_404(db, "s1", b["id"]))["state"] == "tainted"
    assert dump_record(db, row_or_404(db, "s1", c["id"]))["state"] == "tainted"
    assert check_usable(db, "s1", c["id"]) is False
    db.close()


def test_diamond_both_parents_needed(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "a", "1", source_refs=SRC)
    b = remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    c = remember(db, "s1", "c", "3", source_refs=SRC, parent_ids=[a["id"]])
    d = remember(db, "s1", "d", "4", source_refs=SRC, parent_ids=[b["id"], c["id"]])
    retract(db, "s1", b["id"], 1, "correction")
    from turnkeep.storage import row_or_404, dump_record
    assert dump_record(db, row_or_404(db, "s1", d["id"]))["state"] == "tainted"
    assert dump_record(db, row_or_404(db, "s1", c["id"]))["state"] == "active"
    db.close()


def test_cycle_rejected(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "a", "1", source_refs=SRC)
    b = remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    # a cannot gain b as a parent: that would cycle a->b->a. Use correct() with
    # parent_ids=[b] on a fresh value — the new record's parents are validated,
    # and the cycle check runs against the child id; here we test the direct
    # validator instead: inserting a record whose parent chain leads back.
    from turnkeep.storage import validate_parents
    with pytest.raises(TurnkeepError) as ei:
        validate_parents(db, "s1", [b["id"]], child_id=a["id"])
    assert ei.value.code == "CYCLE_DETECTED"
    db.close()


def test_cross_scope_parent_rejected(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s2", "a", "1", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    assert ei.value.code == "INVALID_PARENT"
    db.close()


def test_missing_parent_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=["no-such-id"])
    assert ei.value.code == "INVALID_PARENT"
    db.close()


def test_impact_paths(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "a", "1", source_refs=SRC)
    b = remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    paths = impact(db, "s1", a["id"])
    assert [p["record_id"] for p in paths] == [b["id"]]
    assert paths[0]["dependency_path"] == [a["id"], b["id"]]
    db.close()
