"""T013 begin snapshot tests."""
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin, get_run
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1"); register_scope(db, "s2"); return db


def test_begin_snapshot_matches_epoch_and_records(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    run = begin(db, "s1", "do work")
    assert run["read_epoch"] == scope_epoch(db, "s1")
    assert [r["id"] for r in run["records"]] == [a["id"]]
    stored = get_run(db, "s1", run["run_id"])
    assert stored["snapshot_epoch"] == run["read_epoch"]
    assert stored["status"] == "open"
    db.close()


def test_begin_survives_restart_and_other_scope_ignored(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v1", source_refs=SRC)
    run = begin(db, "s1", "work")
    db.close()
    db = open_db(tmp_path / "r.sqlite3")
    assert get_run(db, "s1", run["run_id"])["status"] == "open"
    remember(db, "s2", "k", "other", source_refs=SRC)  # other scope: no effect
    assert get_run(db, "s1", run["run_id"])["snapshot_epoch"] == run["read_epoch"]
    db.close()


def test_client_cannot_forge_run(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError) as ei:
        get_run(db, "s1", "forged-run-id")
    assert ei.value.code == "NOT_FOUND"
    db.close()
