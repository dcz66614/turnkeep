"""Repro for the R18 rework (GPT R17 verdict: PASS_WITH_P2 — 2 new
P2s, 0 new P0 / 0 new P1). Runs from a source checkout, an extracted
RC, or an installed wheel against throwaway DBs. The
historical-residue fixture is built by an independent subprocess
(scripts/off_residue_child.py) that runs the OFF chain and crashes
without closing, so the deleted keys' bytes stay in the main DB
file; the child also records B's deleted verification key hex to a
sidecar file (scripts/off_residue_child.py 6th argument).

Sections:
  A: (1) P2-R17-01 — the global purge_residue collects the UNION of
     erase-event scopes and purge_pending scopes: a ghost-scope
     marker (a scope with no erase event) is cleared by a global
     run; an explicit scope run still settles only that scope; a
     global failure keeps every marker in the union and reports
     NOT_CLEAN; a global success after the reader releases clears
     them all, with no reverse error and no false-positive residue.
  B: R17 original-scope spot checks: A1–A6 (busy checkpoint with a
     six-digit value and a four-digit PIN, reader held), B1–B2
     (marker persists across reopen, retry converges), and the
     six-digit value cleaned with no concurrent reader.
  C: (3) no PIN in any output surface — CLI --json and plain stdout
     AND stderr on a residue DB (success), the API return dict, the
     two failure cases (probe forced to keep finding residue;
     reader held so the checkpoint is busy), plus a logging capture
     during an in-process CLI run (caplog equivalent: no log record
     may contain the PIN; erasure.py / cli.py make no logging calls
     on this path, so the capture is expected empty — the assertion
     is written as PIN absence, not emptiness).
  D: (2) docs — §30 exists with the R17/R18 record and no
     placeholder; §29:594 rewritten (residue_before, 4-digit PIN
     range, no probe_before); §28:574's return dict uses
     residue_*; both erasure.py docstrings and the migration 007
     comment carry no unqualified "re-discovers" probe claim;
     every remaining probe_before / probe_after occurrence is
     historical narrative of the R16 contract change; SECURITY.md
     holds no placeholder. (Skipped when the docs are not next to
     this copy — the installed wheel carries no docs.)
  E: schema still v7 (no new migration in this rework) +
     purge_pending table present in a fresh DB.

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT.
By default it is a fresh mkdtemp under /tmp (tmpfs, 512M); set
TURNKEEP_REPRO_TMPDIR to a directory on persistent disk to keep the
repro off /tmp entirely when pytest runs concurrently (both sharing
/tmp once filled the disk). This script removes its TMPROOT on
success (registered with atexit); on failure it keeps it and prints
the path, so the failing DBs can be inspected.
"""
import atexit
import contextlib
import hashlib
import hmac
import io
import json
import logging
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
_REPRO_TMPDIR = os.environ.get("TURNKEEP_REPRO_TMPDIR")
if _REPRO_TMPDIR:
    Path(_REPRO_TMPDIR).mkdir(parents=True, exist_ok=True)
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r18-",
                                dir=_REPRO_TMPDIR or None))
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


def _purge_pending_scopes(db):
    return sorted(r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall())


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


def _make_ghost_marker(db, path, scope):
    """Write a purge_pending marker for a scope that has NO erase
    event: hold a reader so the explicit cleanup fails (busy
    checkpoint), which persists the marker. Returns the held reader
    — the caller releases it. Only the checkpoint's busy bit is
    asserted: the log/checkpointed components vary with DB history."""
    reader = _hold_reader(path)
    out = purge_residue(db, scope)
    assert out["cleaned"] is False
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 1
    assert scope in _purge_pending_scopes(db)
    return reader


def _cli_both(argv):
    """Run the CLI in-process and capture its stdout and stderr."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = cli_main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _LogCollector(logging.Handler):
    """Collects formatted log messages for the C6 capture."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: union
