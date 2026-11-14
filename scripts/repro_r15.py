"""Repro for the R15 rework (GPT R14 verdict, P0-R14-01 crash consistency).
Runs from a source checkout, an extracted RC, or an installed wheel
against throwaway DBs. Crash points are exercised by an independent
subprocess (scripts/crash_child.py) that calls os._exit(77) at the
requested point — no normal close, no transaction commit.

Sections:
  A: Repair path, four crash points (i) before the pending write,
     (ii) after the pending + key-deletion transaction commits,
     (iii) after the checkpoint returns busy, (iv) after the checkpoint
     succeeds, before the marker clears. Each point: reopen the DB,
     assert the report and the DB/WAL file bytes, then converge via a
     repair re-run.
  B: First-erase path, same four points. The first-erase path's
     assertions are marker/report honesty: its key is created and
     deleted inside the same transaction and never hits disk (no
     key-recovery chain — verification §1.3), but the raw payload
     bytes sit in the WAL from the remember frame, so a crash before
     the checkpoint leaves the payload recoverable with no marker
     unless the marker was written in-transaction (the §27
     correction to §1.3's narrowing).
  C: _run_maintenance unit; (1e) pending-write failure rolls the
     whole erase back atomically; (1d) the stored-hash consumers in
     the "key deleted, marker present" state (repair-reason vetting
     still works via the key-independent erase_flags).
  D: §26/§27 doc checks (TBD synced, §27 present, stale-marker
     semantics, unconditional decision, open_db decision) + SECURITY.md
     §5. (Skipped when the docs are not next to this copy — the
     installed wheel carries no docs.)
  E: R14 original-scope spot checks (busy checkpoint chain, "202" /
     "2026" COMPLETE, label decode chains).
  F: .pre-v7.bak behaviour — a populated upgrade writes no backup;
     a fresh-DB backup holds no payload; a planted .bak is listed as
     an uncontrolled backup copy.

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT in
/tmp (tmpfs, 512M). This script removes its TMPROOT on success
(registered with atexit); on failure it keeps it and prints the path,
so the failing DBs can be inspected. The crash-child subprocesses
write their DBs under the same TMPROOT (persistent disk, never a
fresh /tmp dir per spawn).
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
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, _run_maintenance, _purge_failed,
    _stored_needle_hashes, _record_verification_keys,
    _check_reason_against_stored_hashes, _erased_record_flags,
    _maintenance_pending,
)
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
PAYLOAD = "PAYLOAD-TOKEN-9f3a7c2e1b5d-UNIQUE-7741"
CRASH_CHILD = Path(__file__).resolve().parent / "crash_child.py"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r15-"))
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


def _live_keys(db, scope="s"):
    keys = []
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    return keys


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN against the persisted
    needle hashes, using every key the live DB holds. Returns the
    recovered value, or None."""
    keys = _live_keys(db, scope)
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
    """Stronger probe: recover the PIN from the live DB file + WAL
    bytes alone, with NO prior knowledge of any key. A sliding window
    over every >=64-char hex run collects candidate 32-byte keys (a
    fixed 64-char regex window misaligns — the key hex nests inside
    longer hex strings, e.g. adjacent cell payloads), then each
    candidate is tried against the event needle hashes. Returns the
    recovered value, or None."""
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


def _key_record_ids(db):
    return {r[0] for r in db.execute(
        "SELECT record_id FROM record_erase_keys").fetchall()}


def _pending_ids(db):
    rep = residual_report(db, "s")
    return [p["record_id"] for p in rep["controlled"]["file_purge_pending"]]


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


def _spawn_child(db_path, record_id, revision, mode, point, reason=None):
    """Run crash_child.py as an independent subprocess. Returns the
    CompletedProcess; the expected exit code is 77 (crashed at the
    requested point)."""
    argv = [sys.executable, str(CRASH_CHILD), str(db_path), record_id,
            str(revision), mode, point]
    if reason is not None:
        argv.append(reason)
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _crash_setup_pin(name):
    """Repair-path setup: two records sharing the PIN, B erased
    PARTIAL (A still holds the payload). Returns (db, path, a, b)."""
    path = TMPROOT / f"{name}.sqlite3"
    db = _db(name)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    assert out_b["status"] == "PARTIAL", out_b["status"]
    return db, path, a, b


