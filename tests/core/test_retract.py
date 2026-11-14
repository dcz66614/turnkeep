"""T011 retract tests: history kept, recall stops, no revision reuse."""
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, active_records
from turnkeep.corrections import retract
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_retract_keeps_history_and_stops_recall(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    r = retract(db, "s1", a["id"], 1, "withdrawn")
    assert r["state"] == "retracted" and r["revision"] == 2
    assert active_records(db, "s1") == []
    assert active_records(db, "s1", include_history=True)[0]["state"] == "retracted"
    db.close()


def test_retract_twice_fails(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    retract(db, "s1", a["id"], 1, "correction")
    with pytest.raises(TurnkeepError):
        retract(db, "s1", a["id"], 2, "correction")
    db.close()


def test_retract_is_not_erase(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    retract(db, "s1", a["id"], 1, "correction")
    row = db.execute("SELECT value_json FROM memories WHERE id=?", (a["id"],)).fetchone()
    assert row[0] == '"v1"'  # payload retained in history; erasure is T038, not this
    db.close()