print("\n== A: global purge_residue collects the union of scopes ==")
path = TMPROOT / "a-ghost.sqlite3"
db = open_db(path)
reader = _make_ghost_marker(db, path, "ghost-scope")
check("A1 ghost-scope marker written by a failed explicit cleanup: "
      "cleaned False, checkpoint busy == 1, report NOT_CLEAN",
      _purge_pending_scopes(db) == ["ghost-scope"]
      and residual_report(db, "ghost-scope")["status"] ==
      "CONTROLLED_NOT_CLEAN"
      and PIN not in _purge_pending(db, "ghost-scope")[0]["detail"])
reader.close()
out = purge_residue(db)
check("A2 a GLOBAL purge clears the ghost-scope marker and the "
      "report is clean (no reverse error, no false-positive residue)",
      out["cleaned"] is True and out["checkpoint"] is not None
      and out["checkpoint"][0] == 0
      and _purge_pending_scopes(db) == []
      and residual_report(db, "ghost-scope")["controlled"]["live_tables_clean"] is True
      and residual_report(db, "ghost-scope")["controlled"][
          "file_purge_pending"] == [])
db.close()

path = TMPROOT / "a-isolation.sqlite3"
db = open_db(path)
reader_a = _make_ghost_marker(db, path, "scope-a")
reader_b = _make_ghost_marker(db, path, "scope-b")
assert _purge_pending_scopes(db) == ["scope-a", "scope-b"]
reader_a.close()
reader_b.close()
out = purge_residue(db, "scope-a")
check("A3 an explicit purge of scope-a clears ONLY scope-a's "
      "marker; scope-b's stays and its report stays NOT_CLEAN",
      out["cleaned"] is True
      and _purge_pending_scopes(db) == ["scope-b"]
      and residual_report(db, "scope-b")["status"] ==
      "CONTROLLED_NOT_CLEAN"
      and [p["record_id"] for p in residual_report(
          db, "scope-b")["controlled"]["file_purge_pending"]] ==
      ["purge_residue:scope-b"])
out = purge_residue(db)
check("A4 a later GLOBAL purge clears scope-b too",
      out["cleaned"] is True and _purge_pending_scopes(db) == []
      and residual_report(db, "scope-b")["controlled"]["live_tables_clean"] is True)
db.close()

db, path, _key_hex = _make_off_residue("a-unclean", PIN)
reader = _make_ghost_marker(db, path, "ghost-scope")
out = purge_residue(db)
check("A5 global purge with a reader held FAILS and keeps BOTH "
      "markers (union), both reports NOT_CLEAN",
      out["cleaned"] is False and out["checkpoint"] is not None
      and out["checkpoint"][0] == 1
      and _purge_pending_scopes(db) == ["ghost-scope", "s"]
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and residual_report(db, "ghost-scope")["status"] ==
      "CONTROLLED_NOT_CLEAN")
reader.close()
out = purge_residue(db)
check("A6 after the reader releases, a GLOBAL purge succeeds and "
      "clears both markers, both reports clean",
      out["cleaned"] is True and out["checkpoint"] is not None
      and out["checkpoint"][0] == 0
      and _purge_pending_scopes(db) == []
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True
      and residual_report(db, "ghost-scope")["controlled"]["live_tables_clean"] is True)
db.close()

# ---------------------------------------------------------------- B: R17 spot checks
print("\n== B: R17 original-scope spot checks ==")
db, path, key_hex = _make_off_residue("b-six", SIX)
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
check("B1 six-digit + reader: cleaned False, checkpoint busy == 1",
      out["cleaned"] is False and out["checkpoint"] is not None
      and out["checkpoint"][0] == 1
      and out["error"] == f"PRAGMA wal_checkpoint(TRUNCATE) busy: {out['checkpoint']}",
      str(out["checkpoint"]))
check("B2 six-digit + reader: probe is blind (residue_* False) — "
      "and that does NOT compensate for busy == 1",
      out["residue_before"] is False and out["residue_after"] is False
      and "probe_before" not in out and "probe_after" not in out)
check("B3 six-digit + reader: deleted key still in the main DB file",
      key_hex.encode("ascii") in Path(path).read_bytes())
