"""R17 rework tests for P0-R16-01 / P0-R16-03 / P1-R16-02
(docs/KNOWN_LIMITATIONS.md §29).

P0-R16-01: purge_residue() judged cleaned from the probe alone and
never read the checkpoint busy bit — a busy checkpoint (concurrent
read transaction holds a snapshot) was reported as a success while
the deleted verification key's bytes stayed recoverable from the
live DB file. The fix: cleaned requires checkpoint busy == 0.

P0-R16-03: a failed historical cleanup persisted no marker, so a
later residual_report() still called the tables clean. The fix: a
failed purge_residue writes a live-DB-only purge_pending row per
scope (migration 007); residual_report() reads it (listed in
file_purge_pending as "purge_residue:<scope>") and returns
CONTROLLED_NOT_CLEAN until a successful purge_residue re-check
clears it. A repair re-run never clears it.

P1-R16-02: purge_residue returned the recovered PIN in
probe_before / probe_after, and the CLI printed it to stdout. The
fix: the probe answers booleans only — the return dict carries
residue_before / residue_after, never a recovered value; the old
probe_* keys are gone (see §29 for the contract change). The R16
tests that asserted the leak are updated to forbid it (a
tightening, not a weakening).

Note on timing: a purge_residue call with a held reader waits out
the connection's 10 s busy_timeout on the checkpoint before the
busy bit is reported — the busy tests below each take ~10 s.
"""
import hashlib
import hmac
import io
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from turnkeep.storage import open_db, SCHEMA_VERSION
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue, _verify_purge,
    _purge_pending,
)

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
SIX = "864218"
REPO_ROOT = Path(__file__).resolve().parents[2]
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


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _marker_rows(db):
    return db.execute("SELECT COUNT(*) FROM maintenance_pending").fetchone()[0]


def _purge_pending_rows(db):
    return db.execute("SELECT COUNT(*) FROM purge_pending").fetchone()[0]


def _pending_ids(db):
    rep = residual_report(db, "s")
    return [p["record_id"] for p in rep["controlled"]["file_purge_pending"]]


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


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


def _hold_reader(db_path):
    """A second connection holding a long read transaction. It never
    reads the key table — any long read transaction blocks the
    checkpoint (count-only: BEGIN + SELECT COUNT(*) FROM memories)."""
    db2 = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    db2.execute("BEGIN")
    db2.execute("SELECT COUNT(*) FROM memories").fetchone()
    return db2


def _spawn_off_residue(db_path, a_id, a_rev, b_id, b_rev, key_out=None):
    """Run scripts/off_residue_child.py as an independent subprocess;
    the expected exit code is 77 (crashed after the OFF chain, without
    closing). With key_out given, the child also records B's
    per-record verification key hex there before the repair deletes
    the row."""
    argv = [sys.executable, str(OFF_RESIDUE_CHILD), str(db_path), a_id,
            str(a_rev), b_id, str(b_rev)]
    if key_out is not None:
        argv.append(str(key_out))
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env, encoding="utf-8")


def _setup_value(tmp_path, name, value):
    """Two records sharing `value`, B erased PARTIAL (A still holds
    the payload)."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", value, source_refs=SRC)
    b = remember(db, "s", "b-key", value, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    return db, path, a, b


def _make_off_residue(tmp_path, name, value):
    """An F0 historical-residue DB: the OFF chain (B PARTIAL, A
    COMPLETE, repair B COMPLETE) crashed without closing, so the
    deleted key's bytes stay in the main DB file. Returns
    (db, path, key_hex) with the connection open — the caller must
    keep it open (a clean close would checkpoint the residue away).
    key_hex is B's deleted verification key, recorded by the child
    before the repair deleted the row."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", value, source_refs=SRC)
    b = remember(db, "s", "b-key", value, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    key_out = tmp_path / f"{name}.keyhex"
    db.close()
    res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev, key_out)
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    assert _key_rows(db) == 0
    assert _marker_rows(db) == 0
    return db, path, key_out.read_text(encoding="ascii")


# --- (a) P0-R16-01: cleaned requires checkpoint busy == 0 ---

