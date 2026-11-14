"""T018 fault + negative scenarios: cross-scope, cycle, invalid parent,
forged run, finish replay, empty-read phantom. All against a real DB file.
"""
import json
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin, get_run
from turnkeep.commits import finish
from turnkeep.corrections import correct, retract
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "f.sqlite3"); register_scope(db, "s1"); register_scope(db, "s2"); return db


def test_cross_scope_recall_and_parent_blocked(tmp_path):
    db = _db(tmp_path)
    remember(db, "s2", "k", "secret", source_refs=SRC)
    from turnkeep.retrieval import recall
    assert recall(db, "s1", "secret") == []
    a = remember(db, "s2", "a", "1", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "b", "2", source_refs=SRC, parent_ids=[a["id"]])
    assert ei.value.code == "INVALID_PARENT"
    db.close()


def test_forged_run_finish_blocked(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", "forged", {"key": "out", "value": "x"})
    assert ei.value.code == "NOT_FOUND"
    db.close()


def test_finish_replay_after_restart(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v", source_refs=SRC)
    run = begin(db, "s1", "w")
    r1 = finish(db, "s1", run["run_id"], {"key": "out", "value": "x"}, idempotency_key="k-1")
    db.close()
    db = open_db(tmp_path / "f.sqlite3")
    r2 = finish(db, "s1", run["run_id"], {"key": "out", "value": "x"}, idempotency_key="k-1")
    assert r2 == r1
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='out'").fetchone()[0] == 1
    db.close()


def test_empty_read_phantom(tmp_path):
    db = _db(tmp_path)
    run = begin(db, "s1", "w")
    remember(db, "s1", "late", "fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], {"key": "out", "value": "x"})
    assert ei.value.code == "STALE_SNAPSHOT"
    db.close()


def test_coarse_epoch_misblock_is_recorded(tmp_path):
    # Same-scope UNRELATED write mis-blocks an old run (known coarse-epoch
    # limitation, contract §7). It must be a clean STALE_SNAPSHOT with a
    # reason, not a silent accept — and the diff must name the change.
    db = _db(tmp_path)
    remember(db, "s1", "a", "1", source_refs=SRC)
    run = begin(db, "s1", "w")
    remember(db, "s1", "unrelated", "2", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], {"key": "out", "value": "x"})
    assert ei.value.code == "STALE_SNAPSHOT"
    assert "unrelated" in ei.value.details.get("changed_keys", [])
    db.close()


def test_corrupt_input_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError):
        remember(db, "s1", "k", float("nan"), source_refs=SRC)
    with pytest.raises(TurnkeepError):
        remember(db, "s1", "k", "v", kind="bogus", source_refs=SRC)
    with pytest.raises(TurnkeepError):
        remember(db, "s1", "k", "v", source_refs=[])
    db.close()
