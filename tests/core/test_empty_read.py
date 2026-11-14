"""T013 empty-read tests: a run that read nothing still records the epoch."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_empty_read_then_new_fact_invalidates(tmp_path):
    db = open_db(tmp_path / "e.sqlite3"); register_scope(db, "s1")
    run = begin(db, "s1", "work with no results yet")
    assert run["records"] == []
    remember(db, "s1", "new", "fact", source_refs=SRC)  # phantom: new fact appears
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], {"key": "out", "value": "x"})
    assert ei.value.code == "STALE_SNAPSHOT"
    assert "new" in ei.value.details.get("changed_keys", [])
    db.close()