def _crash_setup_payload(name):
    """First-erase-path setup: two records sharing a distinctive
    payload, B erased PARTIAL. Returns (db, path, a, b)."""
    path = TMPROOT / f"{name}.sqlite3"
    db = _db(name)
    a = remember(db, "s", "a-key", PAYLOAD, source_refs=SRC)
    b = remember(db, "s", "b-key", PAYLOAD, source_refs=SRC)
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    assert out_b["status"] == "PARTIAL", out_b["status"]
    return db, path, a, b


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: repair path, four crash points
print("\n== A: repair path, four crash points ==")

# A(i) pre_pending: crash inside the erase transaction, at the marker
# write — atomic rollback: B keeps its pre-erase state, its key row
# is intact, no marker, no repair event.
db, path, a, b = _crash_setup_pin("a-pre-pending")
rev_b = _rev(db, b["id"])
db.close()
proc = _spawn_child(path, b["id"], rev_b, "repair", "pre_pending")
check("A-i1 child crashed at pre_pending (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("A-i2 B still tombstoned after the rollback",
      _state(db, b["id"]) == "erased", _state(db, b["id"]))
check("A-i3 B revision unchanged by the rolled-back repair",
      _rev(db, b["id"]) == rev_b, f"got {_rev(db, b['id'])}")
check("A-i4 B key row intact (1)", _key_rows(db) == 1, f"got {_key_rows(db)}")
check("A-i5 no maintenance_pending marker", _pending_ids(db) == [])
check("A-i6 no repair event published (still 1 erase event)",
      _erase_event_count(db) == 1, f"got {_erase_event_count(db)}")