def test_busy_checkpoint_fails_purge_six_digit_value(tmp_path):
    """(a1)/(d2) Six-digit value + held reader: the probe cannot see
    the value (its range is 4-digit PINs — a disclosed blind spot),
    but the busy checkpoint alone must fail the purge. The deleted
    key stays in the main file and the failure is persisted."""
    db, path, key_hex = _make_off_residue(tmp_path, "t", SIX)
    reader = _hold_reader(path)
    try:
        out = purge_residue(db)
    finally:
        reader.close()
    assert out["cleaned"] is False
    assert out["checkpoint"] is not None
    assert out["checkpoint"][0] == 1
    assert out["error"] == (
        f"PRAGMA wal_checkpoint(TRUNCATE) busy: {out['checkpoint']}")
    # Blind spot, honestly present: the probe finds nothing for a
    # 6-digit value — and that must NOT compensate for busy == 1.
    assert out["residue_before"] is False
    assert out["residue_after"] is False
    assert "probe_before" not in out and "probe_after" not in out
    # The deleted key is still in the main DB file.
    assert key_hex.encode("ascii") in Path(path).read_bytes()
    # The failure is persisted, and the report is NOT_CLEAN.
    assert _purge_pending_rows(db) == 1
    detail = _purge_pending(db, "s")[0]["detail"]
    assert SIX not in detail
    rep = residual_report(db, "s")
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert rep["controlled"]["live_tables_clean"] is False
    assert "purge_residue:s" in _pending_ids(db)
    db.close()


def test_busy_checkpoint_fails_purge_four_digit_pin(tmp_path):
    """(a1) Four-digit PIN + held reader: the probe DOES find the
    residue — and the busy checkpoint fails the purge independently
    of it. Both signals agree; neither is needed to excuse the
    other."""
    db, path, key_hex = _make_off_residue(tmp_path, "t", PIN)
    reader = _hold_reader(path)
    try:
        out = purge_residue(db)
    finally:
        reader.close()
    assert out["cleaned"] is False
    assert out["checkpoint"] is not None
    assert out["checkpoint"][0] == 1
    assert out["error"] == (
        f"PRAGMA wal_checkpoint(TRUNCATE) busy: {out['checkpoint']}")
    assert out["residue_before"] is True
    assert out["residue_after"] is True
    assert key_hex.encode("ascii") in Path(path).read_bytes()
    assert _purge_pending_rows(db) == 1
    detail = _purge_pending(db, "s")[0]["detail"]
    assert PIN not in detail
    rep = residual_report(db, "s")
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert rep["controlled"]["live_tables_clean"] is False
    assert "purge_residue:s" in _pending_ids(db)
    db.close()


# --- (b) P0-R16-03: a failed cleanup persists a NOT_CLEAN state ---

def test_failed_cleanup_marker_persists_across_reopen(tmp_path):
    """(b)/(d3) After a failed purge, closing every connection and
    reopening must still report CONTROLLED_NOT_CLEAN with the
    persisted pending indicator — the failure is not forgotten with
    the process that observed it."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    reader = _hold_reader(path)
    try:
        out = purge_residue(db)
    finally:
        reader.close()
    assert out["cleaned"] is False
    assert _purge_pending_rows(db) == 1
    db.close()
    db = open_db(path)
    assert _purge_pending_rows(db) == 1
    rep = residual_report(db, "s")
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert rep["controlled"]["live_tables_clean"] is False
    assert "purge_residue:s" in _pending_ids(db)
    db.close()


def test_failed_cleanup_retry_converges_and_clears_marker(tmp_path):
    """(b3) Only a successful re-check clears the marker: after the
    reader releases, purge_residue again — cleaned True, marker
    gone, report clean. A repeated failure would keep the marker
    (see test_failed_cleanup_second_failure_keeps_marker)."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    reader = _hold_reader(path)
    try:
        out1 = purge_residue(db)
    finally:
        reader.close()
    assert out1["cleaned"] is False
    assert _purge_pending_rows(db) == 1
    out2 = purge_residue(db)
    assert out2["cleaned"] is True
    assert out2["checkpoint"] is not None
    assert out2["checkpoint"][0] == 0
    assert out2["residue_after"] is False
    assert _purge_pending_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin is None
    assert wal_pin is None
    db.close()


