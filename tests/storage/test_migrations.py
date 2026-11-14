"""T007 migration tests (schema 002: record provenance; schema 003:
per-scope HMAC key for erasure needle hashes; schema 004: live-DB-only
erase_flags table for the blocked repair-reason labels; schema 005:
per-record erase verification keys, P0-R12-01; schema 006:
live-DB-only maintenance_pending markers, P0-R13-01; schema 007:
live-DB-only purge_pending markers, P0-R16-03)."""
import sqlite3
import threading
from pathlib import Path
import pytest
from turnkeep.storage import (open_db, migrate, SCHEMA_VERSION, data_path,
                              MIGRATIONS_DIR, record_provenance, get_provenance)
from turnkeep.errors import TurnkeepError


def test_fresh_db_is_v7(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 7
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("scopes", "memories", "sources", "memory_sources", "dependencies",
              "replacements", "runs", "observed_reads", "events", "idempotency",
              "import_batches", "schema_meta", "record_provenance",
              "scope_secrets", "erase_flags", "record_erase_keys",
              "maintenance_pending", "purge_pending"):
        assert t in tables
    indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "record_provenance_origin" in indexes
    # One schema_meta row per migration applied, in order.
    assert {r[0] for r in db.execute("SELECT version FROM schema_meta")} == {1, 2, 3, 4, 5, 6, 7}
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.close()


def _make_v1_db(path):
    """A database exactly as the v1 code left it: only 001 applied,
    user_version=1, one scope row so the upgrade must preserve data."""
    raw = sqlite3.connect(str(path))
    sql = (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
    raw.executescript(sql)
    raw.execute("INSERT INTO schema_meta(version, installed_at) VALUES (1, '2026-01-01T00:00:00+00:00')")
    raw.execute("INSERT INTO scopes(id, display_name, created_at) VALUES ('s', 's', '2026-01-01T00:00:00+00:00')")
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()


def test_v1_db_upgrades_to_v7_on_open(tmp_path):
    p = tmp_path / "v1.sqlite3"
    _make_v1_db(p)
    db = open_db(p)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "record_provenance" in tables
    assert "scope_secrets" in tables
    assert "erase_flags" in tables
    assert "record_erase_keys" in tables
    assert "maintenance_pending" in tables
    assert {r[0] for r in db.execute("SELECT version FROM schema_meta")} == {1, 2, 3, 4, 5, 6, 7}
    # Pre-existing data survives the upgrade.
    assert db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 1
    db.close()


def test_provenance_helpers_roundtrip(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    assert get_provenance(db, "s", "m1") is None
    record_provenance(db, "s", "m1", "orig-1", "other-scope")
    row = get_provenance(db, "s", "m1")
    assert row["scope_id"] == "s" and row["memory_id"] == "m1"
    assert row["origin_id"] == "orig-1" and row["origin_scope"] == "other-scope"
    assert row["imported_at"]
    # Re-import of the same package stays a no-op (one row, replaced in place).
    record_provenance(db, "s", "m1", "orig-1", "other-scope")
    assert db.execute("SELECT COUNT(*) FROM record_provenance").fetchone()[0] == 1
    # A different origin replaces the row rather than duplicating it.
    record_provenance(db, "s", "m1", "orig-2", "other-scope")
    row = get_provenance(db, "s", "m1")
    assert row["origin_id"] == "orig-2"
    assert db.execute("SELECT COUNT(*) FROM record_provenance").fetchone()[0] == 1
    db.close()


def test_unknown_future_version_refused(tmp_path):
    p = tmp_path / "f.sqlite3"
    db = open_db(p); db.close()
    raw = sqlite3.connect(str(p))
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
    raw.close()
    with pytest.raises(TurnkeepError) as ei:
        open_db(p)
    assert ei.value.code == "SCHEMA_VERSION"


def test_starter_format_refused(tmp_path):
    # Build a starter-shaped DB (source_refs_json column) and confirm refusal.
    p = tmp_path / "starter.sqlite3"
    raw = sqlite3.connect(str(p))
    raw.executescript("CREATE TABLE scopes(scope TEXT PRIMARY KEY, epoch INTEGER);"
                      "CREATE TABLE memories(id TEXT PRIMARY KEY, scope TEXT, key TEXT, value_json TEXT, kind TEXT, state TEXT, revision INTEGER, created_at TEXT, updated_at TEXT, source_refs_json TEXT);")
    raw.close()
    with pytest.raises(TurnkeepError) as ei:
        open_db(p)
    assert ei.value.code == "SCHEMA_VERSION"


def test_migration_failure_keeps_original(tmp_path):
    # A DB whose 001 script cannot apply (pre-existing conflicting table)
    # must fail without destroying the original file content.
    p = tmp_path / "bad.sqlite3"
    raw = sqlite3.connect(str(p))
    raw.execute("CREATE TABLE schema_meta(version INTEGER PRIMARY KEY, installed_at TEXT NOT NULL)")
    raw.execute("INSERT INTO schema_meta VALUES (99, 'x')")
    raw.commit(); raw.close()
    before = p.read_bytes()
    with pytest.raises(Exception):
        open_db(p)
    assert p.read_bytes() == before


def test_concurrent_first_open_all_succeed(tmp_path):
    # P1-e follow-up: two MCP servers opening the same fresh DB raced in
    # PRAGMA journal_mode=WAL ("database is locked" — the pragma does not
    # honour the busy handler). Eight simultaneous openers must all
    # succeed and all see schema v3 in WAL mode.
    p = tmp_path / "race.sqlite3"
    barrier = threading.Barrier(8)
    results, errors = [], []

    def opener():
        try:
            barrier.wait(timeout=30)
            db = open_db(p)
            results.append(db.execute("PRAGMA user_version").fetchone()[0])
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            db.close()
        except BaseException as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=opener) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert results == [SCHEMA_VERSION] * 8


def test_data_path_is_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("TURNKEEP_DATA_DIR", str(tmp_path / "data"))
    p1 = data_path("proj-a")
    assert str(p1).startswith(str(tmp_path))
    assert p1.name == "proj-a.sqlite3"
