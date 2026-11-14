"""T011 resolve_conflict tests: full candidate set or nothing."""
import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts
from turnkeep.corrections import resolve_conflict
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def _conflict(tmp_path):
    db = open_db(tmp_path / "r.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "k", "a", source_refs=SRC)
    remember(db, "s1", "k", "b", source_refs=SRC)
    return db


def test_partial_candidate_set_rejected(tmp_path):
    db = _conflict(tmp_path)
    group = list_conflicts(db, "s1", "k")
    epoch = scope_epoch(db, "s1")
    with pytest.raises(TurnkeepError) as ei:
        resolve_conflict(db, "s1", "k", [{"id": group[0]["id"], "revision": group[0]["revision"]}],
                         "a", source_refs=SRC)
    assert ei.value.code == "CANDIDATE_SET_CHANGED"
    assert scope_epoch(db, "s1") == epoch
    assert len(list_conflicts(db, "s1", "k")) == 2
    db.close()


def test_stale_candidate_revision_rejected(tmp_path):
    db = _conflict(tmp_path)
    group = list_conflicts(db, "s1", "k")
    cands = [{"id": r["id"], "revision": r["revision"] + 5} for r in group]
    with pytest.raises(TurnkeepError) as ei:
        resolve_conflict(db, "s1", "k", cands, "a", source_refs=SRC)
    assert ei.value.code == "CANDIDATE_SET_CHANGED"
    db.close()


def test_full_set_resolves_and_supersedes_all(tmp_path):
    db = _conflict(tmp_path)
    group = list_conflicts(db, "s1", "k")
    cands = [{"id": r["id"], "revision": r["revision"]} for r in group]
    r = resolve_conflict(db, "s1", "k", cands, "a", source_refs=SRC)
    assert r["state"] == "active"
    assert sorted(r["supersedes_ids"]) == sorted(x["id"] for x in group)
    assert list_conflicts(db, "s1", "k") == []
    db.close()
