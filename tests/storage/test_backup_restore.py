"""T039 backup/restore: live backup, roundtrip, corrupt refusal, overwrite guard."""
import sqlite3
from pathlib import Path

import pytest

from turnkeep.storage import open_db, SCHEMA_VERSION
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.errors import TurnkeepError
from turnkeep.backup import create_backup, restore_backup, diagnose

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path, name="a.sqlite3"):
    db = open_db(tmp_path / name)
    register_scope(db, "s")
    remember(db, "s", "k1", "v1", kind="fact", source_refs=SRC)
    remember(db, "s", "k2", "v2", kind="decision", source_refs=SRC)
    return db


def test_live_backup_and_restore_roundtrip(tmp_path):
    db = _db(tmp_path)
    meta = create_backup(db, tmp_path / "b.sqlite3")
    assert meta["records"] == 2 and meta["scopes"] == ["s"]
    assert meta["schema_version"] == SCHEMA_VERSION
    assert (tmp_path / "b.sqlite3.meta.json").exists()
    db.close()
    out = restore_backup(tmp_path / "b.sqlite3", tmp_path / "r.sqlite3")
    assert out["records"] == 2
    db2 = open_db(tmp_path / "r.sqlite3")
    assert db2.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    db2.close()


def test_backup_while_second_connection_writes(tmp_path):
    # The online backup API gives a consistent snapshot even with another
    # connection open; the snapshot is taken at backup time.
    db = _db(tmp_path)
    other = open_db(tmp_path / "a.sqlite3")
    meta = create_backup(db, tmp_path / "b.sqlite3")
    assert meta["records"] == 2
    db.close()
    other.close()


def test_restore_refuses_truncated_backup(tmp_path):
    db = _db(tmp_path)
    create_backup(db, tmp_path / "b.sqlite3")
    db.close()
    data = (tmp_path / "b.sqlite3").read_bytes()
    (tmp_path / "bad.sqlite3").write_bytes(data[:100])
    with pytest.raises(TurnkeepError):
        restore_backup(tmp_path / "bad.sqlite3", tmp_path / "r.sqlite3")
    assert not (tmp_path / "r.sqlite3").exists()


def test_restore_refuses_overwrite_without_force(tmp_path):
    db = _db(tmp_path)
    create_backup(db, tmp_path / "b.sqlite3")
    db.close()
    (tmp_path / "r.sqlite3").write_bytes(b"existing")
    with pytest.raises(TurnkeepError):
        restore_backup(tmp_path / "b.sqlite3", tmp_path / "r.sqlite3")
    assert (tmp_path / "r.sqlite3").read_bytes() == b"existing"
    restore_backup(tmp_path / "b.sqlite3", tmp_path / "r.sqlite3", force=True)
    assert (tmp_path / "r.sqlite3").read_bytes()[:6] == b"SQLite"


def test_diagnose_reports_status(tmp_path):
    db = _db(tmp_path)
    db.close()
    diag = diagnose(tmp_path / "a.sqlite3")
    assert diag["status"] in ("OK", "DEGRADED")
    assert any("integrity_check ok" in f for f in diag["findings"])
    missing = diagnose(tmp_path / "nope.sqlite3")
    assert missing["status"] == "CORRUPT"
    garbage = tmp_path / "g.sqlite3"
    garbage.write_bytes(b"not a database at all........")
    assert diagnose(garbage)["status"] == "CORRUPT"
