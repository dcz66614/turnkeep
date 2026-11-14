"""T017 handoff tests (separate file per backlog owned_paths)."""
import json
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.handoff import export_handoff

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_handoff_unknown_goal_not_invented(tmp_path):
    db = open_db(tmp_path / "h.sqlite3"); register_scope(db, "s1")
    h = export_handoff(db, "s1")
    assert h["json"]["brief"]["goal"] == "unknown"
    assert h["json"]["brief"]["current_status"] == "unknown"
    db.close()


def test_handoff_structured_secret_redacted(tmp_path):
    """P0-4 repro: decision value {"api_key": "sk-LEAK-789"} must appear
    nowhere in the JSON envelope or the Markdown export."""
    db = open_db(tmp_path / "h.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "decide.api", {"api_key": "sk-LEAK-789"}, kind="decision", source_refs=SRC)
    remember(db, "s1", "k", {"api_key": "sk-LEAK-111"}, source_refs=SRC)
    remember(db, "s1", "k", {"api_key": "sk-LEAK-222"}, source_refs=SRC)
    h = export_handoff(db, "s1", goal="x")
    assert "sk-LEAK-789" not in json.dumps(h["json"], ensure_ascii=False)
    assert "sk-LEAK-789" not in h["markdown"]
    assert "sk-LEAK-111" not in json.dumps(h["json"], ensure_ascii=False)
    assert "sk-LEAK-222" not in h["markdown"]
    assert "[REDACTED]" in h["markdown"]
    assert h["json"]["brief"]["valid_decisions"][0]["value"] == {"api_key": "[REDACTED]"}
    db.close()