rep = residual_report(db, "s")
check("A-i7 report still lists the honest PARTIAL residual (A holds the PIN)",
      len(rep["controlled"]["controlled_residuals"]) > 0)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("A-i8 erase A after the rollback -> COMPLETE",
      out_a["status"] == "COMPLETE", out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("A-i9 repair B after the rollback -> COMPLETE",
      out_r["status"] == "COMPLETE", out_r["status"])
rep2 = residual_report(db, "s")
check("A-i10 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
check("A-i11 enumeration finds nothing after convergence",
      _enumerate_pin(db) is None)
db.close()

# A(ii) post_commit: crash right after the erase transaction commits
# (at _run_maintenance) — marker persisted, key deleted, checkpoint
# never ran. The key bytes are still in the WAL: the file-byte probe
# recovers the PIN while the report says NOT_CLEAN (honest).
db, path, a, b = _crash_setup_pin("a-post-commit")
rev_b = _rev(db, b["id"])
db.close()
proc = _spawn_child(path, b["id"], rev_b, "repair", "post_commit")
check("A-ii1 child crashed at post_commit (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("A-ii2 marker stands after the crash", _pending_ids(db) == [b["id"]],
      str(_pending_ids(db)))
check("A-ii3 key row deleted (0)", _key_rows(db) == 0, f"got {_key_rows(db)}")
rep = residual_report(db, "s")
check("A-ii4 report NOT_CLEAN with file_purge_pending naming B",
      rep["controlled"]["live_tables_clean"] is False
      and rep["status"] == "CONTROLLED_NOT_CLEAN"
      and _pending_ids(db) == [b["id"]])
check("A-ii5 file-byte sliding-window probe still recovers the PIN "
      "(key bytes in the WAL — honest NOT_CLEAN, not clean)",
      _file_byte_enumerate(str(path), db) == PIN,
      f"got {_file_byte_enumerate(str(path), db)}")
check("A-ii6 live-table enumeration finds nothing (no key row left)",
      _enumerate_pin(db) is None)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("A-ii7 erase A -> COMPLETE", out_a["status"] == "COMPLETE", out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("A-ii8 repair B -> COMPLETE, marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
rep2 = residual_report(db, "s")
check("A-ii9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True
      and rep2["controlled"]["file_purge_pending"] == [])
check("A-ii10 file-byte probe finds nothing after convergence",
      _file_byte_enumerate(str(path), db) is None)
db.close()

# A(iii) post_busy: crash after the checkpoint returned busy (at the
# key restore) — the restore never ran, so the key stays deleted and
# the marker stands. A repair re-run with the reader still held must
# stay PARTIAL with the marker kept (key_row None -> restore is a
# no-op); after the reader releases it converges to COMPLETE.
# The reader is held from BEFORE B's first erase, so no checkpoint in
# this DB ever succeeds and the WAL is never reset: B's key INSERT
# frame stays in the WAL, and the file-byte probe deterministically
# recovers the PIN pre-convergence. (A busy checkpoint that copies
# every frame — checkpointed == log — would already have purged the
# key from the live files; how many frames it copies depends on DB
# history volume, so the probe is only deterministic when the WAL was
# never reset.)
path = TMPROOT / "a-post-busy.sqlite3"
db = _db("a-post-busy")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
reader = _hold_reader(path)
out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
assert out_b["status"] == "PARTIAL", out_b["status"]
rev_b = _rev(db, b["id"])
db.close()
proc = _spawn_child(path, b["id"], rev_b, "repair", "post_busy")
# Reopen FIRST, while the reader is still held: closing the reader
# would be the last live connection closing, and SQLite checkpoints
# the WAL on a clean last close — purging the very key bytes the
# probe below must still find. The reopened connection keeps the WAL
# hot until the probe has read it.
db = open_db(path)
reader.execute("COMMIT")
reader.close()
check("A-iii1 child crashed at post_busy (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
check("A-iii2 marker stands after the crash", _pending_ids(db) == [b["id"]],
      str(_pending_ids(db)))
check("A-iii3 key row deleted, restore never ran (0)",
      _key_rows(db) == 0, f"got {_key_rows(db)}")
rep = residual_report(db, "s")
check("A-iii4 report NOT_CLEAN with file_purge_pending naming B",
      rep["controlled"]["live_tables_clean"] is False
      and _pending_ids(db) == [b["id"]])
check("A-iii5 file-byte probe still recovers the PIN (key bytes in the WAL)",
      _file_byte_enumerate(str(path), db) == PIN,
      f"got {_file_byte_enumerate(str(path), db)}")
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("A-iii6 erase A -> COMPLETE", out_a["status"] == "COMPLETE", out_a["status"])
reader = _hold_reader(path)
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("A-iii7 repair B with the reader held -> PARTIAL, marker kept",
      out_r["status"] == "PARTIAL" and out_r["maintenance_pending"] is True
      and _pending_ids(db) == [b["id"]], out_r["status"])
reader.execute("COMMIT")
reader.close()
out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("A-iii8 repair B after release -> COMPLETE, marker cleared",
      out_r2["status"] == "COMPLETE" and out_r2["maintenance_pending"] is False,
      out_r2["status"])
rep2 = residual_report(db, "s")
check("A-iii9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
check("A-iii10 file-byte probe finds nothing after convergence",
      _file_byte_enumerate(str(path), db) is None)
db.close()

# A(iv) post_checkpoint: crash after the checkpoint succeeded, before
# the marker clears — a STALE marker: the file is actually clean, the
# report stays NOT_CLEAN (fail-closed false positive), and a repair
# re-run clears it.
db, path, a, b = _crash_setup_pin("a-post-checkpoint")
rev_b = _rev(db, b["id"])
db.close()
proc = _spawn_child(path, b["id"], rev_b, "repair", "post_checkpoint")
check("A-iv1 child crashed at post_checkpoint (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("A-iv2 stale marker stands after the crash",
      _pending_ids(db) == [b["id"]], str(_pending_ids(db)))
check("A-iv3 key row deleted (0)", _key_rows(db) == 0, f"got {_key_rows(db)}")
rep = residual_report(db, "s")
check("A-iv4 report NOT_CLEAN (fail-closed) with file_purge_pending naming B",
      rep["controlled"]["live_tables_clean"] is False
      and _pending_ids(db) == [b["id"]])
check("A-iv5 file-byte probe finds nothing (checkpoint already succeeded)",
      _file_byte_enumerate(str(path), db) is None)
check("A-iv6 live-table enumeration finds nothing",
      _enumerate_pin(db) is None)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("A-iv7 erase A -> COMPLETE", out_a["status"] == "COMPLETE", out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("A-iv8 repair B -> COMPLETE, stale marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
rep2 = residual_report(db, "s")
check("A-iv9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True
      and rep2["controlled"]["file_purge_pending"] == [])
db.close()

# ---------------------------------------------------------------- B: first-erase path, four crash points
print("\n== B: first-erase path, four crash points ==")

# B(i) pre_pending: crash inside the erase transaction — atomic
# rollback: A stays live with its payload, no key row, no marker, no
# erase event for A.
db, path, a, b = _crash_setup_payload("b-pre-pending")
rev_a = _rev(db, a["id"])
db.close()
proc = _spawn_child(path, a["id"], rev_a, "first", "pre_pending")
check("B-i1 child crashed at pre_pending (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("B-i2 A still live after the rollback",
      _state(db, a["id"]) == "active", _state(db, a["id"]))
check("B-i3 A value intact after the rollback",
      json.loads(_value(db, a["id"])) == PAYLOAD)
check("B-i4 A revision unchanged by the rolled-back erase",
      _rev(db, a["id"]) == rev_a, f"got {_rev(db, a['id'])}")
check("B-i5 key rows still 1 (B's PARTIAL key; A's key never created)",
      _key_rows(db) == 1, f"got {_key_rows(db)}")
check("B-i6 no maintenance_pending marker", _pending_ids(db) == [])
check("B-i7 no erase event for A (still 1 erase event)",
      _erase_event_count(db) == 1, f"got {_erase_event_count(db)}")
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("B-i8 erase A after the rollback -> COMPLETE",
      out_a["status"] == "COMPLETE", out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("B-i9 repair B after the rollback -> COMPLETE",
      out_r["status"] == "COMPLETE", out_r["status"])
rep2 = residual_report(db, "s")
check("B-i10 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
check("B-i11 payload gone from the live files after convergence",
      not _payload_in_files(str(path), PAYLOAD))
db.close()

# B(ii) post_commit: crash right after the erase transaction commits
# — marker persisted, key deleted, checkpoint never ran. The key
# never hit disk (§1.3: no key-recovery chain), but the raw payload
# bytes sit in the WAL from the remember frame: the marker is what
# keeps the report honest until the repair re-run purges them.
db, path, a, b = _crash_setup_payload("b-post-commit")
rev_a = _rev(db, a["id"])
db.close()
proc = _spawn_child(path, a["id"], rev_a, "first", "post_commit")
check("B-ii1 child crashed at post_commit (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("B-ii2 marker stands after the crash", _pending_ids(db) == [a["id"]],
      str(_pending_ids(db)))
check("B-ii3 key rows still 1 — B's PARTIAL key; A's key was created "
      "and deleted in the same transaction and never persisted (§1.3)",
      _key_rows(db) == 1 and _key_record_ids(db) == {b["id"]},
      f"got {_key_rows(db)} {_key_record_ids(db)}")
rep = residual_report(db, "s")
check("B-ii4 report NOT_CLEAN with file_purge_pending naming A",
      rep["controlled"]["live_tables_clean"] is False
      and _pending_ids(db) == [a["id"]])
check("B-ii5 payload IS in the WAL bytes pre-convergence (§27 correction)",
      _payload_in_files(str(path), PAYLOAD))
check("B-ii6 file-byte probe finds no key (§1.3: key never hit disk)",
      _file_byte_enumerate(str(path), db) is None)
out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("B-ii7 repair A -> COMPLETE, marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("B-ii8 repair B -> COMPLETE", out_rb["status"] == "COMPLETE",
      out_rb["status"])
rep2 = residual_report(db, "s")
check("B-ii9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
check("B-ii10 payload gone from the live files after convergence",
      not _payload_in_files(str(path), PAYLOAD))
db.close()

# B(iii) post_busy: crash after the checkpoint returned busy — same
# payload-in-WAL state as (ii), reached via the busy path. The reader
# is held from BEFORE B's first erase, so the WAL is never reset and
# the payload's remember frames stay in it (a busy checkpoint that
# copies every frame would already have purged the payload from the
# live files — see A(iii)).
path = TMPROOT / "b-post-busy.sqlite3"
db = _db("b-post-busy")
a = remember(db, "s", "a-key", PAYLOAD, source_refs=SRC)
b = remember(db, "s", "b-key", PAYLOAD, source_refs=SRC)
reader = _hold_reader(path)
out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
assert out_b["status"] == "PARTIAL", out_b["status"]
rev_a = _rev(db, a["id"])
db.close()
proc = _spawn_child(path, a["id"], rev_a, "first", "post_busy")
# Reopen FIRST, while the reader is still held (see A(iii)): closing
# the reader would be the last live connection closing, and SQLite
# checkpoints the WAL on a clean last close — purging the very
# payload bytes the probe below must still find.
db = open_db(path)
reader.execute("COMMIT")
reader.close()
check("B-iii1 child crashed at post_busy (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
check("B-iii2 marker stands after the crash", _pending_ids(db) == [a["id"]],
      str(_pending_ids(db)))
check("B-iii3 key rows still 1 — B's PARTIAL key; A's key never persisted",
      _key_rows(db) == 1 and _key_record_ids(db) == {b["id"]},
      f"got {_key_rows(db)} {_key_record_ids(db)}")
rep = residual_report(db, "s")
check("B-iii4 report NOT_CLEAN with file_purge_pending naming A",
      rep["controlled"]["live_tables_clean"] is False
      and _pending_ids(db) == [a["id"]])
check("B-iii5 payload IS in the WAL bytes pre-convergence",
      _payload_in_files(str(path), PAYLOAD))
check("B-iii6 file-byte probe finds no key (§1.3 boundary)",
      _file_byte_enumerate(str(path), db) is None)
out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("B-iii7 repair A -> COMPLETE, marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("B-iii8 repair B -> COMPLETE", out_rb["status"] == "COMPLETE",
      out_rb["status"])
rep2 = residual_report(db, "s")
check("B-iii9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
check("B-iii10 payload gone from the live files after convergence",
      not _payload_in_files(str(path), PAYLOAD))
db.close()

# B(iv) post_checkpoint: crash after the checkpoint succeeded, before
# the marker clears — a STALE marker on the first-erase path: the
# checkpoint already purged the payload, so the file is clean and the
# report's NOT_CLEAN is a fail-closed false positive.
db, path, a, b = _crash_setup_payload("b-post-checkpoint")
rev_a = _rev(db, a["id"])
db.close()
proc = _spawn_child(path, a["id"], rev_a, "first", "post_checkpoint")
check("B-iv1 child crashed at post_checkpoint (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("B-iv2 stale marker stands after the crash",
      _pending_ids(db) == [a["id"]], str(_pending_ids(db)))
check("B-iv3 key rows still 1 — B's PARTIAL key; A's key never persisted",
      _key_rows(db) == 1 and _key_record_ids(db) == {b["id"]},
      f"got {_key_rows(db)} {_key_record_ids(db)}")
rep = residual_report(db, "s")
check("B-iv4 report NOT_CLEAN (fail-closed) with file_purge_pending naming A",
      rep["controlled"]["live_tables_clean"] is False
      and _pending_ids(db) == [a["id"]])
check("B-iv5 payload NOT in the live files (checkpoint already succeeded)",
      not _payload_in_files(str(path), PAYLOAD))
check("B-iv6 file-byte probe finds no key",
      _file_byte_enumerate(str(path), db) is None)
out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("B-iv7 repair A -> COMPLETE, stale marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("B-iv8 repair B -> COMPLETE", out_rb["status"] == "COMPLETE",
      out_rb["status"])
rep2 = residual_report(db, "s")
check("B-iv9 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True
      and rep2["controlled"]["file_purge_pending"] == [])
db.close()

# ---------------------------------------------------------------- C: maintenance unit, rollback, consumers
print("\n== C: _run_maintenance unit, pending-write rollback, consumers ==")
db = _db("c-unit")
path_c = TMPROOT / "c-unit.sqlite3"
reader = _hold_reader(path_c)
notes = _run_maintenance(db)
check("C1 busy checkpoint with held reader is reported",
      len(notes) == 1 and "wal_checkpoint" in notes[0] and "busy" in notes[0],
      "; ".join(notes))
check("C2 _purge_failed counts the checkpoint note", _purge_failed(notes) is True)
reader.execute("COMMIT")
reader.close()
notes2 = _run_maintenance(db)
check("C3 no reader -> maintenance empty", notes2 == [], "; ".join(notes2))
db.close()

# (1e) A pending-write failure rolls the whole erase back atomically:
# the key is not deleted, the event is not published, the record keeps
# its prior state, and the erase is retryable.
db, path, a, b = _crash_setup_pin("c-rollback")
rev_b = _rev(db, b["id"])
orig_set_pending = E._set_maintenance_pending


def _raise_pending(*_args, **_kwargs):
    raise RuntimeError("injected pending-write failure")


E._set_maintenance_pending = _raise_pending
raised = None
try:
    erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
except RuntimeError as exc:
    raised = str(exc)
finally:
    E._set_maintenance_pending = orig_set_pending
check("C4 pending-write failure propagates (erase fails, not silent)",
      raised == "injected pending-write failure", f"got {raised!r}")
check("C5 B still tombstoned with its revision unchanged after the rollback",
      _state(db, b["id"]) == "erased" and _rev(db, b["id"]) == rev_b,
      f"state={_state(db, b['id'])} rev={_rev(db, b['id'])}")
check("C6 B key row intact after the rollback (1)",
      _key_rows(db) == 1, f"got {_key_rows(db)}")
check("C7 no marker after the rollback", _pending_ids(db) == [])
check("C8 no repair event published (still 1 erase event)",
      _erase_event_count(db) == 1, f"got {_erase_event_count(db)}")
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("C9 erase A after the rollback -> COMPLETE",
      out_a["status"] == "COMPLETE", out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("C10 repair B after the rollback -> COMPLETE (retryable)",
      out_r["status"] == "COMPLETE", out_r["status"])
rep2 = residual_report(db, "s")
check("C11 report clean after the retry",
      rep2["controlled"]["live_tables_clean"] is True)
db.close()

# (1d) The stored-hash consumers in the "key deleted, marker present"
# state: the repair-reason vetting still works via the key-independent
# erase_flags, and "hashes exist but the key is gone" is a non-match,
# never an error. Payload "repair" is a label-equals-payload case, so
# the flags are the only thing vetting the reason after the crash.
path = TMPROOT / "c-consumers.sqlite3"
db = _db("c-consumers")
c = remember(db, "s", "c-key", "repair", source_refs=SRC)
d = remember(db, "s", "d-key", "repair", source_refs=SRC)
out_d = erase_record(db, "s", d["id"], _rev(db, d["id"]), "rotation")
check("C12 erase D (payload 'repair') -> PARTIAL (C still holds it)",
      out_d["status"] == "PARTIAL", out_d["status"])
check("C13 blocked_repair_reasons flags the payload label",
      _erased_record_flags(db, "s", d["id"])["blocked_repair_reasons"]
      == frozenset({"repair"}),
      str(_erased_record_flags(db, "s", d["id"])))
# C is erased first, so D's repair scan is COMPLETE (otherwise the
# repair would stay PARTIAL on the scan and never reach the marker
# write / key deletion the crash point is about). The child repairs D
# with reason "rotation": the default repair reason "repair" IS the
# payload here, so the reason gate would reject it before any crash
# point is reached (see crash_child.py).
out_c = erase_record(db, "s", c["id"], _rev(db, c["id"]), "rotation")
check("C14 erase C -> COMPLETE", out_c["status"] == "COMPLETE", out_c["status"])
rev_d = _rev(db, d["id"])
db.close()
proc = _spawn_child(path, d["id"], rev_d, "repair", "post_commit", "rotation")
check("C15 child crashed at post_commit (exit 77)",
      proc.returncode == 77, f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()[-200:]}")
db = open_db(path)
check("C16 marker stands, key deleted after the crash",
      _pending_ids(db) == [d["id"]] and _key_rows(db) == 0,
      f"pending={_pending_ids(db)} keys={_key_rows(db)}")
check("C17 no verification key remains for D",
      _record_verification_keys(db, "s", d["id"]) == [])
check("C18 the stored needle hashes are still persisted (2)",
      len(_stored_needle_hashes(db, "s", d["id"])) == 2,
      f"got {len(_stored_needle_hashes(db, 's', d['id']))}")
raised = None
try:
    _check_reason_against_stored_hashes(db, "s", d["id"], "repair")
except TurnkeepError as exc:
    raised = exc.code
check("C19 reason vetting still rejects 'repair' with the key gone (flags)",
      raised == "INVALID_INPUT", f"got {raised!r}")
check("C20 reason vetting still passes an unrelated label ('rotation')",
      _check_reason_against_stored_hashes(db, "s", d["id"], "rotation") is None)
raised = None
try:
    erase_record(db, "s", d["id"], _rev(db, d["id"]), "repair")
except TurnkeepError as exc:
    raised = exc.code
check("C21 full repair path rejects reason 'repair' with the key gone",
      raised == "INVALID_INPUT", f"got {raised!r}")
out_r = erase_record(db, "s", d["id"], _rev(db, d["id"]), "rotation")
check("C22 repair D with 'rotation' -> COMPLETE, marker cleared",
      out_r["status"] == "COMPLETE" and out_r["maintenance_pending"] is False,
      out_r["status"])
rep2 = residual_report(db, "s")
check("C23 report clean after convergence",
      rep2["controlled"]["live_tables_clean"] is True)
db.close()

# ---------------------------------------------------------------- D: doc checks (docs copies only)
print("\n== D: KNOWN_LIMITATIONS §26/§27 + SECURITY.md §5 ==")
kl = Path(__file__).resolve().parents[1] / "docs" / "KNOWN_LIMITATIONS.md"
if kl.exists():
    text = kl.read_text(encoding="utf-8")
    s26 = text.split("## 26.", 1)[1].split("## 27.", 1)[0]
    s27 = text.split("## 27.", 1)[1]
    check("D1 §26 has no remaining TBD", "TBD" not in s26)
    check("D2 §27 exists", "## 27." in text)
    check("D3 §27 records the UNCONDITIONAL marker decision",
          "UNCONDITIONAL" in s27)
    check("D4 §27 defines the stale marker as 'purge UNCONFIRMED'",
          "purge UNCONFIRMED" in s27)
    check("D5 §27 records the open_db crash-recovery decision",
          "open_db crash-recovery" in s27)
    check("D6 §27 carries the R15 TBD forward",
          "TBD after R15 verdict" in s27)
else:
    print("SKIP D1-D6: no KNOWN_LIMITATIONS.md next to this copy (wheel run)")
sec = Path(__file__).resolve().parents[1] / "SECURITY.md"
if sec.exists():
    stext = sec.read_text(encoding="utf-8")
    check("D7 T042 VERIFIED in SECURITY.md",
          "Real-model evaluation VERIFIED (T042)" in stext)
    check("D8 T026 VERIFIED in SECURITY.md",
          "two-client live\n  handoff VERIFIED (T026)" in stext
          or "two-client live handoff VERIFIED (T026)" in stext)
    check("D9 strict live-clients gate still NOT_RUN",
          "strict live-clients gate itself is still NOT_RUN" in stext)
    check("D10 no stale NOT_RUN bullet remains",
          "Real-model evaluation NOT_RUN (T042)" not in stext
          and "two-client live\n  handoff NOT_RUN (T026)" not in stext
          and "two-client live handoff NOT_RUN (T026)" not in stext)
else:
    print("SKIP D7-D10: no SECURITY.md next to this copy (wheel run)")

# ---------------------------------------------------------------- E: R14 original-scope spot checks
print("\n== E: R14 original-scope spot checks ==")
db = _db("e")
path_e = TMPROOT / "e.sqlite3"
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
check("E1 erase B -> PARTIAL, key row kept",
      out_b["status"] == "PARTIAL" and _key_rows(db) == 1, out_b["status"])
metas = _all_erase_metas(db)
check("E2 PARTIAL erase persists needle hashes (2)",
      len(metas[0].get("needle_hashes", []) or []) == 2,
      f"got {len(metas[0].get('needle_hashes', []) or [])}")
reader = _hold_reader(path_e)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("E3 erase A under a held reader -> PARTIAL + marker",
      out_a["status"] == "PARTIAL" and out_a["maintenance_pending"] is True,
      out_a["status"])
out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("E4 repair B under a held reader -> PARTIAL + marker",
      out_r["status"] == "PARTIAL" and out_r["maintenance_pending"] is True,
      out_r["status"])
rep = residual_report(db, "s")
check("E5 report NOT_CLEAN with both records pending",
      rep["controlled"]["live_tables_clean"] is False
      and set(_pending_ids(db)) == {a["id"], b["id"]})
reader.execute("COMMIT")
reader.close()
out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
check("E6 repair B after release -> COMPLETE",
      out_r2["status"] == "COMPLETE", out_r2["status"])
out_a2 = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("E7 repair A after release -> COMPLETE",
      out_a2["status"] == "COMPLETE", out_a2["status"])
rep2 = residual_report(db, "s")
check("E8 report clean, key rows 0, enumeration finds nothing",
      rep2["controlled"]["live_tables_clean"] is True
      and _key_rows(db) == 0 and _enumerate_pin(db) is None)
check("E9 file-byte probe finds nothing after the chain",
      _file_byte_enumerate(str(path_e), db) is None)
db.close()
# "202" / "2026" still COMPLETE.
for payload in ("202", "2026"):
    db3 = _db(f"e-{payload}")
    rec3 = remember(db3, "s", "k", payload, source_refs=SRC)
    out3 = erase_record(db3, "s", rec3["id"], _rev(db3, rec3["id"]), "rotation")
    check(f"E10 payload {payload!r} still COMPLETE",
          out3["status"] == "COMPLETE", out3["status"])
    db3.close()
# Label decode chains (R12 P1): Base64("COMPLETE") / Base64("fact").
db2 = _db("e-label")
p_complete = base64.b64encode(b"COMPLETE").decode()
rec = remember(db2, "s", "k", p_complete, source_refs=SRC)
out = erase_record(db2, "s", rec["id"], _rev(db2, rec["id"]), "rotation")
check("E11 Base64(COMPLETE) payload erases PARTIAL (status_label)",
      out["status"] == "PARTIAL", out["status"])
p_fact = base64.b64encode(b"fact").decode()
rec2 = remember(db2, "s", "k2", p_fact, source_refs=SRC)
out2 = erase_record(db2, "s", rec2["id"], _rev(db2, rec2["id"]), "rotation")
check("E12 Base64(fact) payload erases PARTIAL (kind_label)",
      out2["status"] == "PARTIAL", out2["status"])
db2.close()

# ---------------------------------------------------------------- F: .pre-v7.bak behaviour
print("\n== F: .pre-v7.bak behaviour ==")
# F1: a fresh DB's backup is created before any payload exists.
p_fresh = TMPROOT / "f-fresh.sqlite3"
db = open_db(p_fresh)
register_scope(db, "s")
rec = remember(db, "s", "k", "SENSITIVE-PAYLOAD-7f3a", source_refs=SRC)
bak = Path(str(p_fresh) + ".pre-v7.bak")
check("F1 fresh open writes a .pre-v7.bak", bak.exists())
check("F2 fresh .bak holds no payload (created before it existed)",
      bak.exists() and b"SENSITIVE-PAYLOAD-7f3a" not in bak.read_bytes())
db.close()
# F3: a populated v5 DB upgraded to v7 writes NO new backup. Build the
# v5 shape by downgrading the just-made v7 DB, snapshot the .bak, re-open.
raw = sqlite3.connect(str(p_fresh))
raw.execute("PRAGMA user_version=5")
raw.execute("DROP TABLE maintenance_pending")
raw.execute("DROP TABLE purge_pending")
raw.execute("DELETE FROM schema_meta WHERE version=6")
raw.execute("DELETE FROM schema_meta WHERE version=7")
raw.commit()
raw.close()
before = bak.read_bytes()
before_mtime = bak.stat().st_mtime_ns
db = open_db(p_fresh)
# R16 rework (P0-R16-03): migration 007 bumps the schema to v7.
check("F3 populated v5 DB upgrades to v7 on open",
      db.execute("PRAGMA user_version").fetchone()[0] == 7)
check("F4 upgrade wrote no new .bak (bytes + mtime unchanged)",
      bak.read_bytes() == before and bak.stat().st_mtime_ns == before_mtime)
check("F5 upgraded .bak still holds no payload",
      b"SENSITIVE-PAYLOAD-7f3a" not in bak.read_bytes())
check("F6 payload survives the upgrade",
      db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 1)
db.close()
# F7: a planted .bak next to the live DB is listed as uncontrolled.
p_plant = TMPROOT / "f-plant.sqlite3"
db = open_db(p_plant)
register_scope(db, "s")
planted = Path(str(p_plant) + ".pre-v6.bak")
planted.write_bytes(b"planted backup copy holding SENSITIVE-PAYLOAD-7f3a")
rep = residual_report(db, "s")
check("F7 planted .bak is listed as an uncontrolled backup copy",
      any(str(planted) in u.get("path", "") for u in rep["uncontrolled"]))
db.close()

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
