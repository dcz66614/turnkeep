"""T007 constraint tests: FKs / unique / CHECKs must be enforced by SQLite."""
import sqlite3
import pytest
from turnkeep.storage import open_db


def _db(tmp_path):
    return open_db(tmp_path / "c.sqlite3")


def test_fk_enforced(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m1','nope','k','{}','fact','active',1,'t','t')")
    db.close()


def test_one_active_value_per_slot(tmp_path):
    db = _db(tmp_path)
    db.execute("INSERT INTO scopes(id, display_name, created_at) VALUES ('s','s','t')")
    db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m1','s','k','\"1\"','fact','active',1,'t','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m2','s','k','\"2\"','fact','active',1,'t','t')")
    rows = db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s' AND key='k' AND state='active'").fetchone()[0]
    assert rows == 1
    db.close()


def test_kind_and_state_checks(tmp_path):
    db = _db(tmp_path)
    db.execute("INSERT INTO scopes(id, display_name, created_at) VALUES ('s','s','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m','s','k','{}','nope','active',1,'t','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m','s','k','{}','fact','bogus',1,'t','t')")
    db.close()


def test_json_valid_check(tmp_path):
    db = _db(tmp_path)
    db.execute("INSERT INTO scopes(id, display_name, created_at) VALUES ('s','s','t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('m','s','k','not-json','fact','active',1,'t','t')")
    db.close()
