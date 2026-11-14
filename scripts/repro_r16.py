"""Repro for the R16 rework (GPT R15 verdict, P0-R15-01 secure_delete).
Runs from a source checkout, an extracted RC, or an installed wheel
against throwaway DBs. Crash points are exercised by an independent
subprocess (scripts/crash_child.py) that calls os._exit(77) at the
requested point — no normal close, no transaction commit. The
historical-residue fixture is built by another independent subprocess
(scripts/off_residue_child.py) that runs the OFF chain and crashes
without closing, so the deleted keys' bytes stay in the main DB file.

Sections:
  A: (1a)/(1d) open_db() sets secure_delete=ON and reads it back;
     secure_delete is per-connection, not persisted; the erase-entry
     gate refuses OFF (fail closed) on both paths, nothing written,
     retryable; normal ON COMPLETE does not regress.
  B: (2a) the OFF chain (gate bypassed, test-only) — post-fix
     behaviour: the file check downgrades the repair to PARTIAL with
     the key restored and the marker kept, never a false COMPLETE;
     the ON reverse chain is truly clean. Three-place file scans
     (main file / WAL / both separately). NOTE: GPT's R15 result table
     (repair COMPLETE, marker 0, report clean, blind scan recovers
     4827) is no longer reachable under the fixed code — the gate
     refuses an OFF erase outright, and even with the gate bypassed
     the file check downgrades the repair. The pre-fix table is
     preserved in ~/workspace/r15-verification/repro_r15_verify.py
     (run against HEAD d1750d8).
  C: (3) the file-level purge acceptance: _verify_purge unit checks;
     the post-VACUUM checkpoint gap (a manual checkpoint after VACUUM
     clears the key from the main file); the clean-close control (a
     clean close checkpoints the WAL's VACUUM pages and clears the
     residue); the mid-transaction crash point (marker written, key
     not yet deleted — GPT's R15 extra check); the probe-only branch
     (key already deleted by a crashed erase) converging on re-run.
  D: (4) historical-residue cleanup F0–F4: the F0 fixture (OFF chain
     crashed without closing) and purge_residue; the F4 no-VACUUM
     residue (checkpoint-only fails, VACUUM + checkpoint succeeds);
     idempotence; the never-lies report; the `turnkeep purge-residue`
     CLI exit codes.
  E: (5) §27/§28 doc checks (TBD rewritten, §28 present, purge
     assumption stated with secure_delete=ON as its premise) +
     SECURITY.md §6. (Skipped when the docs are not next to this
     copy — the installed wheel carries no docs.)
  F: R15 original-scope spot checks (busy-checkpoint chain, "202" /
     "2026" COMPLETE, label decode chain, pre_pending + post_checkpoint
     crash points, first-erase crash payload in the live files).

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT in
/tmp (tmpfs, 512M). This script removes its TMPROOT on success
(registered with atexit); on failure it keeps it and prints the path,
so the failing DBs can be inspected. The child subprocesses write
their DBs under the same TMPROOT (persistent disk, never a fresh /tmp
dir per spawn).
"""
import atexit
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import turnkeep  # noqa: F401  (proves the import resolves: source, RC or wheel)
from turnkeep.storage import open_db, SCHEMA_VERSION
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue, _run_maintenance,
    _purge_failed, _stored_needle_hashes, _record_verification_keys,
    _check_reason_against_stored_hashes, _erased_record_flags,
    _maintenance_pending, _all_stored_needle_hashes, _probe_files,
    _verify_purge, _candidate_recovers_pin, _require_secure_delete,
)
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
PAYLOAD = "PAYLOAD-TOKEN-9f3a7c2e1b5d-UNIQUE-7741"
CRASH_CHILD = Path(__file__).resolve().parent / "crash_child.py"
OFF_RESIDUE_CHILD = Path(__file__).resolve().parent / "off_residue_child.py"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r16-"))
CHECKS = []


