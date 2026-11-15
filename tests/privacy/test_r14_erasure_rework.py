"""R14 rework regression tests: P0-R13-01 (a busy WAL checkpoint must
not be reported as maintenance success), P2-R13-02 (SECURITY.md §5
T042 + T026 status unification).

Fix rules (verification report §3):
  1. _run_maintenance reads the PRAGMA wal_checkpoint(TRUNCATE)
     return triple (busy, log, checkpointed); busy != 0 is a
     maintenance FAILURE whatever checkpointed says — (1, 83, 83),
     checkpointed == log, is still a failure (the frames were copied
     but the WAL cannot be reset while a read mark is held). The
     existing PARTIAL downgrade path, which could never trigger
     before, now triggers.
  2. On a busy checkpoint at a terminal COMPLETE erase the key is
     RESTORED (captured before deletion in the erase transaction, so
     the exact same key goes back) and a maintenance_pending marker
     is persisted in the live-DB-only table (schema v6); the erase
     returns PARTIAL with maintenance_pending=True. Convergence is
     via a later repair re-run, which retries the checkpoint and
     clears the marker on success. No in-call retry loop: a reader
     held indefinitely would block cleanup indefinitely, and the
     honest response is PARTIAL + the marker + a clear remedy
     (release the reader, then re-run repair).
  3. residual_report() distinguishes logical-table-clean from
     file-cleanup-incomplete: live_tables_clean is True only when the
     tombstone structure is intact AND no persisted unresolved
     controlled residual AND no maintenance_pending marker stands
     (definition extended explicitly, not silently); the pending
     records are listed separately as file_purge_pending, and the
     top-level status becomes CONTROLLED_NOT_CLEAN while any stands.
  4. VACUUM is NOT the purge action: in this stack's WAL mode it
     compacts the b-trees (page_count drops, freelist -> 0) but does
     not shrink the file and removes no deleted bytes beyond what
     the checkpoint already removed — a delete + checkpoint(busy=0)
     already leaves 0/250 deleted-row markers in the live file — so
     its success says nothing about purge, and a VACUUM failure
     alone never fails the purge verdict. The maintenance checkpoint
     waits only 1 s (_MAINTENANCE_CHECKPOINT_TIMEOUT_MS) instead of
     the connection's 10 s busy_timeout, which is restored afterwards.
  5. SECURITY.md §5: T042 VERIFIED (two independent clean live runs,
     finding MIXED) and T026 VERIFIED (hybrid substitution accepted;
     strict live-clients gate still NOT_RUN).
"""
import hashlib
import hmac
import json
import re
import sqlite3
from pathlib import Path

