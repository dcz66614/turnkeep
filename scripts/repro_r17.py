"""Repro for the R17 rework (GPT R16 verdict: P0-R16-01 busy
checkpoint, P0-R16-03 no persistent NOT_CLEAN state, P1-R16-02 PIN
leak through the probe). Runs from a source checkout, an extracted
RC, or an installed wheel against throwaway DBs. The
historical-residue fixture is built by an independent subprocess
(scripts/off_residue_child.py) that runs the OFF chain and crashes
without closing, so the deleted keys' bytes stay in the main DB
file; the child also records B's deleted verification key hex to a
sidecar file (scripts/off_residue_child.py 6th argument).

Sections:
  A: (a) the busy checkpoint: six-digit value + held reader (the
     probe is blind to it — a disclosed blind spot — yet the busy
     checkpoint alone fails the purge); four-digit PIN + held
     reader (probe and busy check agree, independently).
  B: (b) the purge_pending marker: failure persists it, a reopen
     still reports CONTROLLED_NOT_CLEAN, a second failure keeps it,
     a repair re-run does not clear it, and only a successful
     purge_residue re-check clears it (retry convergence).
  C: (c) no PIN in any output surface: CLI --json and plain stdout
     on a residue DB (success), the API return dict, and the two
     failure cases (probe forced to keep finding residue; reader
     held so the checkpoint is busy) — all must contain no erased
     value.
  D: happy-path spot checks: F0 cleanup, F4-style clean DB
     idempotence, _verify_purge returns a bool, the probe answers
     booleans only.
  E: docs — §29 present with the R16 rework record and no TBD, §28
     corrected ("never-lies" gone, R15 needles intact), SECURITY.md
     has no TBD. (Skipped when the docs are not next to this copy —
     the installed wheel carries no docs.)
  F: schema v7 + purge_pending migration present in a fresh DB.

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT in
/tmp (tmpfs, 512M). This script removes its TMPROOT on success
(registered with atexit); on failure it keeps it and prints the
path, so the failing DBs can be inspected.
"""
import atexit
import contextlib
import hashlib
import hmac
import io
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
    erase_record, residual_report, purge_residue,
    _purge_pending, _probe_files, _verify_purge,
    _candidate_recovers_pin, _all_stored_needle_hashes,
)
from turnkeep.cli import main as cli_main

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
SIX = "864218"
OFF_RESIDUE_CHILD = Path(__file__).resolve().parent / "off_residue_child.py"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r17-"))
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
    return _blind_scan_file(path, db), _blind_scan_file(str(path) + "-wal", db)


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
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env, encoding="utf-8")


def _make_off_residue(name, value):
    """An F0 historical-residue DB: the OFF chain (B PARTIAL, A
    COMPLETE, repair B COMPLETE) crashed without closing, so the
    deleted key's bytes stay in the main DB file. Returns
    (db, path, key_hex) with the connection open — the caller must
    keep it open (a clean close would checkpoint the residue away).
    key_hex is B's deleted verification key, recorded by the child
    before the repair deleted the row."""
    path = TMPROOT / f"{name}.sqlite3"
    db = _db(name)
    a = remember(db, "s", "a-key", value, source_refs=SRC)
    b = remember(db, "s", "b-key", value, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    key_out = TMPROOT / f"{name}.keyhex"
    db.close()
    res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev, key_out)
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    assert _key_rows(db) == 0
    assert _marker_rows(db) == 0
    return db, path, key_out.read_text(encoding="ascii")


