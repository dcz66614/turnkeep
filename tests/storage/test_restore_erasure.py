"""T039 restore x erasure: a pre-erasure backup resurrects the payload.
That is honest point-in-time behaviour — and exactly why backups are
listed as uncontrolled residuals in T038, never claimed as deleted."""
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retrieval import recall
from turnkeep.erasure import erase_record
from turnkeep.backup import create_backup, restore_backup

SECRET = "SYNTHETIC-SECRET-backup-test"
SRC = [{"id": "u:1", "type": "user_statement"}]


def test_restore_before_erasure_resurrects_payload(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    register_scope(db, "s")
    rec = remember(db, "s", "ci.api_key", SECRET, kind="fact", source_refs=SRC)
    create_backup(db, tmp_path / "pre-erase.sqlite3")
    erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert not any(SECRET in str(r.get("value", "")) for r in recall(db, "s", "api_key"))
    db.close()
    # Restoring the pre-erasure backup brings the payload back. This is
    # expected: a backup is a point-in-time copy, not an erasure.
    restore_backup(tmp_path / "pre-erase.sqlite3", tmp_path / "r.sqlite3")
    db2 = open_db(tmp_path / "r.sqlite3")
    assert any(SECRET in str(r.get("value", "")) for r in recall(db2, "s", "api_key"))
    # Erasing again in the restored DB works and is verified the same way.
    rec2 = db2.execute("SELECT id, revision FROM memories WHERE key='ci.api_key'").fetchone()
    out = erase_record(db2, "s", rec2[0], rec2[1], "rotation")
    assert out["status"] == "COMPLETE"
    assert not any(SECRET in str(r.get("value", "")) for r in recall(db2, "s", "api_key"))
    db2.close()


def test_backup_after_erasure_stays_clean(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    register_scope(db, "s")
    rec = remember(db, "s", "ci.api_key", SECRET, kind="fact", source_refs=SRC)
    erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    create_backup(db, tmp_path / "post-erase.sqlite3")
    db.close()
    restore_backup(tmp_path / "post-erase.sqlite3", tmp_path / "r.sqlite3")
    db2 = open_db(tmp_path / "r.sqlite3")
    assert not any(SECRET in str(r.get("value", "")) for r in recall(db2, "s", "api_key"))
    db2.close()