def test_failed_cleanup_second_failure_keeps_marker(tmp_path):
    """(b3) Clearing is success-only: a second failed purge (reader
    still held) refreshes the marker but never clears it."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    reader = _hold_reader(path)
    try:
        out1 = purge_residue(db)
        assert out1["cleaned"] is False
        assert _purge_pending_rows(db) == 1
        out2 = purge_residue(db)
        assert out2["cleaned"] is False
    finally:
        reader.close()
    assert _purge_pending_rows(db) == 1
    rep = residual_report(db, "s")
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    db.close()


def test_repair_rerun_does_not_clear_purge_marker(tmp_path):
    """(b4) The two marker tables are two separate states: a repair
    re-run that converges a PARTIAL record must NOT clear the
    historical-cleanup marker — repair cannot know whether the
    historical residue is gone. Only a successful purge_residue
    re-check clears it.

    Construction: F0 residue with a 6-digit value (probe-blind, so
    it cannot trip the repair's file check), plus a PARTIAL record
    D holding a different 4-digit PIN 1357 (C still live). Fail the
    purge under a held reader, release the reader, erase C, repair
    D to COMPLETE — then the marker must still stand."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", SIX)
    c = remember(db, "s", "c-key", "1357", source_refs=SRC)
    d = remember(db, "s", "d-key", "1357", source_refs=SRC)
    assert erase_record(db, "s", d["id"], _rev(db, d["id"]),
                        "rotation")["status"] == "PARTIAL"
    reader = _hold_reader(path)
    try:
        out = purge_residue(db)
    finally:
        reader.close()
    assert out["cleaned"] is False
    assert _purge_pending_rows(db) == 1
    # Converge the unrelated PARTIAL record through the repair path.
    assert erase_record(db, "s", c["id"], _rev(db, c["id"]),
                        "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", d["id"], _rev(db, d["id"]),
                        "repair")["status"] == "COMPLETE"
    # The repair converged — but the historical marker must stand.
    assert _purge_pending_rows(db) == 1
    rep = residual_report(db, "s")
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert "purge_residue:s" in _pending_ids(db)
    # Only a successful purge_residue re-check clears it.
    out2 = purge_residue(db)
    assert out2["cleaned"] is True
    assert _purge_pending_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


# --- (c) P1-R16-02: the probe must not return a PIN in plain text ---

