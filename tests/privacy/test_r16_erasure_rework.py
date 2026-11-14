"""R16 rework tests for P0-R15-01 (SQLite secure_delete not explicitly
enabled) — docs/KNOWN_LIMITATIONS.md §28.

P0-R15-01: open_db() never set PRAGMA secure_delete=ON, so on a
default-OFF SQLite build a deleted verification key's cell bytes stay
in the main DB file (checkpoint copies the freeblock page; VACUUM's
compact pages go to the WAL with no checkpoint after them) and the
PIN stays recoverable from an ordinary file read while erase reports
COMPLETE and the residual report reads clean. The fix: (1) open_db()
sets secure_delete=ON and reads it back, fail-closed; (2) a
fail-closed gate at both erase entries; (3) a file-level purge
acceptance (_verify_purge) before the marker is cleared; (4) an
explicit cleanup entry (purge_residue / `turnkeep purge-residue`).

Note on the OFF chain (test_off_chain_file_check_downgrades_repair):
with the gate + file check in place, GPT's R15 result table (repair
COMPLETE, marker 0, report clean, blind scan recovers 4827) is NO
LONGER reachable under the fixed code — the gate refuses an OFF erase
outright, and even with the gate bypassed the file check downgrades
the repair to PARTIAL. The pre-fix table is preserved in
~/workspace/r15-verification/repro_r15_verify.py (run against HEAD
d1750d8). This file locks the POST-fix behaviour of the same chain,
and the ON reverse chain (true clean).
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from turnkeep.storage import open_db, SCHEMA_VERSION
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue, _run_maintenance,
    _purge_failed, _maintenance_pending, _record_verification_keys,
    _stored_needle_hashes, _all_stored_needle_hashes, _probe_files,
    _verify_purge, _candidate_recovers_pin, _require_secure_delete,
)
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
PAYLOAD = "PAYLOAD-TOKEN-9f3a7c2e1b5d-UNIQUE-7741"
REPO_ROOT = Path(__file__).resolve().parents[2]
CRASH_CHILD = REPO_ROOT / "scripts" / "crash_child.py"
OFF_RESIDUE_CHILD = REPO_ROOT / "scripts" / "off_residue_child.py"


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _state(db, rid):
    return db.execute("SELECT state FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _value(db, rid):
    return db.execute("SELECT value_json FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _erase_event_count(db):
    return db.execute("SELECT COUNT(*) FROM events WHERE scope_id='s' "
                      "AND event_type='erase'").fetchone()[0]


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _marker_rows(db):
    return db.execute("SELECT COUNT(*) FROM maintenance_pending").fetchone()[0]


def _pending_ids(db):
    rep = residual_report(db, "s")
    return [p["record_id"] for p in rep["controlled"]["file_purge_pending"]]


def _blind_scan_file(path, db):
    """Recover the PIN from ONE file's bytes alone, with NO prior
    knowledge of any key: a sliding window over every >=64-char hex
    run collects candidate 32-byte keys, then each candidate is
    tried against the event needle hashes. Independent of the
    product probe (this is the verification's method)."""
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if not hashes:
        return None
    blob = Path(path).read_bytes() if Path(path).exists() else b""
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


def _blind_scan_main_wal(path, db):
    """(main-file PIN, WAL PIN) — the two files are different
    evidence and must be scanned separately, never only merged."""
    wal = str(path) + "-wal"
    return _blind_scan_file(path, db), _blind_scan_file(wal, db)


def _spawn_crash(db_path, record_id, revision, mode, point, reason=None):
    """Run scripts/crash_child.py as an independent subprocess; the
    expected exit code is 77 (crashed at the requested point)."""
    argv = [sys.executable, str(CRASH_CHILD), str(db_path), record_id,
            str(revision), mode, point]
    if reason is not None:
        argv.append(reason)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env)


def _spawn_off_residue(db_path, a_id, a_rev, b_id, b_rev):
    """Run scripts/off_residue_child.py as an independent subprocess;
    the expected exit code is 77 (crashed after the OFF chain, without
    closing)."""
    argv = [sys.executable, str(OFF_RESIDUE_CHILD), str(db_path), a_id,
            str(a_rev), b_id, str(b_rev)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env)


