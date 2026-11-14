"""T011 corrections tests."""
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import correct, retract, resolve_conflict
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "c.sqlite3"); register_scope(db, "s1"); return db


def test_correct_supersedes_and_taints(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "build.python_version", "3.11", kind="requirement", source_refs=SRC)
    plan = remember(db, "s1", "build.plan", "Build for 3.11", kind="decision",
                    source_refs=SRC, parent_ids=[req["id"]])
    new = correct(db, "s1", req["id"], 1, "3.12", source_refs=SRC)
    assert new["value"] == "3.12" and new["supersedes_ids"] == [req["id"]]
    from turnkeep.storage import row_or_404, dump_record
    assert dump_record(db, row_or_404(db, "s1", req["id"]))["state"] == "superseded"
    assert dump_record(db, row_or_404(db, "s1", plan["id"]))["state"] == "tainted"
    db.close()


def test_correct_wrong_revision_fails_without_side_effects(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "k", "v1", source_refs=SRC)
    epoch = scope_epoch(db, "s1")
    with pytest.raises(TurnkeepError) as ei:
        correct(db, "s1", req["id"], 99, "v2", source_refs=SRC)
    assert ei.value.code == "VERSION_CONFLICT"
    assert scope_epoch(db, "s1") == epoch  # failed change writes nothing
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s1'").fetchone()[0] == 1
    db.close()


def test_correct_conflicted_requires_resolve(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    remember(db, "s1", "k", "v2", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        correct(db, "s1", a["id"], 2, "v3", source_refs=SRC)
    assert ei.value.code == "CONFLICT_REQUIRES_RESOLVE"
    db.close()
