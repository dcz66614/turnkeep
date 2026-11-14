"""T012 taint tests: remember-caused conflict also taints descendants."""
import pytest
from turnkeep.storage import open_db, row_or_404, dump_record
from turnkeep.scopes import register_scope
from turnkeep.memory import remember

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_remember_conflict_taints_child(tmp_path):
    db = open_db(tmp_path / "t.sqlite3"); register_scope(db, "s1")
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    plan = remember(db, "s1", "plan", "build 3.11", kind="decision", source_refs=SRC,
                    parent_ids=[req["id"]])
    remember(db, "s1", "req", "3.12", kind="requirement", source_refs=SRC)
    assert dump_record(db, row_or_404(db, "s1", plan["id"]))["state"] == "tainted"
    db.close()


def test_tainted_never_returns_as_active(tmp_path):
    db = open_db(tmp_path / "t.sqlite3"); register_scope(db, "s1")
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    plan = remember(db, "s1", "plan", "build 3.11", kind="decision", source_refs=SRC,
                    parent_ids=[req["id"]])
    from turnkeep.corrections import retract
    retract(db, "s1", req["id"], 1, "correction")
    from turnkeep.memory import active_records
    assert plan["id"] not in {r["id"] for r in active_records(db, "s1")}
    db.close()