def _setup_value(name, value):
    """Two records sharing `value`, B erased PARTIAL (A still holds
    the payload)."""
    path = TMPROOT / f"{name}.sqlite3"
    db = _db(name)
    a = remember(db, "s", "a-key", value, source_refs=SRC)
    b = remember(db, "s", "b-key", value, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    return db, path, a, b


def _converge_value(db, path, a, b):
    assert erase_record(db, "s", a["id"], _rev(db, a["id"]),
                        "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "repair")["status"] == "COMPLETE"


def _cli_stdout(argv):
    """Run the CLI in-process and capture its stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: busy checkpoint
print("\n== A: purge_residue must fail on a busy checkpoint ==")
db, path, key_hex = _make_off_residue("a-six", SIX)
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
check("A1 six-digit + reader: cleaned False, checkpoint busy == 1, "
      "error names the busy triple",
      out["cleaned"] is False and out["checkpoint"] is not None
      and out["checkpoint"][0] == 1
      and out["error"] == f"PRAGMA wal_checkpoint(TRUNCATE) busy: {out['checkpoint']}",
      str(out["checkpoint"]))
check("A2 six-digit + reader: probe is blind (residue_* False) — and "
      "that does NOT compensate for busy == 1",
      out["residue_before"] is False and out["residue_after"] is False
      and "probe_before" not in out and "probe_after" not in out)
check("A3 six-digit + reader: deleted key still in the main DB file",
      key_hex.encode("ascii") in Path(path).read_bytes())
check("A4 six-digit + reader: failure persisted, report NOT_CLEAN, "
      "marker detail holds no value",
      _purge_pending_rows(db) == 1
      and SIX not in _purge_pending(db, "s")[0]["detail"]
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and "purge_residue:s" in _pending_ids(db))
db.close()

db, path, key_hex = _make_off_residue("a-pin", PIN)
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
check("A5 four-digit + reader: cleaned False, checkpoint busy == 1, "
      "error names the busy triple",
      out["cleaned"] is False and out["checkpoint"] is not None
      and out["checkpoint"][0] == 1
      and out["error"] == f"PRAGMA wal_checkpoint(TRUNCATE) busy: {out['checkpoint']}",
      str(out["checkpoint"]))
check("A6 four-digit + reader: probe finds the residue (residue_* "
      "True) — busy and probe are two independent requirements",
      out["residue_before"] is True and out["residue_after"] is True
      and key_hex.encode("ascii") in Path(path).read_bytes()
      and PIN not in _purge_pending(db, "s")[0]["detail"])
db.close()

# ---------------------------------------------------------------- B: marker
print("\n== B: a failed cleanup persists a NOT_CLEAN state ==")
db, path, _key_hex = _make_off_residue("b-reopen", PIN)
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
assert out["cleaned"] is False
assert _purge_pending_rows(db) == 1
db.close()
db = open_db(path)
check("B1 after a failed purge, reopening still reports "
      "CONTROLLED_NOT_CLEAN with the persisted marker",
      _purge_pending_rows(db) == 1
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is False
      and "purge_residue:s" in _pending_ids(db))
out = purge_residue(db)
check("B2 retry after the reader releases: cleaned True, marker "
      "cleared, report clean, blind scan finds nothing",
      out["cleaned"] is True and out["checkpoint"] is not None
      and out["checkpoint"][0] == 0 and out["residue_after"] is False
      and _purge_pending_rows(db) == 0
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True
      and _blind_scan_main_wal(str(path), db) == (None, None))
db.close()

db, path, _key_hex = _make_off_residue("b-second", PIN)
reader = _hold_reader(path)
try:
    out1 = purge_residue(db)
    out2 = purge_residue(db)
finally:
    reader.close()
check("B3 a second failed purge keeps the marker (clearing is "
      "success-only)",
      out1["cleaned"] is False and out2["cleaned"] is False
      and _purge_pending_rows(db) == 1
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN")
db.close()

db, path, _key_hex = _make_off_residue("b-repair", SIX)
c = remember(db, "s", "c-key", "1357", source_refs=SRC)
d = remember(db, "s", "d-key", "1357", source_refs=SRC)
assert erase_record(db, "s", d["id"], _rev(db, d["id"]),
                    "rotation")["status"] == "PARTIAL"
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
assert out["cleaned"] is False and _purge_pending_rows(db) == 1
assert erase_record(db, "s", c["id"], _rev(db, c["id"]),
                    "rotation")["status"] == "COMPLETE"
assert erase_record(db, "s", d["id"], _rev(db, d["id"]),
                    "repair")["status"] == "COMPLETE"
check("B4 a repair re-run that converges an unrelated PARTIAL "
      "record does NOT clear the historical marker",
      _purge_pending_rows(db) == 1
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and "purge_residue:s" in _pending_ids(db))
out = purge_residue(db)
check("B5 only a successful purge_residue re-check clears it",
      out["cleaned"] is True and _purge_pending_rows(db) == 0
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True)
db.close()

# ---------------------------------------------------------------- C: outputs
print("\n== C: no erased value in any output surface ==")
db, path, _key_hex = _make_off_residue("c-json", PIN)
rc, raw = _cli_stdout(["--db", str(path), "--json", "purge-residue"])
check("C1 CLI --json on a residue DB: exit 0, residue_before True, "
      "no PIN in stdout",
      rc == 0 and json.loads(raw)["residue_before"] is True
      and PIN not in raw, str(rc))
db.close()

db, path, _key_hex = _make_off_residue("c-plain", PIN)
rc, raw = _cli_stdout(["--db", str(path), "purge-residue"])
check("C2 CLI plain mode on a residue DB: exit 0, no PIN in stdout",
      rc == 0 and json.loads(raw)["residue_before"] is True
      and PIN not in raw, str(rc))
db.close()

db, path, _key_hex = _make_off_residue("c-api", PIN)
out = purge_residue(db)
check("C3 API return dict: booleans only, no probe_* keys, no PIN "
      "anywhere in the dict",
      out["cleaned"] is True and out["residue_before"] is True
      and out["residue_after"] is False
      and "probe_before" not in out and "probe_after" not in out
      and PIN not in json.dumps(out))
db.close()

db, path, a, b = _setup_value("c-fail", PIN)
_converge_value(db, path, a, b)
db.close()
real_probe = E._probe_files
E._probe_files = lambda _db, _hashes: True
try:
    rc, raw = _cli_stdout(["--db", str(path), "--json", "purge-residue"])
finally:
    E._probe_files = real_probe
check("C4 CLI --json on a forced failure: exit 2, no PIN in stdout",
      rc == 2 and json.loads(raw)["cleaned"] is False
      and PIN not in raw, str(rc))

db, path, _key_hex = _make_off_residue("c-busy", PIN)
reader = _hold_reader(path)
try:
    rc, raw = _cli_stdout(["--db", str(path), "--json", "purge-residue"])
finally:
    reader.close()
check("C5 CLI --json with the checkpoint busy: exit 2, no PIN in "
      "stdout",
      rc == 2 and json.loads(raw)["cleaned"] is False
      and PIN not in raw, str(rc))
db.close()

# ---------------------------------------------------------------- D: happy path
print("\n== D: happy-path spot checks ==")
db, path, _key_hex = _make_off_residue("d-f0", PIN)
out = purge_residue(db)
check("D1 F0 cleanup: cleaned True, residue_before True, "
      "residue_after False, blind scan finds nothing",
      out["cleaned"] is True and out["residue_before"] is True
      and out["residue_after"] is False
      and _blind_scan_main_wal(str(path), db) == (None, None))
db.close()

db, path, a, b = _setup_value("d-f4", PIN)
_converge_value(db, path, a, b)
out1 = purge_residue(db)
out2 = purge_residue(db)
check("D2 F4-style clean DB: purge is idempotent, residue_* False",
      out1["cleaned"] is True and out2["cleaned"] is True
      and out1["residue_before"] is False
      and out1["residue_after"] is False
      and out2["residue_before"] is False
      and out2["residue_after"] is False)
db.close()

db, path, _key_hex = _make_off_residue("d-verify", PIN)
hashes = _all_stored_needle_hashes(db, "s")
check("D3 _verify_purge returns a bool (False on residue, True on "
      "a clean DB)",
      _verify_purge(db, _key_hex, hashes) is False
      and isinstance(_verify_purge(db, _key_hex, hashes), bool))
db.close()
db2 = _db("d-verify-clean")
check("D3b _verify_purge is True on a clean DB",
      _verify_purge(db2, "ab" * 32, set()) is True)
db2.close()

key = bytes.fromhex("11" * 32)
hashes = {hmac.new(key, PIN.encode(), hashlib.sha256).hexdigest()}
check("D4 _candidate_recovers_pin answers booleans only",
      _candidate_recovers_pin(key, hashes) is True
      and _candidate_recovers_pin(bytes.fromhex("cd" * 32), hashes) is False
      and _candidate_recovers_pin(key, set()) is False)
db_probe = _db("d-probe")
check("D5 _probe_files returns a bool (never a recovered value)",
      _probe_files(db_probe, hashes) is False
      and isinstance(_probe_files(db_probe, hashes), bool))
db_probe.close()

# ---------------------------------------------------------------- E: docs
print("\n== E: KNOWN_LIMITATIONS §29/§28 + SECURITY.md ==")
doc_root = Path(__file__).resolve().parents[1]
kl = doc_root / "docs" / "KNOWN_LIMITATIONS.md"
sec = doc_root / "SECURITY.md"
if not kl.exists():
    print("SKIP E1-E4: no KNOWN_LIMITATIONS.md next to this copy (wheel run)")
else:
    text = kl.read_text(encoding="utf-8")
    assert "## 29." in text
    sec29 = text.split("## 29.")[1]
    check("E1 §29 exists, names all three findings and the fix, "
          "and holds no TBD",
          "TBD" not in sec29
          and all(n in sec29 for n in (
              "P0-R16-01", "P0-R16-03", "P1-R16-02", "purge_pending",
              "007_purge_pending.sql", "residue_before", "residue_after",
              "busy", "CONTROLLED_NOT_CLEAN", "purge_residue",
              "OFFLINE_RC_READY", "PUBLIC_CANDIDATE NOT MET")))
    sec28 = text.split("## 28.")[1].split("## 29.")[0]
    check("E2 §28 'never-lies' corrected, R15 needles intact",
          "never-lies" not in sec28 and "never lies" not in sec28
          and all(n in sec28 for n in (
              "P0-R15-01", "purge_residue", "F0", "F4",
              "the recoverable material is the deleted verification KEY")))
    check("E3 SECURITY.md holds no TBD",
          "TBD" not in sec.read_text(encoding="utf-8"))

# ---------------------------------------------------------------- F: schema
print("\n== F: schema v7 ==")
db = _db("f-schema")
check("F1 SCHEMA_VERSION == 7 and the purge_pending table exists in "
      "a fresh DB",
      SCHEMA_VERSION == 7
      and db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                     "AND name='purge_pending'").fetchone()[0] == 1
      and db.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 7)
db.close()

print("\n== summary ==")
fails = [n for n, ok in CHECKS if not ok]
print(f"{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed")
if fails:
    print("FAILURES:")
    for n in fails:
        print("  - " + n)
    sys.exit(1)