check("B4 six-digit + reader: failure persisted, report NOT_CLEAN, "
      "marker detail holds no value",
      _purge_pending_rows(db) == 1
      and SIX not in _purge_pending(db, "s")[0]["detail"]
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and "purge_residue:s" in _pending_ids(db))
db.close()

db, path, key_hex = _make_off_residue("b-pin", PIN)
reader = _hold_reader(path)
try:
    out = purge_residue(db)
finally:
    reader.close()
check("B5 four-digit + reader: cleaned False, checkpoint busy == 1, "
      "probe finds the residue (residue_* True)",
      out["cleaned"] is False and out["checkpoint"] is not None
      and out["checkpoint"][0] == 1
      and out["residue_before"] is True and out["residue_after"] is True
      and key_hex.encode("ascii") in Path(path).read_bytes()
      and PIN not in _purge_pending(db, "s")[0]["detail"],
      str(out["checkpoint"]))
db.close()

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
check("B6 after a failed purge, reopening still reports "
      "CONTROLLED_NOT_CLEAN with the persisted marker",
      _purge_pending_rows(db) == 1
      and residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is False
      and "purge_residue:s" in _pending_ids(db))
out = purge_residue(db)
check("B7 retry after the reader releases: cleaned True, marker "
      "cleared, report clean, blind scan finds nothing",
      out["cleaned"] is True and out["checkpoint"] is not None
      and out["checkpoint"][0] == 0 and out["residue_after"] is False
      and _purge_pending_rows(db) == 0
      and residual_report(db, "s")["controlled"]["live_tables_clean"] is True
      and _blind_scan_main_wal(str(path), db) == (None, None))
db.close()

db, path, key_hex = _make_off_residue("b-six-clean", SIX)
out = purge_residue(db)
check("B8 six-digit with NO concurrent reader: cleaned True, key "
      "hex gone from main file + WAL, blind scan finds nothing",
      out["cleaned"] is True and out["checkpoint"] is not None
      and out["checkpoint"][0] == 0
      and key_hex.encode("ascii") not in Path(path).read_bytes()
      and key_hex.encode("ascii") not in Path(str(path) + "-wal").read_bytes()
      and _blind_scan_main_wal(str(path), db) == (None, None))
db.close()

# ---------------------------------------------------------------- C: outputs
print("\n== C: no erased value in any output surface (stdout, stderr, logs) ==")
db, path, _key_hex = _make_off_residue("c-json", PIN)
rc, raw, err = _cli_both(["--db", str(path), "--json", "purge-residue"])
check("C1 CLI --json on a residue DB: exit 0, residue_before True, "
      "no PIN in stdout or stderr",
      rc == 0 and json.loads(raw)["residue_before"] is True
      and PIN not in raw and PIN not in err, str(rc))
db.close()

db, path, _key_hex = _make_off_residue("c-plain", PIN)
rc, raw, err = _cli_both(["--db", str(path), "purge-residue"])
check("C2 CLI plain mode on a residue DB: exit 0, no PIN in stdout "
      "or stderr",
      rc == 0 and json.loads(raw)["residue_before"] is True
      and PIN not in raw and PIN not in err, str(rc))
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
    rc, raw, err = _cli_both(["--db", str(path), "--json", "purge-residue"])
finally:
    E._probe_files = real_probe
check("C4 CLI --json on a forced failure: exit 2, no PIN in stdout "
      "or stderr",
      rc == 2 and json.loads(raw)["cleaned"] is False
      and PIN not in raw and PIN not in err, str(rc))

db, path, _key_hex = _make_off_residue("c-busy", PIN)
reader = _hold_reader(path)
try:
    rc, raw, err = _cli_both(["--db", str(path), "--json", "purge-residue"])
finally:
    reader.close()
check("C5 CLI --json with the checkpoint busy: exit 2, no PIN in "
      "stdout or stderr",
      rc == 2 and json.loads(raw)["cleaned"] is False
      and PIN not in raw and PIN not in err, str(rc))
db.close()

