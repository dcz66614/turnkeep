"""T009 event atomicity: a rolled-back tx leaves no event and no receipt."""
import pytest
from turnkeep.storage import open_db, transaction, publish_event
from turnkeep.scopes import register_scope
from turnkeep.events import list_events


def test_rollback_leaves_no_event(tmp_path):
    db = open_db(tmp_path / "e.sqlite3")
    register_scope(db, "s1")
    try:
        with transaction(db, write=True):
            publish_event(db, "s1", "remember", "cli:local", {"r1": None})
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert list_events(db, "s1") == []
    assert db.execute("SELECT epoch FROM scopes WHERE id='s1'").fetchone()[0] == 0
    db.close()


def test_event_written_with_change_and_no_body(tmp_path):
    db = open_db(tmp_path / "e.sqlite3")
    register_scope(db, "s1")
    with transaction(db, write=True):
        publish_event(db, "s1", "remember", "cli:local", {"r1": None},
                      metadata={"key": "build.python_version"})
    evs = list_events(db, "s1")
    assert len(evs) == 1 and evs[0]["scope_epoch"] == 1
    assert evs[0]["affected_ids"] == ["r1"]
    assert "3.12" not in str(evs[0])  # no record bodies in events
    db.close()