def _setup_pin(tmp_path, name):
    """Repair-path setup: two records sharing the PIN, B erased
    PARTIAL (A still holds the payload)."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    return db, path, a, b


def _converge_pin(db, path, a, b):
    """Erase A, repair B, and assert the report is clean and nothing
    is enumerable from the live files."""
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin is None
    assert wal_pin is None


# --- (1a)/(1d)(i): open_db() sets secure_delete=ON and reads it back ---

def test_open_db_sets_secure_delete_on(tmp_path):
    db = _db(tmp_path)
    assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    db.close()


def test_secure_delete_is_per_connection_not_persisted(tmp_path):
    """secure_delete is per-connection, NOT persisted in the file
    (R15 verification §1.1): an OFF setting on one connection does
    not survive a reopen — every open must set it again."""
    path = tmp_path / "t.sqlite3"
    db = open_db(path)
    register_scope(db, "s")
    db.execute("PRAGMA secure_delete=OFF")
    assert db.execute("PRAGMA secure_delete").fetchone()[0] == 0
    db.close()
    db2 = open_db(path)
    assert db2.execute("PRAGMA secure_delete").fetchone()[0] == 1
    db2.close()


# --- (1c)/(1d)(ii): the erase-entry gate refuses OFF, fail closed ---

def test_erase_refused_when_secure_delete_off_first_erase(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    rev = _rev(db, a["id"])
    db.execute("PRAGMA secure_delete=OFF")
    with pytest.raises(TurnkeepError) as exc:
        erase_record(db, "s", a["id"], rev, "rotation")
    assert exc.value.code == "SECURE_DELETE_OFF"
    # Nothing was written: the record keeps its pre-erase state, no
    # key row was created, no marker, no erase event.
    assert _state(db, a["id"]) == "active"
    assert _rev(db, a["id"]) == rev
    assert _value(db, a["id"]) == json.dumps(PIN)
    assert _key_rows(db) == 0
    assert _marker_rows(db) == 0
    assert _erase_event_count(db) == 0
    # Retryable: the same erase succeeds once the connection is ON.
    db.execute("PRAGMA secure_delete=ON")
    assert erase_record(db, "s", a["id"], rev, "rotation")["status"] == "COMPLETE"


def test_repair_refused_when_secure_delete_off(tmp_path):
    db, path, a, b = _setup_pin(tmp_path, "t")
    assert _key_rows(db) == 1
    db.execute("PRAGMA secure_delete=OFF")
    with pytest.raises(TurnkeepError) as exc:
        erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert exc.value.code == "SECURE_DELETE_OFF"
    # The repair never ran: B's key row is intact, no marker, no new
    # event, B still tombstoned at the same revision.
    assert _key_rows(db) == 1
    assert _marker_rows(db) == 0
    assert _erase_event_count(db) == 1
    assert _state(db, b["id"]) == "erased"
    db.execute("PRAGMA secure_delete=ON")
    assert erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")["status"] == "COMPLETE"


def test_gate_runs_before_the_transaction(tmp_path):
    """The check runs before any transaction, so a failing check can
    never delete the key first and then report the failure: even a
    repair of an already-erased record under OFF raises instead of
    touching anything."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    db.execute("PRAGMA secure_delete=OFF")
    with pytest.raises(TurnkeepError) as exc:
        E._try_repair_erased(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert exc.value.code == "SECURE_DELETE_OFF"
    assert _key_rows(db) == 1
    assert _marker_rows(db) == 0
    assert _erase_event_count(db) == 1


# --- (2a): the OFF chain, post-fix behaviour (gate bypassed) ---

def test_off_chain_file_check_downgrades_repair(tmp_path):
    """GPT's R15 chain under OFF with the gate bypassed (test-only
    monkeypatch, reproducing pre-fix behaviour): the file-level purge
    acceptance catches the residue the gate would have refused. The
    repair is PARTIAL with the key restored and the marker kept —
    never a false COMPLETE. The pre-fix table (repair COMPLETE,
    marker 0, report clean) is preserved in
    ~/workspace/r15-verification/repro_r15_verify.py."""
    path = tmp_path / "t.sqlite3"
    db = _db(tmp_path)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    db.execute("PRAGMA secure_delete=OFF")
    E._require_secure_delete = lambda _db: None
    try:
        out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
        assert out_b["status"] == "PARTIAL"
        # The first erase of A is also downgraded: under OFF its key
        # stays in the page freeblock and the checkpoint copies that
        # page into the main file, so the file check fails.
        out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
        assert out_a["status"] == "PARTIAL"
        assert any("file purge verification failed" in n
                   for n in out_a.get("maintenance", []))
        out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
        assert out_r["status"] == "PARTIAL"
        assert out_r.get("maintenance_pending") is True
        assert any("file purge verification failed" in n
                   for n in out_r.get("maintenance", []))
    finally:
        E._require_secure_delete = _require_secure_delete
    # Both keys restored, both markers standing, report NOT_CLEAN —
    # honest, not false-clean.
    assert _key_rows(db) == 2
    assert _marker_rows(db) == 2
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert sorted(_pending_ids(db)) == sorted([a["id"], b["id"]])
    # The independent blind scan still recovers 4827 from the main
    # file — but via the LIVE (restored) keys, not residue: the
    # product probe skips live keys and correctly finds no residue.
    # The WAL recovers it too (VACUUM's pages carry the restored key
    # rows) — same reason, not residue.
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin == PIN
    assert wal_pin == PIN
    assert _probe_files(db, _all_stored_needle_hashes(db)) is False


def test_on_chain_true_clean(tmp_path):
    """(2a) reverse: the same chain under product code (open_db()
    forces ON) ends truly clean — main file + WAL blind scans find
    nothing, the report is clean."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    _converge_pin(db, path, a, b)


# --- (1d)(iii)/(3): normal COMPLETE under ON does not regress ---

def test_first_erase_complete_under_on(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    out = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out["status"] == "COMPLETE"
    assert out["maintenance_pending"] is False
    assert _marker_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []


def test_file_check_does_not_trip_on_live_keys(tmp_path):
    """The file check skips live keys: B's PARTIAL key and the legacy
    scope key are still in the file and still verify hashes, so
    erasing A (ON) must still reach COMPLETE."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    assert out_a["maintenance_pending"] is False
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"


def test_candidate_recovers_pin_matches_hmac_module():
    """The manual HMAC-SHA256 in _candidate_recovers_pin agrees with
    hmac.new on a known vector, and rejects a wrong key.

    R16 rework (P1-R16-02): the probe answers a boolean only — the
    recovered PIN is never returned, so the equivalence assertion
    is "recovers / does not recover", not the value itself."""
    key = bytes.fromhex("ab" * 32)
    hashes = {hmac.new(key, b"4827", hashlib.sha256).hexdigest()}
    assert _candidate_recovers_pin(key, hashes) is True
    assert _candidate_recovers_pin(bytes.fromhex("cd" * 32), hashes) is False
    assert _candidate_recovers_pin(key, set()) is False


# --- (3): mid-transaction crash point (GPT's R15 extra check) ---

def test_mid_delete_crash_rolls_back(tmp_path):
    """Crash inside the erase transaction, after the marker write and
    before the key deletion: atomic rollback — marker 0 rows, key
    row intact, record unchanged, report CONTROLLED_NOT_CLEAN."""
    for mode in ("repair", "first"):
        name = f"t-{mode}"
        if mode == "repair":
            db, path, a, b = _setup_pin(tmp_path, name)
            rid, rev = b["id"], _rev(db, b["id"])
        else:
            # A single record: its first erase must reach COMPLETE for
            # the key deletion (the crash point) to run — a second
            # record holding the payload would make the scan PARTIAL.
            path = tmp_path / f"{name}.sqlite3"
            db = _db(tmp_path, name)
            b = remember(db, "s", "b-key", PIN, source_refs=SRC)
            rid, rev = b["id"], _rev(db, b["id"])
        db.close()
        res = _spawn_crash(path, rid, rev, mode, "mid_delete")
        assert res.returncode == 77, res.stderr
        db = open_db(path)
        # The transaction never committed: the marker write rolled
        # back with it, so there is no marker and no key deletion.
        assert _marker_rows(db) == 0
        if mode == "repair":
            assert _key_rows(db) == 1
            assert _state(db, rid) == "erased"
            assert _erase_event_count(db) == 1
            # The first PARTIAL erase left a controlled residual (A
            # still holds the payload), so the report is not clean.
            rep = residual_report(db, "s")
            assert rep["controlled"]["live_tables_clean"] is False
        else:
            assert _key_rows(db) == 0
            assert _state(db, rid) == "active"
            assert _erase_event_count(db) == 0
        db.close()


def test_repair_after_post_commit_crash_converges(tmp_path):
    """The probe-only branch (key_row is None): after a crash between
    the key-deletion commit and the checkpoint, the repair re-run
    has no key hex to check directly — the blind probe over the
    historical hashes must still pass under ON, so the repair
    converges to COMPLETE instead of looping PARTIAL."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    b_rev = _rev(db, b["id"])
    db.close()
    res = _spawn_crash(path, b["id"], b_rev, "repair", "post_commit")
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    assert _key_rows(db) == 0
    assert _marker_rows(db) == 1
    out = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out["status"] == "COMPLETE"
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []


# --- (4): historical-residue cleanup entry ---

def test_off_residue_fixture_and_purge_residue(tmp_path):
    """(4d) F0: an OFF chain crashed without closing leaves a deleted
    key recoverable from the main file (GPT's table: key rows 0,
    marker 0, report clean — false-clean — main-file blind scan
    recovers 4827, WAL finds nothing). purge_residue (ON + VACUUM +
    checkpoint + probe) clears it and reports truly clean."""
    path = tmp_path / "t.sqlite3"
    db = _db(tmp_path)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    db.close()
    res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev)
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    assert _key_rows(db) == 0
    assert _marker_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True  # false-clean
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin == PIN
    assert wal_pin is None
    out = purge_residue(db)
    assert out["cleaned"] is True
    # R16 rework (P1-R16-02): the probe answers booleans only — the
    # old probe_before/probe_after keys (which held the recovered
    # PIN) are gone; residue_before is True because the residue was
    # there before the cleanup, residue_after False after it.
    assert out["residue_before"] is True
    assert out["residue_after"] is False
    assert "probe_before" not in out and "probe_after" not in out
    assert out["error"] is None
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin is None
    assert wal_pin is None
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True  # truly clean
    assert rep["controlled"]["file_purge_pending"] == []
    db.close()


def test_checkpoint_only_does_not_clear_no_vacuum_residue(tmp_path):
    """(4d) F4: a no-VACUUM residue DB (direct DELETE of the key row
    under OFF + checkpoint) is NOT cleared by checkpoint-only under
    ON, but IS cleared by purge_residue (ON + VACUUM + checkpoint)."""
    path = tmp_path / "t.sqlite3"
    db = _db(tmp_path)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    # Direct DELETE of B's key row under OFF, then checkpoint (no
    # VACUUM): the deleted cell stays in the page freeblock and the
    # checkpoint copies that page into the main file.
    db.execute("PRAGMA secure_delete=OFF")
    db.execute("DELETE FROM record_erase_keys WHERE scope_id='s' AND record_id=?",
               (b["id"],))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    db.execute("PRAGMA secure_delete=ON")
    assert _blind_scan_file(path, db) == PIN
    # Checkpoint-only under ON does not clear it (the WAL holds no
    # cleaner pages to copy over the residue page).
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert _blind_scan_file(path, db) == PIN
    out = purge_residue(db)
    assert out["cleaned"] is True
    assert out["residue_before"] is True
    assert out["residue_after"] is False
    assert "probe_before" not in out and "probe_after" not in out
    assert _blind_scan_file(path, db) is None
    db.close()


def test_purge_residue_idempotent_on_clean_db(tmp_path):
    """(4b) Running the cleanup on a clean DB is safe and idempotent:
    no payload loss, cleaned both times."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    _converge_pin(db, path, a, b)
    for _ in range(2):
        out = purge_residue(db)
        assert out["cleaned"] is True
        assert out["residue_before"] is False
        assert out["residue_after"] is False
        assert out["error"] is None
    # No payload loss: the tombstones and the report are unchanged.
    assert _state(db, a["id"]) == "erased"
    assert _state(db, b["id"]) == "erased"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_purge_residue_reports_not_clean_when_probe_still_finds(tmp_path):
    """(4b) A cleanup that cannot clear the residue must report NOT
    CLEAN, never lie: with the probe forced to keep finding the PIN,
    cleaned is False."""
    db, path, a, b = _setup_pin(tmp_path, "t")
    _converge_pin(db, path, a, b)
    real_probe = E._probe_files
    E._probe_files = lambda _db, _hashes: True
    try:
        out = purge_residue(db)
    finally:
        E._probe_files = real_probe
    assert out["cleaned"] is False
    assert out["residue_after"] is True
    db.close()


def test_purge_residue_cli(tmp_path, capsys):
    """`turnkeep purge-residue --db PATH` exits 0 when clean, 2 when
    residue remains. R16 rework (P1-R16-02): stdout must never
    contain the erased PIN — the probe answers booleans only, so
    the residue keys are residue_before/residue_after and the raw
    output is asserted free of the PIN in every case below."""
    from turnkeep.cli import main as cli_main
    db, path, a, b = _setup_pin(tmp_path, "t")
    _converge_pin(db, path, a, b)
    db.close()
    assert cli_main(["--db", str(path), "--json", "purge-residue"]) == 0
    raw = capsys.readouterr().out
    assert PIN not in raw
    out = json.loads(raw)
    assert out["cleaned"] is True
    # A residue DB is cleaned and exits 0 — and its stdout must not
    # re-disclose the erased PIN.
    path2 = tmp_path / "t2.sqlite3"
    db2 = _db(tmp_path, "t2")
    a2 = remember(db2, "s", "a-key", PIN, source_refs=SRC)
    b2 = remember(db2, "s", "b-key", PIN, source_refs=SRC)
    a2_rev, b2_rev = _rev(db2, a2["id"]), _rev(db2, b2["id"])
    db2.close()
    res = _spawn_off_residue(path2, a2["id"], a2_rev, b2["id"], b2_rev)
    assert res.returncode == 77, res.stderr
    assert cli_main(["--db", str(path2), "--json", "purge-residue"]) == 0
    raw2 = capsys.readouterr().out
    assert PIN not in raw2
    out2 = json.loads(raw2)
    assert out2["cleaned"] is True
    assert out2["residue_before"] is True
    # A cleanup that cannot clear the residue exits 2, never 0 —
    # and its stdout still must not contain the PIN.
    real_probe = E._probe_files
    E._probe_files = lambda _db, _hashes: True
    try:
        assert cli_main(["--db", str(path), "--json", "purge-residue"]) == 2
        raw3 = capsys.readouterr().out
    finally:
        E._probe_files = real_probe
    assert PIN not in raw3
    out3 = json.loads(raw3)
    assert out3["cleaned"] is False
    assert out3["residue_after"] is True
    # No --db and no --scope is an input error, not a silent default.
    assert cli_main(["purge-residue"]) == 2


# --- (5): docs — §27 TBD rewritten, §28 added, purge assumption fixed ---

def test_known_limitations_sections_27_28():
    """(5a)/(5b) §27's TBD is rewritten, §28 records the R15 rework,
    and the purge assumption is stated with secure_delete=ON as its
    premise — no stale wording for the next round to misread."""
    text = (REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    assert "TBD after R15 verdict" not in text
    sec28 = text.split("## 28.")[1]
    assert "TBD" not in sec28
    for needle in (
        "P0-R15-01", "secure_delete", "PRAGMA secure_delete=ON",
        "purge_residue", "purge-residue",
        "per-connection", "not persisted",
        "F0", "F4", "checkpoint-only",
        "the recoverable material is the deleted verification KEY",
        "OFFLINE_RC_READY", "PUBLIC_CANDIDATE NOT MET",
    ):
        assert needle in sec28, needle
    sec27 = text.split("## 27.")[1].split("## 28.")[0]
    assert "P0-R15-01" in sec27
    assert "secure_delete" in sec27
    sec26 = text.split("## 26.")[1].split("## 27.")[0]
    assert "secure_delete=ON" in sec26
    sec = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "TBD" not in sec


def test_schema_v7_with_purge_pending_migration():
    """R16 rework (P0-R16-03) adds migration 007 (live-DB-only
    purge_pending table): schema v7, not v6. This is the conforming
    update of the R15 pin — the R15 fix itself changed no schema."""
    assert SCHEMA_VERSION == 7