db, path, _key_hex = _make_off_residue("c-log", PIN)
collector = _LogCollector()
root = logging.getLogger()
old_level = root.level
root.setLevel(logging.DEBUG)
root.addHandler(collector)
try:
    rc, raw, err = _cli_both(["--db", str(path), "--json", "purge-residue"])
finally:
    root.removeHandler(collector)
    root.setLevel(old_level)
check("C6 logging capture during an in-process CLI purge: exit 0, "
      "no PIN in stdout, stderr or any captured log record",
      rc == 0 and PIN not in raw and PIN not in err
      and all(PIN not in m for m in collector.messages),
      f"{len(collector.messages)} records captured")
db.close()

# ---------------------------------------------------------------- D: docs
print("\n== D: KNOWN_LIMITATIONS §30/§29/§28 + SECURITY.md ==")
doc_root = Path(__file__).resolve().parents[1]
kl = doc_root / "docs" / "KNOWN_LIMITATIONS.md"
sec = doc_root / "SECURITY.md"
if not kl.exists():
    print("SKIP D1-D6: no KNOWN_LIMITATIONS.md next to this copy (wheel run)")
else:
    text = kl.read_text(encoding="utf-8")
    check("D1 §30 exists, records the R17 verdict + R18 rework, "
          "§29 header synced, no placeholder anywhere in the file",
          "## 30." in text and "TBD" not in text
          and "R17 not yet sent" not in text
          and "R17 sent 2026-11-13, PASS_WITH_P2" in text
          and all(n in text.split("## 30.")[1] for n in (
              "P2-R17-01", "P2-R17-02", "PASS_WITH_P2", "union",
              "OFFLINE_RC_READY", "PUBLIC_CANDIDATE NOT MET")))
    line594 = [ln for ln in text.splitlines()
               if ln.startswith("- **Marker write timing")][0]
    check("D2 §29:594 rewritten: residue_before, 4-digit PIN range, "
          "no probe_before, no re-discovers, no all-residue claim",
          "residue_before" in line594
          and "4-digit PIN range" in line594
          and "probe_before" not in line594
          and "re-discovers" not in line594
          and "must never be claimed to re-discover all residue" in line594)
    line574 = [ln for ln in text.splitlines()
               if ln.startswith("- **(4) Historical-residue cleanup:")][0]
    check("D3 §28:574 return dict uses residue_before / "
          "residue_after, no probe_* keys",
          "residue_before" in line574 and "residue_after" in line574
          and "probe_before" not in line574
          and "probe_after" not in line574)
    era = (doc_root / "src" / "turnkeep" / "erasure.py").read_text(
        encoding="utf-8")
    mig = (doc_root / "src" / "turnkeep" / "migrations" /
           "007_purge_pending.sql").read_text(encoding="utf-8")
    check("D4 both erasure.py docstrings + the migration 007 comment "
          "carry no unqualified 're-discovers' probe claim",
          "re-discovers" not in era and "re-discovers" not in mig)
    hist_markers = ("old", "contract", "gone", "→",
                    "judged success from", "R15 claim",
                    "returned the recovered PIN in")
    hist_ok = True
    for fpath in (kl, doc_root / "src" / "turnkeep" / "erasure.py"):
        for ln in fpath.read_text(encoding="utf-8").splitlines():
            if "probe_before" in ln or "probe_after" in ln:
                if not any(m in ln for m in hist_markers):
                    hist_ok = False
    check("D5 every remaining probe_before / probe_after occurrence "
          "is historical narrative of the R16 contract change",
          hist_ok)
    check("D6 're-discovers' survives only inside §30's own quoted "
          "record of the old wording",
          all("sweep" in ln for ln in text.splitlines()
              if "re-discovers" in ln))
if not sec.exists():
    print("SKIP D7: no SECURITY.md next to this copy (wheel run)")
else:
    check("D7 SECURITY.md holds no placeholder",
          "TBD" not in sec.read_text(encoding="utf-8"))

# ---------------------------------------------------------------- E: schema
print("\n== E: schema still v7 ==")
db = _db("e-schema")
check("E1 SCHEMA_VERSION == 7 and the purge_pending table exists in "
      "a fresh DB (no new migration in this rework)",
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