def _cleanup():
    fails = [n for n, ok in CHECKS if not ok]
    if not fails:
        shutil.rmtree(TMPROOT, ignore_errors=True)
    else:
        print(f"\n(keeping TMPROOT for inspection: {TMPROOT})")


atexit.register(_cleanup)


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def _db(name, scope="s"):
    db = open_db(TMPROOT / f"{name}.sqlite3")
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
    return _blind_scan_file(path, db), _blind_scan_file(str(path) + "-wal", db)


def _blind_scan_merged(path, db):
    """The merged main+WAL blob (the strongest single probe)."""
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if not hashes:
        return None
    blob = Path(path).read_bytes()
    wal = Path(str(path) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
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


def _payload_in_files(db_path, payload):
    """The raw payload bytes in the live DB file + WAL (verbatim)."""
    blob = Path(db_path).read_bytes()
    wal = Path(str(db_path) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    return payload.encode() in blob


def _hold_reader(db_path):
    """A second connection holding a long read transaction. It never
    reads the key table — any long read transaction blocks the
    checkpoint (count-only: BEGIN + SELECT COUNT(*) FROM memories)."""
    db2 = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    db2.execute("BEGIN")
    db2.execute("SELECT COUNT(*) FROM memories").fetchone()
    return db2


def _spawn_crash(db_path, record_id, revision, mode, point, reason=None):
    """Run crash_child.py as an independent subprocess. Returns the
    CompletedProcess; the expected exit code is 77 (crashed at the
    requested point)."""
    argv = [sys.executable, str(CRASH_CHILD), str(db_path), record_id,
            str(revision), mode, point]
    if reason is not None:
        argv.append(reason)
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _spawn_off_residue(db_path, a_id, a_rev, b_id, b_rev):
    """Run off_residue_child.py as an independent subprocess. Returns
    the CompletedProcess; the expected exit code is 77 (crashed after
    the OFF chain, without closing)."""
    argv = [sys.executable, str(OFF_RESIDUE_CHILD), str(db_path), a_id,
            str(a_rev), b_id, str(b_rev)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _setup_pin(name):
    """Repair-path setup: two records sharing the PIN, B erased
    PARTIAL (A still holds the payload). Returns (db, path, a, b)."""
    path = TMPROOT / f"{name}.sqlite3"
    db = _db(name)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    assert out_b["status"] == "PARTIAL", out_b["status"]
    return db, path, a, b


def _converge_pin(db, path, a, b):
    """Erase A, repair B, and assert the report is clean and nothing
    is enumerable from the live files."""
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE", out_a["status"]
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE", out_r["status"]
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
    assert main_pin is None and wal_pin is None


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: open_db + gate
print("\n== A: open_db sets secure_delete=ON; erase-entry gate ==")
db = _db("a-open")
check("A1 open_db returns a connection reading secure_delete == 1",
      db.execute("PRAGMA secure_delete").fetchone()[0] == 1)
db.execute("PRAGMA secure_delete=OFF")
check("A2 an OFF setting reads back 0 on the same connection",
      db.execute("PRAGMA secure_delete").fetchone()[0] == 0)
db.close()
db = open_db(TMPROOT / "a-open.sqlite3")
check("A3 secure_delete is per-connection, not persisted: a reopen reads 1",
      db.execute("PRAGMA secure_delete").fetchone()[0] == 1)
db.close()

db = _db("a-gate")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
a_rev = _rev(db, a["id"])
db.execute("PRAGMA secure_delete=OFF")
try:
    erase_record(db, "s", a["id"], a_rev, "rotation")
    gate_ok = False
except TurnkeepError as e:
    gate_ok = e.code == "SECURE_DELETE_OFF"
check("A4 erase under OFF raises SECURE_DELETE_OFF (first-erase path)", gate_ok)
check("A5 refused erase leaves the record active at the same revision",
      _state(db, a["id"]) == "active" and _rev(db, a["id"]) == a_rev
      and _value(db, a["id"]) == json.dumps(PIN))
check("A6 refused erase writes nothing: no key row, no marker, no event",
      _key_rows(db) == 0 and _marker_rows(db) == 0 and _erase_event_count(db) == 0)
db.execute("PRAGMA secure_delete=ON")
check("A7 the refused erase is retryable: ON erase reaches COMPLETE",
      erase_record(db, "s", a["id"], a_rev, "rotation")["status"] == "COMPLETE")
db.close()

db, path, a, b = _setup_pin("a-repair-gate")
db.execute("PRAGMA secure_delete=OFF")
try:
    erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    gate_ok = False
except TurnkeepError as e:
    gate_ok = e.code == "SECURE_DELETE_OFF"
check("A8 repair under OFF raises SECURE_DELETE_OFF (repair path)", gate_ok)
check("A9 refused repair leaves B's key row intact, no marker, no new event",
      _key_rows(db) == 1 and _marker_rows(db) == 0 and _erase_event_count(db) == 1)
db.execute("PRAGMA secure_delete=ON")
_converge_pin(db, path, a, b)
check("A10 both paths converge to COMPLETE once the connection is ON", True)
db.close()

key = bytes.fromhex("ab" * 32)
hashes = {hmac.new(key, b"4827", hashlib.sha256).hexdigest()}
# R16 rework (P1-R16-02): the probe answers a boolean only — the
# recovered PIN is never returned, so the equivalence assertion is
# "recovers / does not recover", not the value itself.
check("A11 _candidate_recovers_pin matches hmac.new on a known vector",
      _candidate_recovers_pin(key, hashes) is True
      and _candidate_recovers_pin(bytes.fromhex("cd" * 32), hashes) is False)

# ---------------------------------------------------------------- B: OFF chain + ON reverse
print("\n== B: OFF chain (gate bypassed) + ON reverse chain ==")
path = TMPROOT / "b-off.sqlite3"
db = _db("b-off")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
db.execute("PRAGMA secure_delete=OFF")
E._require_secure_delete = lambda _db: None
try:
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
finally:
    E._require_secure_delete = _require_secure_delete
check("B1 OFF chain: erase B PARTIAL", out_b["status"] == "PARTIAL", out_b["status"])
check("B2 OFF chain: erase A PARTIAL (file check fails on its own key residue)",
      out_a["status"] == "PARTIAL"
      and any("file purge verification failed" in n for n in out_a.get("maintenance", [])),
      out_a["status"])
check("B3 OFF chain: repair B PARTIAL, marker kept, file-check note",
      out_r["status"] == "PARTIAL" and out_r.get("maintenance_pending") is True
      and any("file purge verification failed" in n for n in out_r.get("maintenance", [])),
      out_r["status"])
check("B4 OFF chain: both keys restored, both markers standing",
      _key_rows(db) == 2 and _marker_rows(db) == 2,
      f"keys={_key_rows(db)} markers={_marker_rows(db)}")
rep = residual_report(db, "s")
check("B5 OFF chain: report NOT_CLEAN with both records pending",
      rep["controlled"]["live_tables_clean"] is False
      and sorted(_pending_ids(db)) == sorted([a["id"], b["id"]]))
check("B6 OFF chain: the product probe finds no RESIDUE (keys are live, skipped)",
      _probe_files(db, _all_stored_needle_hashes(db)) is False)
main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
check("B7 OFF chain: independent blind scan of the main file recovers 4827 "
      "(via the LIVE restored keys — not residue)",
      main_pin == PIN, str(main_pin))
check("B8 OFF chain: WAL blind scan also recovers 4827 (via the LIVE "
      "restored keys — VACUUM's pages carry the restored key rows)",
      wal_pin == PIN, str(wal_pin))
db.close()

db, path, a, b = _setup_pin("b-on")
_converge_pin(db, path, a, b)
check("B9 ON chain: erase A + repair B COMPLETE, report clean", True)
main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
check("B10 ON chain: main-file blind scan finds nothing", main_pin is None, str(main_pin))
check("B11 ON chain: WAL blind scan finds nothing", wal_pin is None, str(wal_pin))
check("B12 ON chain: merged main+WAL blind scan finds nothing",
      _blind_scan_merged(str(path), db) is None)
db.close()

# ---------------------------------------------------------------- C: file check + crash points
print("\n== C: file-level purge acceptance + crash points ==")
db, path, a, b = _setup_pin("c-unit")
live_hex = db.execute(
    "SELECT secret_hex FROM record_erase_keys WHERE scope_id='s' AND record_id=?",
    (b["id"],)).fetchone()[0]
hashes = set()
for meta in _all_erase_metas(db):
    hashes.update(h for h in meta.get("needle_hashes", []) or [] if isinstance(h, str))
check("C1 _verify_purge fails when the key hex IS in the file (live key)",
      _verify_purge(db, live_hex, hashes) is False)
check("C2 _verify_purge passes for a key hex that is NOT in the file",
      _verify_purge(db, os.urandom(32).hex(), hashes) is True)
db.close()

# C3: post-VACUUM checkpoint gap — in an F0 fixture DB, VACUUM alone
# leaves the key in the main file; a checkpoint AFTER the VACUUM is
# what moves the compact pages in (the gap _run_maintenance has).
path = TMPROOT / "c-gap.sqlite3"
db = _db("c-gap")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
db.close()
res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev)
assert res.returncode == 77, res.stderr
db = open_db(path)
check("C3a F0 fixture: main-file blind scan recovers 4827 before any cleanup",
      _blind_scan_file(path, db) == PIN)
db.execute("VACUUM")
check("C3b VACUUM alone does NOT clear the main file (no checkpoint follows it)",
      _blind_scan_file(path, db) == PIN)
db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
check("C3c a checkpoint AFTER the VACUUM clears the key from the main file",
      _blind_scan_file(path, db) is None)
db.close()

# C4: clean-close control — the same OFF chain, closed cleanly (not
# crashed), has its residue cleared by the close checkpoint.
path = TMPROOT / "c-close.sqlite3"
db = _db("c-close")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
db.execute("PRAGMA secure_delete=OFF")
E._require_secure_delete = lambda _db: None
E._verify_purge = lambda _db, _key, _hashes: True
try:
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")["status"] == "PARTIAL"
    assert erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")["status"] == "COMPLETE"
finally:
    E._require_secure_delete = _require_secure_delete
    E._verify_purge = _verify_purge
db.close()  # clean close: checkpoints the WAL's VACUUM pages in
db = open_db(path)
check("C4 clean close clears the residue (close checkpoints the VACUUM pages)",
      _blind_scan_file(path, db) is None)
db.close()

# C5/C6: mid-transaction crash point (marker written, key not deleted).
for mode, name in (("repair", "c-mid-repair"), ("first", "c-mid-first")):
    if mode == "repair":
        db, path, a, b = _setup_pin(name)
        rid, rev = b["id"], _rev(db, b["id"])
    else:
        # A single record: its first erase must reach COMPLETE for the
        # key deletion (the crash point) to run — a second record
        # holding the payload would make the scan PARTIAL instead.
        path = TMPROOT / f"{name}.sqlite3"
        db = _db(name)
        b = remember(db, "s", "b-key", PIN, source_refs=SRC)
        rid, rev = b["id"], _rev(db, b["id"])
    db.close()
    res = _spawn_crash(path, rid, rev, mode, "mid_delete")
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    if mode == "repair":
        check("C5 mid_delete crash (repair): marker 0, key 1, record erased",
              _marker_rows(db) == 0 and _key_rows(db) == 1 and _state(db, rid) == "erased")
        rep = residual_report(db, "s")
        check("C6 mid_delete crash (repair): report NOT_CLEAN",
              rep["controlled"]["live_tables_clean"] is False)
    else:
        check("C7 mid_delete crash (first): marker 0, key 0, record active",
              _marker_rows(db) == 0 and _key_rows(db) == 0 and _state(db, rid) == "active")
    db.close()

# C8: probe-only branch — key already deleted by a crashed erase.
db, path, a, b = _setup_pin("c-probe-only")
b_rev = _rev(db, b["id"])
db.close()
res = _spawn_crash(path, b["id"], b_rev, "repair", "post_commit")
assert res.returncode == 77, res.stderr
db = open_db(path)
check("C8a post_commit crash: key 0, marker 1",
      _key_rows(db) == 0 and _marker_rows(db) == 1)
check("C8b repair re-run (probe-only, key_row None) converges to COMPLETE",
      erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")["status"] == "COMPLETE"
      and erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")["status"] == "COMPLETE")
rep = residual_report(db, "s")
check("C8c report clean after the probe-only convergence",
      rep["controlled"]["live_tables_clean"] is True
      and rep["controlled"]["file_purge_pending"] == [])
db.close()

# ---------------------------------------------------------------- D: historical cleanup F0–F4
print("\n== D: historical-residue cleanup (purge_residue / CLI) ==")
path = TMPROOT / "d-f0.sqlite3"
db = _db("d-f0")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
db.close()
res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev)
assert res.returncode == 77, res.stderr
db = open_db(path)
check("D1 F0 fixture: key rows 0, marker 0, report clean (false-clean)",
      _key_rows(db) == 0 and _marker_rows(db) == 0
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True)
main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
check("D2 F0 fixture: main-file blind scan recovers 4827", main_pin == PIN, str(main_pin))
check("D3 F0 fixture: WAL blind scan finds nothing", wal_pin is None, str(wal_pin))
out = purge_residue(db)
check("D4 purge_residue cleans the F0 residue (ON + VACUUM + checkpoint + probe)",
      out["cleaned"] is True and out["residue_before"] is True
      and out["residue_after"] is False and out["error"] is None, str(out))
main_pin, wal_pin = _blind_scan_main_wal(str(path), db)
check("D5 after cleanup: main + WAL blind scans find nothing, report clean",
      main_pin is None and wal_pin is None
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True)
db.close()

# F4: no-VACUUM residue — direct DELETE of the key row under OFF +
# checkpoint (no VACUUM). Checkpoint-only under ON must NOT clear it.
path = TMPROOT / "d-f4.sqlite3"
db = _db("d-f4")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")["status"] == "PARTIAL"
db.execute("PRAGMA secure_delete=OFF")
db.execute("DELETE FROM record_erase_keys WHERE scope_id='s' AND record_id=?", (b["id"],))
db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
db.execute("PRAGMA secure_delete=ON")
check("D6 F4 fixture: no-VACUUM residue, main-file blind scan recovers 4827",
      _blind_scan_file(path, db) == PIN)
db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
check("D7 F4: checkpoint-only under ON does NOT clear the residue",
      _blind_scan_file(path, db) == PIN)
out = purge_residue(db)
check("D8 F4: purge_residue (VACUUM + checkpoint) clears it",
      out["cleaned"] is True and out["residue_before"] is True
      and out["residue_after"] is False, str(out))
check("D9 F4: after cleanup the blind scan finds nothing",
      _blind_scan_file(path, db) is None)
db.close()

db, path, a, b = _setup_pin("d-idem")
_converge_pin(db, path, a, b)
out1 = purge_residue(db)
out2 = purge_residue(db)
check("D10 purge_residue is idempotent on a clean DB (twice, no payload loss)",
      out1["cleaned"] is True and out2["cleaned"] is True
      and out1["residue_before"] is False and _state(db, a["id"]) == "erased")
db.close()

db, path, a, b = _setup_pin("d-lies")
_converge_pin(db, path, a, b)
real_probe = E._probe_files
E._probe_files = lambda _db, _hashes: True
try:
    out = purge_residue(db)
finally:
    E._probe_files = real_probe
check("D11 purge_residue never lies: probe still finding => cleaned False",
      out["cleaned"] is False and out["residue_after"] is True)
db.close()

from turnkeep.cli import main as cli_main
import io, contextlib
db, path, a, b = _setup_pin("d-cli")
_converge_pin(db, path, a, b)
db.close()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli_main(["--db", str(path), "--json", "purge-residue"])
check("D12 CLI purge-residue exits 0 on a clean DB",
      rc == 0 and json.loads(buf.getvalue())["cleaned"] is True, str(rc))
path2 = TMPROOT / "d-cli2.sqlite3"
db2 = _db("d-cli2")
a2 = remember(db2, "s", "a-key", PIN, source_refs=SRC)
b2 = remember(db2, "s", "b-key", PIN, source_refs=SRC)
a2_rev, b2_rev = _rev(db2, a2["id"]), _rev(db2, b2["id"])
db2.close()
res = _spawn_off_residue(path2, a2["id"], a2_rev, b2["id"], b2_rev)
assert res.returncode == 77, res.stderr
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli_main(["--db", str(path2), "--json", "purge-residue"])
# R16 rework (P1-R16-02): stdout must never contain the erased PIN.
check("D13 CLI purge-residue exits 0 after cleaning a residue DB "
      "(residue_before True, no PIN in stdout)",
      rc == 0 and json.loads(buf.getvalue())["residue_before"] is True
      and PIN not in buf.getvalue(), str(rc))
E._probe_files = lambda _db, _hashes: True
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(["--db", str(path), "--json", "purge-residue"])
    out3 = json.loads(buf.getvalue())
finally:
    E._probe_files = real_probe
check("D14 CLI purge-residue exits 2 when the residue cannot be cleared",
      rc == 2 and out3["cleaned"] is False, str(rc))
check("D15 CLI purge-residue with neither --db nor --scope exits 2",
      cli_main(["purge-residue"]) == 2)

# ---------------------------------------------------------------- E: docs
print("\n== E: KNOWN_LIMITATIONS §27/§28 + SECURITY.md ==")
doc_root = Path(__file__).resolve().parents[1]
kl = doc_root / "docs" / "KNOWN_LIMITATIONS.md"
sec = doc_root / "SECURITY.md"
if not kl.exists():
    print("SKIP E1-E5: no KNOWN_LIMITATIONS.md next to this copy (wheel run)")
else:
    text = kl.read_text(encoding="utf-8")
    check("E1 §27 TBD rewritten (no 'TBD after R15 verdict')",
          "TBD after R15 verdict" not in text)
    sec28 = text.split("## 28.")[1] if "## 28." in text else ""
    check("E2 §28 present, no TBD, records the R15 rework",
          "## 28." in text and "TBD" not in sec28
          and all(n in sec28 for n in (
              "P0-R15-01", "secure_delete", "PRAGMA secure_delete=ON",
              "purge_residue", "purge-residue", "per-connection",
              "not persisted", "F0", "F4", "checkpoint-only",
              "the recoverable material is the deleted verification KEY",
              "OFFLINE_RC_READY", "PUBLIC_CANDIDATE NOT MET")))
    sec27 = text.split("## 27.")[1].split("## 28.")[0] if "## 27." in text else ""
    check("E3 §27 carries the R15 verdict + secure_delete",
          "P0-R15-01" in sec27 and "secure_delete" in sec27)
    sec26 = text.split("## 26.")[1].split("## 27.")[0] if "## 26." in text else ""
    check("E4 §26 (1f) states the purge claim with secure_delete=ON as its premise",
          "secure_delete=ON" in sec26)
if not sec.exists():
    print("SKIP E5: no SECURITY.md next to this copy (wheel run)")
else:
    check("E5 SECURITY.md §6 has no TBD",
          "TBD" not in sec.read_text(encoding="utf-8"))

# ---------------------------------------------------------------- F: R15 original-scope spot checks
print("\n== F: R15 original-scope spot checks ==")
# F1: busy-checkpoint chain (R13) — reader held -> PARTIAL + marker;
# reader released -> repair COMPLETE.
db, path, a, b = _setup_pin("f-busy")
reader = _hold_reader(path)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("F1a busy checkpoint: erase A PARTIAL with a marker",
      out_a["status"] == "PARTIAL" and out_a.get("maintenance_pending") is True)
reader.close()
db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
check("F1b reader released: repair A converges to COMPLETE",
      erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")["status"] == "COMPLETE")
check("F1c busy chain: report clean after convergence",
      residual_report(db, "s")["controlled"]["live_tables_clean"] is True)
db.close()

# F2: "202" / "2026" COMPLETE (R11).
db = _db("f-202")
for payload, label in (("202", "F2a"), ("2026", "F2b")):
    rec = remember(db, "s", f"k-{payload}", payload, source_refs=SRC)
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check(f"{label} payload {payload!r} erases COMPLETE", out["status"] == "COMPLETE",
          out["status"])
db.close()

# F3: label decode chain (R12) — Base64(COMPLETE) payload is PARTIAL.
db = _db("f-label")
p_complete = base64.b64encode(b"COMPLETE").decode()
rec = remember(db, "s", "k", p_complete, source_refs=SRC)
out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
check("F3 Base64(COMPLETE) payload erases PARTIAL (status_label)",
      out["status"] == "PARTIAL", out["status"])
db.close()

# F4/F5: R14 crash points still work (pre_pending + post_checkpoint).
db, path, a, b = _setup_pin("f-pre")
b_rev = _rev(db, b["id"])
db.close()
res = _spawn_crash(path, b["id"], b_rev, "repair", "pre_pending")
assert res.returncode == 77, res.stderr
db = open_db(path)
check("F4 pre_pending crash (repair): atomic rollback, key 1, marker 0",
      _key_rows(db) == 1 and _marker_rows(db) == 0 and _state(db, b["id"]) == "erased")
db.close()
res = _spawn_crash(path, b["id"], b_rev, "repair", "post_checkpoint")
assert res.returncode == 77, res.stderr
db = open_db(path)
check("F5 post_checkpoint crash (repair): stale marker 1, key 0",
      _marker_rows(db) == 1 and _key_rows(db) == 0)
check("F6 post_checkpoint crash: repair re-run clears the stale marker",
      erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")["status"] == "COMPLETE"
      and erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")["status"] == "COMPLETE"
      and _marker_rows(db) == 0)
db.close()

# F7: first-erase crash — the raw payload bytes sit in the live files
# from the remember frame (R14 §1.3 correction: the key never hit disk,
# but the payload is recoverable pre-convergence), gone post-convergence.
path = TMPROOT / "f-first.sqlite3"
db = _db("f-first")
a = remember(db, "s", "a-key", PAYLOAD, source_refs=SRC)
b = remember(db, "s", "b-key", PAYLOAD, source_refs=SRC)
assert erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")["status"] == "PARTIAL"
a_rev = _rev(db, a["id"])
db.close()
res = _spawn_crash(path, a["id"], a_rev, "first", "post_commit")
assert res.returncode == 77, res.stderr
db = open_db(path)
check("F7a first-erase crash: payload in the live files, marker standing, report NOT_CLEAN",
      _payload_in_files(str(path), PAYLOAD) and _marker_rows(db) == 1
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is False)
check("F7b first-erase crash: repair re-run converges, payload gone from the live files",
      erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")["status"] == "COMPLETE"
      and not _payload_in_files(str(path), PAYLOAD))
db.close()

# R16 rework (P0-R16-03): migration 007 adds the live-DB-only
# purge_pending table — schema v7, not v6.
check("F8 schema v7 (migration 007 purge_pending in this rework)", SCHEMA_VERSION == 7)

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
