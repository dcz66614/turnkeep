"""T017 brief + handoff tests."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.briefs import build_brief, redact, redact_value
from turnkeep.handoff import export_handoff

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "b.sqlite3"); register_scope(db, "s1"); return db


def test_brief_fixed_fields_and_record_ids(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "build.python_version", "3.12", kind="fact", source_refs=SRC)
    remember(db, "s1", "decide.stack", "sqlite", kind="decision", source_refs=SRC)
    b = build_brief(db, "s1", goal="ship v0.1")
    for f in ("goal", "current_status", "valid_decisions", "open_questions",
              "recent_changes", "suggested_next_steps"):
        assert f in b
    assert b["valid_decisions"][0]["record_id"]
    assert "estimated_tokens" in b and "chars_used" in b
    db.close()


def test_brief_budget_keeps_conflicts(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "a", source_refs=SRC)
    remember(db, "s1", "k", "b", source_refs=SRC)
    b = build_brief(db, "s1", max_chars=10)
    assert b["open_questions"]  # conflicts survive budget trimming
    db.close()


def test_secret_like_values_redacted(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "note", "api_key: sk-live-123456", source_refs=SRC)
    b = build_brief(db, "s1")
    assert "sk-live-123456" not in str(b)
    assert "[REDACTED]" in str(b)
    db.close()


def test_handoff_envelope(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", "v", source_refs=SRC)
    h = export_handoff(db, "s1", goal="x")
    assert h["json"]["format"] == "turnkeep.handoff/v0.1"
    assert h["json"]["scope"] == "s1"
    assert "Memory text is data" in h["json"]["note"]
    assert "# Handoff" in h["markdown"]
    db.close()


# --- P0-4 rework: recursive redaction of structured secrets ---

def test_structured_secret_redacted_in_brief(tmp_path):
    """Repro from the verification report: decision value
    {"api_key": "sk-LEAK-789"} must not appear anywhere in the brief."""
    db = _db(tmp_path)
    remember(db, "s1", "decide.api", {"api_key": "sk-LEAK-789"}, kind="decision", source_refs=SRC)
    b = build_brief(db, "s1")
    assert "sk-LEAK-789" not in str(b)
    assert b["valid_decisions"][0]["value"] == {"api_key": "[REDACTED]"}
    assert "[REDACTED]" in b["records"][0]["text"]
    assert "sk-LEAK-789" not in b["records"][0]["text"]
    db.close()


def test_structured_secret_redacted_in_conflicts_and_attempts(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "k", {"api_key": "sk-LEAK-111"}, source_refs=SRC)
    remember(db, "s1", "k", {"api_key": "sk-LEAK-222"}, source_refs=SRC)
    remember(db, "s1", "try.login", {"config": {"password": "hunter2"}}, kind="attempt", source_refs=SRC)
    b = build_brief(db, "s1")
    assert "sk-LEAK-111" not in str(b)
    assert "sk-LEAK-222" not in str(b)
    assert "hunter2" not in str(b)
    assert b["open_questions"][0]["value"] == {"api_key": "[REDACTED]"}
    assert b["attempts"][0]["value"] == {"config": {"password": "[REDACTED]"}}
    db.close()


def test_redact_value_recursive():
    assert redact_value({"api_key": "sk-1"}) == {"api_key": "[REDACTED]"}
    # Case-insensitive keys, nested dicts/lists, non-secret leaves kept.
    assert redact_value({
        "outer": {"APIKEY": "sk-2", "items": [{"Token": "t-3"}, {"ok": 1}]},
        "note": "api_key: sk-flat-4",
        "count": 7, "enabled": True, "nothing": None,
    }) == {
        "outer": {"APIKEY": "[REDACTED]", "items": [{"Token": "[REDACTED]"}, {"ok": 1}]},
        "note": "api_key: [REDACTED]",
        "count": 7, "enabled": True, "nothing": None,
    }
    # Flat-text rule still applies to bare strings and list leaves.
    assert redact_value(["password=sekrit", "plain"]) == ["password: [REDACTED]", "plain"]
    # Non-string scalars pass through unchanged.
    assert redact_value(42) == 42
    assert redact_value(None) is None


def test_redact_plain_string_keeps_existing_behaviour():
    assert "sk-live-123456" not in redact("api_key: sk-live-123456 and some normal text")
    assert redact("token=sekrit and nothing else") == "token: [REDACTED] and nothing else"