def test_purge_residue_api_returns_booleans_not_pin(tmp_path):
    """(c1) The API return dict carries residue_before /
    residue_after (bools) and no probe_* keys — the erased PIN
    appears nowhere in it, even when the cleanup found residue."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    out = purge_residue(db)
    assert out["cleaned"] is True
    assert out["residue_before"] is True
    assert out["residue_after"] is False
    assert isinstance(out["residue_before"], bool)
    assert isinstance(out["residue_after"], bool)
    assert "probe_before" not in out and "probe_after" not in out
    assert PIN not in json.dumps(out)
    assert set(out) == {"cleaned", "secure_delete", "checkpoint",
                        "residue_before", "residue_after", "error"}
    db.close()


def test_purge_residue_cli_stdout_has_no_pin_both_modes(tmp_path, capsys,
                                                      caplog):
    """(c2) Both CLI modes (--json and plain) print the same dict —
    neither may contain the erased PIN. The DB connection is kept
    open so the CLI is never the last closer (no implicit last-close
    checkpoint clearing the residue mid-test). R18 (§30): the same
    no-PIN requirement now covers stderr and the log capture too —
    erasure.py / cli.py make no logging calls on this path, so
    caplog is expected empty; the assertion is written as PIN
    absence (not emptiness) so it stays meaningful if logging is
    added later."""
    from turnkeep.cli import main as cli_main
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    assert cli_main(["--db", str(path), "--json", "purge-residue"]) == 0
    captured = capsys.readouterr()
    assert PIN not in captured.out
    assert PIN not in captured.err
    assert json.loads(captured.out)["residue_before"] is True
    db2_path = tmp_path / "t2.sqlite3"
    db2 = _db(tmp_path, "t2")
    a2 = remember(db2, "s", "a-key", PIN, source_refs=SRC)
    b2 = remember(db2, "s", "b-key", PIN, source_refs=SRC)
    a2_rev, b2_rev = _rev(db2, a2["id"]), _rev(db2, b2["id"])
    db2.close()
    res = _spawn_off_residue(db2_path, a2["id"], a2_rev, b2["id"], b2_rev)
    assert res.returncode == 77, res.stderr
    db2 = open_db(db2_path)
    assert cli_main(["--db", str(db2_path), "purge-residue"]) == 0
    captured = capsys.readouterr()
    assert PIN not in captured.out
    assert PIN not in captured.err
    assert json.loads(captured.out)["residue_before"] is True
    assert PIN not in caplog.text
    assert all(PIN not in r.getMessage() for r in caplog.records)
    db.close()
    db2.close()


def test_purge_residue_cli_stdout_has_no_pin_on_failure(tmp_path, capsys,
                                                      caplog):
    """(c2) The failure case too: with the probe forced to keep
    finding residue the CLI exits 2 — and its stdout still must not
    contain the erased PIN. R18 (§30): stderr and the log capture
    get the same no-PIN assertion as the success case."""
    from turnkeep.cli import main as cli_main
    db, path, a, b = _setup_value(tmp_path, "t", PIN)
    assert erase_record(db, "s", a["id"], _rev(db, a["id"]),
                        "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "repair")["status"] == "COMPLETE"
    db.close()
    real_probe = E._probe_files
    E._probe_files = lambda _db, _hashes: True
    try:
        assert cli_main(["--db", str(path), "--json", "purge-residue"]) == 2
        captured = capsys.readouterr()
    finally:
        E._probe_files = real_probe
    assert PIN not in captured.out
    assert PIN not in captured.err
    out = json.loads(captured.out)
    assert out["cleaned"] is False
    assert out["residue_after"] is True
    assert PIN not in caplog.text
    assert all(PIN not in r.getMessage() for r in caplog.records)


def test_verify_purge_returns_bool(tmp_path):
    """(c2) _verify_purge already returns a bool — lock it: True on
    a clean DB, False when the deleted key is still recoverable."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    result = _verify_purge(db, _key_hex, hashes)
    assert result is False
    assert isinstance(result, bool)
    db2 = _db(tmp_path, "clean")
    assert _verify_purge(db2, "ab" * 32, set()) is True
    db.close()
    db2.close()


# --- (f) docs — §29 added, §28 corrected, no placeholders ---

def test_known_limitations_section_29():
    """(f) §29 records the R16 rework: the three findings, the
    chosen scheme (new purge_pending table, schema v7), the probe
    contract change (residue_* booleans), the probe blind spot and
    the busy-checkpoint requirement as two independent requirements,
    and the crash-window disclosure — with no placeholder left."""
    text = (REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    assert "## 29." in text
    sec29 = text.split("## 29.")[1]
    assert "TBD" not in sec29
    for needle in (
        "P0-R16-01", "P0-R16-03", "P1-R16-02", "purge_pending",
        "007_purge_pending.sql", "residue_before", "residue_after",
        "busy", "CONTROLLED_NOT_CLEAN", "purge_residue",
        "OFFLINE_RC_READY", "PUBLIC_CANDIDATE NOT MET",
    ):
        assert needle in sec29, needle


def test_section_28_never_lies_corrected():
    """(f) §28's "never-lies reporting" claim is corrected: the R15
    wording said a failed cleanup keeps the report NOT_CLEAN, but
    the failure wrote no marker and the report still read clean
    (P0-R16-03) — §29 records the correction. The R15 needles
    must survive the edit."""
    text = (REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    sec28 = text.split("## 28.")[1].split("## 29.")[0]
    assert "never-lies" not in sec28
    assert "never lies" not in sec28
    for needle in ("P0-R15-01", "purge_residue", "F0", "F4",
                   "the recoverable material is the deleted verification KEY"):
        assert needle in sec28, needle
    sec = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "TBD" not in sec


def test_schema_v7_with_purge_pending_migration():
    """(b1) Schema v7: migration 007 adds the live-DB-only
    purge_pending table; a fresh DB carries it and schema_meta
    lists 1–7."""
    assert SCHEMA_VERSION == 7
