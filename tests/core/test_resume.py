"""T015 resume / rebase tests."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.corrections import correct
from turnkeep.recovery import resume, rebase, diff_snapshot
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1"); return db


def test_resume_diff_and_candidate(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    correct(db, "s1", req["id"], 1, "3.12", source_refs=SRC)
    d = resume(db, "s1", run["run_id"])
    assert d["changed_keys"] == ["req"]
    assert d["old_result_still_valid"] is False
    assert d["candidate_snapshot"]["read_epoch"] > d["from_epoch"]
    db.close()


def test_rebase_max_two_then_quarantine(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    r1 = rebase(db, "s1", run["run_id"])
    assert r1["status"] == "rebased" and r1["attempts"] == 1
    r2 = rebase(db, "s1", run["run_id"])
    assert r2["status"] == "rebased" and r2["attempts"] == 2
    r3 = rebase(db, "s1", run["run_id"])
    assert r3["status"] == "quarantined"
    db.close()


def test_resume_does_not_validate_old_result(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    receipt = finish(db, "s1", run["run_id"], {"key": "out", "value": "done"})
    d = resume(db, "s1", run["run_id"])
    assert d["changed"] == []  # nothing changed; resume still does not re-validate the receipt itself
    db.close()
