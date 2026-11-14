"""T010 remember/conflict tests (product core, target schema)."""
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts, active_records
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "m.sqlite3")
    register_scope(db, "s1")
    return db


def test_same_write_reuse_and_noop(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    b = remember(db, "s1", "k", "v1", source_refs=SRC)
    assert a["id"] == b["id"] and b["revision"] == 1
    assert scope_epoch(db, "s1") == 1  # no-op does not bump epoch
    db.close()


def test_append_source_needs_revision(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "k", "v1", source_refs=[{"id": "u:2", "type": "user_statement"}])
    assert ei.value.code == "VERSION_REQUIRED"
    b = remember(db, "s1", "k", "v1", source_refs=[{"id": "u:2", "type": "user_statement"}],
                 expected_revision=1)
    assert b["revision"] == 2
    assert {s["id"] for s in b["source_refs"]} == {"u:1", "u:2"}
    db.close()


def test_different_value_conflicts_both(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v1", source_refs=SRC)
    r = remember(db, "s1", "k", "v2", source_refs=SRC)
    assert r["state"] == "conflicted"
    assert len(list_conflicts(db, "s1", "k")) == 2
    assert active_records(db, "s1") == []  # both leave the default active view
    db.close()


def test_conflicted_same_value_cannot_reuse(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v1", source_refs=SRC)
    remember(db, "s1", "k", "v2", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "k", "v1", source_refs=SRC)
    assert ei.value.code == "CONFLICT_REQUIRES_RESOLVE"
    db.close()


def test_only_history_needs_replaces(tmp_path):
    db = _db(tmp_path)
    from turnkeep.corrections import retract
    a = remember(db, "s1", "k", "v1", source_refs=SRC)
    retract(db, "s1", a["id"], 1, "correction")
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "k", "v2", source_refs=SRC)
    assert ei.value.code == "REPLACEMENT_REQUIRED"
    b = remember(db, "s1", "k", "v2", source_refs=SRC, replaces_ids=[a["id"]])
    assert b["state"] == "active" and b["supersedes_ids"] == [a["id"]]
    db.close()


def test_imported_source_keeps_type(tmp_path):
    db = _db(tmp_path)
    r = remember(db, "s1", "k", "v1", source_refs=[{"id": "web:1", "type": "imported_text"}])
    assert r["source_refs"] == [{"id": "web:1", "type": "imported_text"}]
    db.close()


def test_normalisation_is_not_semantic(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "Python 3.12", source_refs=SRC)
    # Case differs -> different normalised value -> conflict, NOT a merge.
    r = remember(db, "s1", "k", "python 3.12", source_refs=SRC)
    assert r["state"] == "conflicted"
    db.close()
