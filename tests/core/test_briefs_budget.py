"""R2 (D13) brief-budget regression: valid_decisions / open_questions /
attempts are emitted as full structured values, so they must count in
the budget accounting — previously trim_to_budget() only ever saw the
rendered record text and chars_used / budget_overrun ignored them
(repro: 3,000-char decision + 3,000-char attempt at max_chars=4000
reported chars_used=3109, over_budget=False, while the serialised
brief was 10,123 chars)."""
import json

from turnkeep.briefs import build_brief
from turnkeep.memory import remember
from turnkeep.scopes import register_scope
from turnkeep.storage import open_db

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "b.sqlite3")
    register_scope(db, "s1")
    return db


def test_long_decision_reports_overrun(tmp_path):
    """Verification repro: 3,000-char decision value at max_chars=4000
    must report an overrun (it cannot honestly fit in the budget)."""
    db = _db(tmp_path)
    remember(db, "s1", "decide.big", "x" * 3000, kind="decision", source_refs=SRC)
    remember(db, "s1", "try.big", "y" * 3000, kind="attempt", source_refs=SRC)
    b = build_brief(db, "s1", max_chars=4000)
    assert b["budget_overrun"] is True
    assert b["chars_used"] > b["max_chars"]
    # The reported size must actually cover the structured sections,
    # not just the trimmed record list: the serialised brief is ~10k
    # chars, so chars_used must be in the same ballpark, not ~3k.
    assert b["chars_used"] > 8000
    assert len(json.dumps(b, ensure_ascii=False)) > b["max_chars"]
    db.close()


def test_long_conflict_value_counts_towards_budget(tmp_path):
    """Open questions (conflicts) are emitted in full too: a 3,000-char
    conflict value at max_chars=4000 must also report an overrun."""
    db = _db(tmp_path)
    remember(db, "s1", "k", "a" * 3000, source_refs=SRC)
    remember(db, "s1", "k", "b" * 3000, source_refs=SRC)
    b = build_brief(db, "s1", max_chars=4000)
    assert b["open_questions"]  # conflicts survive budget trimming
    assert b["budget_overrun"] is True
    assert b["chars_used"] > b["max_chars"]
    db.close()


def test_small_brief_stays_within_budget(tmp_path):
    """No false overrun: a small brief must still report within budget."""
    db = _db(tmp_path)
    remember(db, "s1", "decide.stack", "sqlite", kind="decision", source_refs=SRC)
    b = build_brief(db, "s1", max_chars=4000)
    assert b["budget_overrun"] is False
    assert b["chars_used"] <= b["max_chars"]
    db.close()
