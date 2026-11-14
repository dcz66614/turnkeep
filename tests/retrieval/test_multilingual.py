"""T016 multilingual tests: Chinese tokens, mixed code symbols, long values."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retrieval import recall, tokenise

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_chinese_tokenise():
    assert "你" in tokenise("你好世界")
    assert tokenise("你好世界") >= {"你", "好", "世", "界"}


def test_chinese_recall(tmp_path):
    db = open_db(tmp_path / "m.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "需求", "支持中文检索", kind="requirement", source_refs=SRC)
    remember(db, "s1", "other", "english only", source_refs=SRC)
    hits = recall(db, "s1", "中文检索")
    assert len(hits) == 1 and hits[0]["key"] == "需求"
    db.close()


def test_mixed_code_symbols(tmp_path):
    db = open_db(tmp_path / "m.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "build.python_version", "3.12", source_refs=SRC)
    hits = recall(db, "s1", "python_version 3.12")
    assert len(hits) == 1
    db.close()


def test_long_value_deterministic(tmp_path):
    db = open_db(tmp_path / "m.sqlite3"); register_scope(db, "s1")
    remember(db, "s1", "long", "x" * 10000, source_refs=SRC)
    hits = recall(db, "s1", "long")
    assert len(hits) == 1 and len(hits[0]["value"]) == 10000
    db.close()
