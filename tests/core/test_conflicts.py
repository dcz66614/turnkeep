"""T010 conflict-query tests."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts
from turnkeep.corrections import resolve_conflict

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_conflict_group_lists_both_values(tmp_path):
    db = open_db(tmp_path / "c.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "k", "a", source_refs=SRC)
    remember(db, "s1", "k", "b", source_refs=SRC)
    group = list_conflicts(db, "s1", "k")
    assert {r["value"] for r in group} == {"a", "b"}
    assert list_conflicts(db, "s1", "other") == []
    db.close()


def test_resolve_clears_group(tmp_path):
    db = open_db(tmp_path / "c.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "k", "a", source_refs=SRC)
    remember(db, "s1", "k", "b", source_refs=SRC)
    group = list_conflicts(db, "s1", "k")
    cands = [{"id": r["id"], "revision": r["revision"]} for r in group]
    r = resolve_conflict(db, "s1", "k", cands, "a", source_refs=SRC)
    assert r["state"] == "active" and r["value"] == "a"
    assert list_conflicts(db, "s1", "k") == []
    db.close()
