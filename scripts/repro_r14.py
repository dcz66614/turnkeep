"""Repro for the R14 rework (GPT R13 verdict, P0-R13-01 + P2-R13-02).
Runs from a source checkout, an extracted RC, or an installed wheel
against throwaway DBs.

Sections:
  A: P0-R13-01 — busy WAL checkpoint (both reader variants): busy ->
     PARTIAL + maintenance_pending marker, report not clean, key rows
     restored (enumeration still finds the PIN — honest, not clean);
     reader released -> repair re-run -> COMPLETE, marker cleared, key
     rows 0, live-key and file-byte enumeration find nothing.
  B: _run_maintenance unit — busy=1 with a held reader, [] without;
     _purge_failed counts checkpoint notes only, never VACUUM notes.
  C: No-reader control — the normal COMPLETE path is unchanged.
  D: P2-R13-02 — SECURITY.md §5: T042 + T026 VERIFIED, strict
     live-clients gate still NOT_RUN. (Skipped when SECURITY.md is not
     next to this copy — the installed wheel carries no docs.)
  E: R13 original-scope spot checks.
  F: .pre-v7.bak behaviour — a populated upgrade writes no backup;
     a fresh-DB backup holds no payload; a planted .bak is listed as
     an uncontrolled backup copy.

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT in
/tmp (tmpfs, 512M). This script removes its TMPROOT on success
(registered with atexit); on failure it keeps it and prints the path,
so the failing DBs can be inspected.
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
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import turnkeep  # noqa: F401  (proves the import resolves: source, RC or wheel)
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.erasure import (
    erase_record, residual_report, _run_maintenance, _purge_failed,
    _stored_needle_hashes,
)
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r14-"))
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


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


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


def _pending_ids(db):
    rep = residual_report(db, "s")
    return [p["record_id"] for p in rep["controlled"]["file_purge_pending"]]


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: P0-R13-01, both reader variants
print("\n== A: P0-R13-01 busy checkpoint (both reader variants) ==")
for variant in ("read-key", "count-only"):
    path = TMPROOT / f"a-{variant}.sqlite3"
    db = _db(f"a-{variant}")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
    check(f"A1-{variant} erase B -> PARTIAL", out_b["status"] == "PARTIAL", out_b["status"])
    # Second connection holds a long read transaction (an ordinary
    # reader — it never reads the key table in the count-only variant).
    db2 = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    db2.execute("BEGIN")
    if variant == "read-key":
        db2.execute("SELECT secret_hex FROM record_erase_keys").fetchall()
    else:
        db2.execute("SELECT COUNT(*) FROM memories").fetchone()
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    check(f"A2-{variant} erase A -> PARTIAL (busy checkpoint)",
          out_a["status"] == "PARTIAL", out_a["status"])
    check(f"A3-{variant} erase A maintenance names the busy checkpoint",
          any("wal_checkpoint" in n and "busy" in n for n in out_a["maintenance"]),
          "; ".join(out_a["maintenance"]))
    check(f"A4-{variant} erase A maintenance_pending=True",
          out_a["maintenance_pending"] is True)
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    check(f"A5-{variant} repair B -> PARTIAL (busy checkpoint)",
          out_r["status"] == "PARTIAL" and out_r["maintenance_pending"] is True,
          out_r["status"])
    rep = residual_report(db, "s")
    check(f"A6-{variant} report live_tables_clean=false while pending",
          rep["controlled"]["live_tables_clean"] is False)
    check(f"A7-{variant} report status CONTROLLED_NOT_CLEAN",
          rep["status"] == "CONTROLLED_NOT_CLEAN", rep["status"])
    check(f"A8-{variant} file_purge_pending names both records",
          set(_pending_ids(db)) == {a["id"], b["id"]}, str(_pending_ids(db)))
    check(f"A9-{variant} key rows restored (2)", _key_rows(db) == 2,
          f"got {_key_rows(db)}")
    check(f"A10-{variant} live-key enumeration still finds PIN (honest, not clean)",
          _enumerate_pin(db) == PIN, f"got {_enumerate_pin(db)}")
    # Release the reader: a repair re-run retries the checkpoint.
    db2.execute("COMMIT")
    db2.close()
    out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    check(f"A11-{variant} repair B after release -> COMPLETE",
          out_r2["status"] == "COMPLETE" and out_r2["maintenance_pending"] is False,
          out_r2["status"])
    check(f"A12-{variant} repair B maintenance empty",
          out_r2["maintenance"] == [], "; ".join(out_r2["maintenance"]))
    out_a2 = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    check(f"A13-{variant} repair A after release -> COMPLETE",
          out_a2["status"] == "COMPLETE" and out_a2["maintenance_pending"] is False,
          out_a2["status"])
    rep2 = residual_report(db, "s")
    check(f"A14-{variant} report live_tables_clean=true after retry",
          rep2["controlled"]["live_tables_clean"] is True)
    check(f"A15-{variant} file_purge_pending empty after retry",
          rep2["controlled"]["file_purge_pending"] == [])
    check(f"A16-{variant} key rows 0 after retry", _key_rows(db) == 0,
          f"got {_key_rows(db)}")
    check(f"A17-{variant} live-key enumeration finds nothing after retry",
          _enumerate_pin(db) is None, f"got {_enumerate_pin(db)}")
    check(f"A18-{variant} file-byte sliding-window enumeration finds nothing",
          _file_byte_enumerate(str(path), db) is None,
          f"got {_file_byte_enumerate(str(path), db)}")
    db.close()

# ---------------------------------------------------------------- B: _run_maintenance unit
print("\n== B: _run_maintenance unit ==")
db = _db("b")
path_b = TMPROOT / "b.sqlite3"
db2 = sqlite3.connect(str(path_b), timeout=10.0, isolation_level=None)
db2.execute("BEGIN")
db2.execute("SELECT COUNT(*) FROM memories").fetchone()
notes = _run_maintenance(db)
check("B1 busy checkpoint with held reader is reported",
      len(notes) == 1 and "wal_checkpoint" in notes[0] and "busy" in notes[0],
      "; ".join(notes))
check("B2 _purge_failed counts the checkpoint note", _purge_failed(notes) is True)
db2.execute("COMMIT")
db2.close()
notes2 = _run_maintenance(db)
check("B3 no reader -> maintenance empty", notes2 == [], "; ".join(notes2))
check("B4 _purge_failed ignores a VACUUM-only note",
      _purge_failed(["VACUUM failed: database is locked"]) is False)
check("B5 _purge_failed ignores an empty list", _purge_failed([]) is False)
db.close()

# ---------------------------------------------------------------- C: no-reader control
print("\n== C: no-reader control (normal COMPLETE path unchanged) ==")
db = _db("c")
path_c = TMPROOT / "c.sqlite3"
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("C1 erase A -> PARTIAL (B still holds the PIN)",
      out_a["status"] == "PARTIAL", out_a["status"])
out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
check("C2 erase B -> COMPLETE, maintenance empty, not pending",
      out_b["status"] == "COMPLETE" and out_b["maintenance"] == []
      and out_b["maintenance_pending"] is False, out_b["status"])
out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("C3 repair A -> COMPLETE, maintenance empty, not pending",
      out_r["status"] == "COMPLETE" and out_r["maintenance"] == []
      and out_r["maintenance_pending"] is False, out_r["status"])
rep = residual_report(db, "s")
check("C4 report clean", rep["controlled"]["live_tables_clean"] is True
      and rep["controlled"]["file_purge_pending"] == [])
check("C5 key rows 0", _key_rows(db) == 0, f"got {_key_rows(db)}")
check("C6 live-key enumeration finds nothing", _enumerate_pin(db) is None)
check("C7 file-byte enumeration finds nothing",
      _file_byte_enumerate(str(path_c), db) is None)
db.close()

# ---------------------------------------------------------------- D: SECURITY.md (docs copies only)
print("\n== D: P2-R13-02 SECURITY.md §5 ==")
sec = Path(__file__).resolve().parents[1] / "SECURITY.md"
if sec.exists():
    text = sec.read_text(encoding="utf-8")
    check("D1 T042 VERIFIED in SECURITY.md",
          "Real-model evaluation VERIFIED (T042)" in text)
    check("D2 T026 VERIFIED in SECURITY.md",
          "two-client live\n  handoff VERIFIED (T026)" in text
          or "two-client live handoff VERIFIED (T026)" in text)
    check("D3 strict live-clients gate still NOT_RUN",
          "strict live-clients gate itself is still NOT_RUN" in text)
    check("D4 no stale NOT_RUN bullet remains",
          "Real-model evaluation NOT_RUN (T042)" not in text
          and "two-client live\n  handoff NOT_RUN (T026)" not in text
          and "two-client live handoff NOT_RUN (T026)" not in text)
else:
    print("SKIP D: no SECURITY.md next to this copy (wheel run)")

# ---------------------------------------------------------------- E: R13 original-scope spot checks
print("\n== E: R13 original-scope spot checks ==")
db = _db("e")
a = remember(db, "s", "a-key", PIN, source_refs=SRC)
b = remember(db, "s", "b-key", PIN, source_refs=SRC)
out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
check("E1 erase A -> PARTIAL, key row kept",
      out_a["status"] == "PARTIAL" and _key_rows(db) == 1, out_a["status"])
metas = _all_erase_metas(db)
check("E2 PARTIAL erase persists needle hashes",
      len(metas[0].get("needle_hashes", []) or []) == 2,
      f"got {len(metas[0].get('needle_hashes', []) or [])}")
out_b = erase_record(db, "s", b["id"], _rev(db, b["id"]), "rotation")
out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
check("E3 repair A -> COMPLETE, key row deleted",
      out_r["status"] == "COMPLETE" and _key_rows(db) == 0, out_r["status"])
# Label decode chain (R12 P1): Base64("COMPLETE") payload -> PARTIAL.
db2 = _db("e-label")
p_complete = base64.b64encode(b"COMPLETE").decode()
rec = remember(db2, "s", "k", p_complete, source_refs=SRC)
out = erase_record(db2, "s", rec["id"], _rev(db2, rec["id"]), "rotation")
check("E4 Base64(COMPLETE) payload erases PARTIAL (status_label)",
      out["status"] == "PARTIAL", out["status"])
# Kind chain: Base64("fact") payload -> PARTIAL (kind_label).
p_fact = base64.b64encode(b"fact").decode()
rec2 = remember(db2, "s", "k2", p_fact, source_refs=SRC)
out2 = erase_record(db2, "s", rec2["id"], _rev(db2, rec2["id"]), "rotation")
check("E5 Base64(fact) payload erases PARTIAL (kind_label)",
      out2["status"] == "PARTIAL", out2["status"])
# "202" / "2026" still COMPLETE.
for payload in ("202", "2026"):
    db3 = _db(f"e-{payload}")
    rec3 = remember(db3, "s", "k", payload, source_refs=SRC)
    out3 = erase_record(db3, "s", rec3["id"], _rev(db3, rec3["id"]), "rotation")
    check(f"E6 payload {payload!r} still COMPLETE",
          out3["status"] == "COMPLETE", out3["status"])
    db3.close()
db.close()
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
