"""T016 retrieval filter tests."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import retract
from turnkeep.retrieval import recall, search_by_key

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1"); register_scope(db, "s2"); return db


def test_cross_scope_excluded_at_candidate_stage(tmp_path):
    db = _db(tmp_path)
    remember(db, "s2", "secret", "s2 value", source_refs=SRC)
    remember(db, "s1", "secret", "s1 value", source_refs=SRC)
    hits = recall(db, "s1", "secret")
    assert [h["value"] for h in hits] == ["s1 value"]
    db.close()


def test_invalid_records_not_returned_by_default(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s1", "k", "old value", source_refs=SRC)
    retract(db, "s1", a["id"], 1, "correction")
    assert recall(db, "s1", "old") == []
    hits = recall(db, "s1", "old", include_history=True)
    assert len(hits) == 1 and hits[0]["_label"] == "retracted"
    db.close()


def test_empty_query_returns_all_active(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "a", "1", source_refs=SRC)
    remember(db, "s1", "b", "2", source_refs=SRC)
    assert len(recall(db, "s1", "")) == 2
    db.close()


def test_search_by_key(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "build.python_version", "3.12", source_refs=SRC)
    assert len(search_by_key(db, "s1", "build.python_version")) == 1
    assert search_by_key(db, "s1", "nope") == []
    db.close()


def test_search_by_key_drops_active_with_unusable_ancestor(tmp_path):
    # R2 D14: search_by_key() must apply the same usable() rule as recall()
    # — an active record whose ancestor is not active must never come back
    # as a plain usable active. Every public state change taints descendants,
    # so this state is built directly to simulate legacy data (a v1 DB
    # upgraded on open) or a missed propagation; the read path must not
    # depend on taint having run.
    db = _db(tmp_path)
    parent = remember(db, "s1", "parent.key", "parent value", source_refs=SRC)
    child = remember(db, "s1", "child.key", "child value", source_refs=SRC,
                     parent_ids=[parent["id"]])
    assert search_by_key(db, "s1", "child.key")[0]["state"] == "active"
    assert recall(db, "s1", "child value") != []
    db.execute("UPDATE memories SET state='erased' WHERE id=?", (parent["id"],))
    db.commit()
    assert recall(db, "s1", "child value") == []
    assert search_by_key(db, "s1", "child.key") == []
    hits = search_by_key(db, "s1", "child.key", include_history=True)
    assert len(hits) == 1 and hits[0].get("_label") == "unusable"
    db.close()