import pytest

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.erasure import (
    erase_record, residual_report, _run_maintenance, _purge_failed,
    _maintenance_pending,
)
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN from the live DB's stored
    material: EVERY key the DB holds against the persisted needle
    hashes. Returns the recovered value, or None."""
    keys = []
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if not keys or not hashes:
        return None
    for i in range(10000):
        cand = f"{i:04d}"
        for key in keys:
            if hmac.new(key, cand.encode(), hashlib.sha256).hexdigest() in hashes:
                return cand
        if hashlib.sha256(cand.encode()).hexdigest() in hashes:
            return cand
    return None


def _file_byte_enumerate(db_path, db):
    """Recover the PIN from the live DB file + WAL bytes alone, with NO
    prior knowledge of any key: a sliding window over every >=64-char
    hex run collects candidate 32-byte keys (a fixed 64-char regex
    window misaligns — the key hex nests inside longer hex strings),
    then each candidate is tried against the event needle hashes."""
    blob = Path(db_path).read_bytes()
    wal = Path(str(db_path) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if not hashes:
        return None
    cands = set()
    for m in re.finditer(rb"[0-9a-f]{64,}", blob):
        run = m.group(0).decode()
        for off in range(len(run) - 63):
            cands.add(run[off:off + 64])
    for cand in sorted(cands):
        try:
            key = bytes.fromhex(cand)
        except ValueError:
            continue
        for i in range(10000):
            c = f"{i:04d}"
            if hmac.new(key, c.encode(), hashlib.sha256).hexdigest() in hashes:
                return c
    return None


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _hold_reader(db_path, variant):
    """A second connection holding a long read transaction. In the
    count-only variant it never reads the key table — any long read
    transaction blocks the checkpoint, not just one reading keys."""
    db2 = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    db2.execute("BEGIN")
    if variant == "read-key":
        db2.execute("SELECT secret_hex FROM record_erase_keys").fetchall()
    else:
        db2.execute("SELECT COUNT(*) FROM memories").fetchone()
    return db2


@pytest.mark.parametrize("variant", ["read-key", "count-only"])
def test_busy_checkpoint_chain_both_reader_variants(tmp_path, variant):
    # GPT's A1 chain, both second-connection constructions (verified
    # equivalent): any long read transaction blocks the checkpoint.
    path = tmp_path / f"a-{variant}.sqlite3"
    db = _db(tmp_path, f"a-{variant}")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")["status"] == "PARTIAL"
    db2 = _hold_reader(path, variant)
    try:
        out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
        assert out_a["status"] == "PARTIAL"
        assert any("wal_checkpoint" in n and "busy" in n for n in out_a["maintenance"])
        assert out_a["maintenance_pending"] is True
        out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
        assert out_r["status"] == "PARTIAL"
        assert out_r["maintenance_pending"] is True
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        assert rep["status"] == "CONTROLLED_NOT_CLEAN"
        assert {p["record_id"] for p in rep["controlled"]["file_purge_pending"]} == {a["id"], b["id"]}
        # The key is restored, so enumeration still finds the PIN —
        # honest, not clean: the report says so.
        assert _key_rows(db) == 2
        assert _enumerate_pin(db) == PIN
        # The marker itself is live-DB-only and carries no payload.
        assert _maintenance_pending(db, "s")
        assert all("4827" not in p["detail"] for p in _maintenance_pending(db, "s"))
    finally:
        db2.execute("COMMIT")
        db2.close()
    # Reader released: a repair re-run retries the checkpoint.
    out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r2["status"] == "COMPLETE"
    assert out_r2["maintenance"] == []
    assert out_r2["maintenance_pending"] is False
    out_a2 = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_a2["status"] == "COMPLETE"
    assert out_a2["maintenance_pending"] is False
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert rep2["controlled"]["file_purge_pending"] == []
    assert _key_rows(db) == 0
    assert _enumerate_pin(db) is None
    assert _file_byte_enumerate(str(path), db) is None
    db.close()


def test_run_maintenance_reports_busy(tmp_path):
    db = _db(tmp_path, "m")
    path = tmp_path / "m.sqlite3"
    db2 = _hold_reader(path, "count-only")
    try:
        notes = _run_maintenance(db)
        assert len(notes) == 1
        assert "wal_checkpoint" in notes[0] and "busy" in notes[0]
        assert _purge_failed(notes) is True
    finally:
        db2.execute("COMMIT")
        db2.close()
    assert _run_maintenance(db) == []
    db.close()


def test_purge_failed_counts_checkpoint_only():
    assert _purge_failed([]) is False
    assert _purge_failed(["VACUUM failed: database is locked"]) is False
    assert _purge_failed(["PRAGMA wal_checkpoint(TRUNCATE) busy: (1, 144, 42)"]) is True
    assert _purge_failed(["PRAGMA wal_checkpoint(TRUNCATE) failed: boom"]) is True


def test_busy_timeout_restored_after_maintenance(tmp_path):
    db = _db(tmp_path, "t")
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    _run_maintenance(db)
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    db.close()


def test_no_reader_complete_path_unchanged(tmp_path):
    # GPT acceptance item 5: the normal no-concurrent-reader COMPLETE
    # path must not regress.
    db = _db(tmp_path, "c")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "PARTIAL"
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    assert out_b["status"] == "COMPLETE"
    assert out_b["maintenance"] == []
    assert out_b["maintenance_pending"] is False
    out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance"] == []
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    assert _key_rows(db) == 0
    assert _enumerate_pin(db) is None
    db.close()


def test_pending_marker_never_exported(tmp_path):
    # The marker is live-DB-only: no checkpoint detail (and no key
    # material) may reach an export.
    path = tmp_path / "e.sqlite3"
    db = _db(tmp_path, "e")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")["status"] == "PARTIAL"
    db2 = _hold_reader(path, "count-only")
    try:
        out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
        assert out_a["status"] == "PARTIAL"
        assert _maintenance_pending(db, "s")
        out_dir = tmp_path / "export"
        export_scope(db, "s", out_dir)
        for name in ("events.jsonl", "records.jsonl", "manifest.json"):
            text = (out_dir / name).read_text(encoding="utf-8")
            assert "wal_checkpoint" not in text
            assert "maintenance_pending" not in text
    finally:
        db2.execute("COMMIT")
        db2.close()
    db.close()


def test_security_md_t042_t026_verified():
    # P2-R13-02: SECURITY.md §5 must match the manifest / handoff —
    # T042 VERIFIED (two independent clean live runs, finding MIXED)
    # and T026 VERIFIED (hybrid substitution accepted); the strict
    # live-clients gate itself is still NOT_RUN.
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())  # §5 bullet wraps across lines
    assert "Real-model evaluation VERIFIED (T042)" in flat
    assert "two-client live handoff VERIFIED (T026)" in flat
    assert "strict live-clients gate itself is still NOT_RUN" in flat
    assert "Real-model evaluation NOT_RUN (T042)" not in flat
    assert "two-client live handoff NOT_RUN (T026)" not in flat


def test_fresh_db_bak_holds_no_payload(tmp_path):
    # A fresh DB's .pre-v7.bak is written by open_db before any
    # payload exists, so it cannot hold one.
    p = tmp_path / "fresh.sqlite3"
    db = open_db(p)
    register_scope(db, "s")
    remember(db, "s", "k", "SENSITIVE-PAYLOAD-7f3a", source_refs=SRC)
    bak = Path(str(p) + ".pre-v7.bak")
    assert bak.exists()
    assert b"SENSITIVE-PAYLOAD-7f3a" not in bak.read_bytes()
    db.close()


def test_populated_upgrade_writes_no_bak(tmp_path):
    # A populated v5 DB upgraded to v7 must NOT gain a backup copy of
    # the populated file: _backup_before_upgrade runs only when the
    # migrating DB is version 0 (fresh, payload-free).
    p = tmp_path / "up.sqlite3"
    db = open_db(p)
    register_scope(db, "s")
    remember(db, "s", "k", "SENSITIVE-PAYLOAD-7f3a", source_refs=SRC)
    db.close()
    bak = Path(str(p) + ".pre-v7.bak")
    assert bak.exists()
    before = bak.read_bytes()
    before_mtime = bak.stat().st_mtime_ns
    raw = sqlite3.connect(str(p))
    raw.execute("PRAGMA user_version=5")
    raw.execute("DROP TABLE maintenance_pending")
    raw.execute("DROP TABLE purge_pending")
    raw.execute("DELETE FROM schema_meta WHERE version=6")
    raw.execute("DELETE FROM schema_meta WHERE version=7")
    raw.commit()
    raw.close()
    db = open_db(p)
    # R16 rework (P0-R16-03): migration 007 bumps the schema to v7.
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert bak.read_bytes() == before
    assert bak.stat().st_mtime_ns == before_mtime
    assert b"SENSITIVE-PAYLOAD-7f3a" not in bak.read_bytes()
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 1
    db.close()


def test_planted_bak_listed_uncontrolled(tmp_path):
    p = tmp_path / "plant.sqlite3"
    db = open_db(p)
    register_scope(db, "s")
    planted = Path(str(p) + ".pre-v6.bak")
    planted.write_bytes(b"planted backup copy holding SENSITIVE-PAYLOAD-7f3a")
    rep = residual_report(db, "s")
    assert any(str(planted) in u.get("path", "") for u in rep["uncontrolled"])
    db.close()


def test_vacuum_adds_no_purge_beyond_checkpoint(tmp_path):
    # (1f): VACUUM is not the purge action. In this stack's WAL mode
    # it compacts the b-trees (page_count drops, freelist -> 0) but
    # does NOT shrink the file, and it removes no deleted bytes
    # beyond what the checkpoint already removed: a delete +
    # checkpoint(busy=0) already leaves 0/250 deleted-row markers in
    # the live file. (The verification report's "VACUUM leaves file
    # bytes completely unchanged" was a tiny-DB artifact in one
    # respect — page_count does move — but its conclusion stands:
    # VACUUM is not the purge action. A DELETE-journal-mode control
    # does shrink under VACUUM; this stack runs WAL, so that does
    # not apply.)
    path = tmp_path / "v.sqlite3"
    db = _db(tmp_path, "v")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(500):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"ROW-MARKER-{i:04d}-" + "x" * 200))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("DELETE FROM t WHERE id % 2 = 0")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = path.read_bytes()
    assert sum(f"ROW-MARKER-{i:04d}".encode() in raw for i in range(0, 500, 2)) == 0
    assert sum(f"ROW-MARKER-{i:04d}".encode() in raw for i in range(1, 500, 2)) == 250
    # VACUUM on a second table whose pages are wholly freed: it
    # compacts the b-trees (page_count drops, freelist -> 0) but does
    # not shrink the file in WAL mode.
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(500):
        db.execute("INSERT INTO u VALUES (?, ?)", (i, "y" * 200))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("DELETE FROM u")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert db.execute("PRAGMA freelist_count").fetchone()[0] >= 1
    size_before = path.stat().st_size
    pages_before = db.execute("PRAGMA page_count").fetchone()[0]
    db.execute("VACUUM")
    assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert db.execute("PRAGMA page_count").fetchone()[0] < pages_before
    assert path.stat().st_size == size_before  # no shrink in WAL mode
    # The purge itself rests on the checkpoint, not VACUUM: a deleted
    # record_erase_keys row's bytes are gone from the live file after
    # a checkpoint, before VACUUM ever runs.
    db.execute("INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at) "
               "VALUES ('s','r1',?,?)", ("ab" * 32, "2026-11-12T00:00:00"))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert ("ab" * 32).encode() in path.read_bytes()
    db.execute("DELETE FROM record_erase_keys WHERE scope_id='s' AND record_id='r1'")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert ("ab" * 32).encode() not in path.read_bytes()
    db.close()
