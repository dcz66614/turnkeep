"""Regression repro for the Turnkeep R19 rework (P0-R18-01 fix).

The R18 verdict (fourth conversation, round 3/5): FINAL_VERDICT: FAIL,
1 new P0 (P0-R18-01) — a concurrent old-version write makes
purge_residue falsely report success (check -> settle race, no
recheck). The R19 fix closes it with a settlement recheck inside ONE
write transaction (BEGIN IMMEDIATE): generation comparison (erase
events + purge_pending rows), resurrected-key-row check, file probe,
and resurrected-key-hex file check — plus deletion of resurrected
rows in the same transaction (they are rows that by design must be
absent, so deleting them restores the invariant and is what makes a
bare purge retry converge). The erase/repair settlement points get
the same in-transaction recheck. See docs/KNOWN_LIMITATIONS.md §31.

Sections:
  A1: lock-timed old-version write, global purge (verification §A1):
      writer commits during the VACUUM wait -> this purge must report
      cleaned=False, keep the marker, report NOT_CLEAN; the
      resurrected row is deleted by the purge; a retry after the
      writer is gone converges to cleaned=True and a clean report.
  A2: pure check->settle window, global purge (hook on the 2nd
      _probe_files call, i.e. after VACUUM + checkpoint, before the
      settlement): same expectations as A1.
  A4: pure check->settle window, EXPLICIT scope purge (same hook):
      the explicit path shares the race (verification A4) and must
      be fixed by the same shared-path change.
  A3b: sparse writer (one commit every 5-30 ms, 300 ghost markers to
      widen settlement): over several attempts there must be NO
      cleaned=True with the key in the main file (a hit is a
      failure); after the writer stops, purges converge to clean.
  A3: tight-loop writer (fail-closed check, NOT a race regression):
      the purge must report cleaned=False (VACUUM cannot get the
      lock or the checkpoint is busy); it must not falsely succeed.
  A5: second interleaving variant (GPT R18 .md): a concurrent
      explicit purge writes a purge_pending marker after the global
      physical check; the global call must NOT clear that new marker
      on the stale verdict -> cleaned=False, marker kept, report
      NOT_CLEAN; retry converges.
  A6: GPT main variant (legacy OFF erase chain via
      scripts/off_residue_child.py, hooked at the 2nd _probe_files
      call): new erase events after the checkpoint must be caught
      by the generation comparison -> cleaned=False, marker kept,
      report NOT_CLEAN; retry converges and the OFF residue is gone.
  B1: erase/repair settlement race (verification item 2, reproduced
      in ~/workspace/r19-work/erase-settle-race.py): a write landing
      between _verify_purge and the settlement transaction must turn
      the repair re-run PARTIAL (marker set, report NOT_CLEAN); a
      clean repair re-run converges to COMPLETE.
  B2: first-erase normal path: a fresh record's first erase still
      returns COMPLETE with no marker (the in-transaction recheck
      must not false-positive).
  C1-C3: R18 original-scope spot checks (no concurrency): clean
      global purge, clean explicit purge, and the R17 P2-R17-01
      ghost-scope marker cleared by a global run.
  D1-D7: docs (§31 exists, §30 synced to sent + FAIL, §31 content,
      no TBD, probe_before/probe_after historical only, no
      "re-discovers", commit->return boundary stated).
  E1-E2: schema still v7 (no new migration; fresh DB user_version 7).

Usage:
  TURNKEEP_SRC=<src dir> python3 repro_r19.py   (source / RC)
  <wheel-venv>/bin/python repro_r19.py           (wheel)
For the wheel run, copy this script AND off_residue_child.py to a
directory OUTSIDE the source tree first (else the D section finds
the source docs and runs full, and A6 cannot find the child).
Temp dirs live under this file's directory (persistent disk, never
/tmp); removed on success via atexit, kept on failure for inspection.
"""
import atexit
import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC_HINT = os.environ.get("TURNKEEP_SRC")
if SRC_HINT:
    sys.path.insert(0, SRC_HINT)

import turnkeep  # noqa: F401
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue,
    _all_stored_needle_hashes,
)

SIX = "864218"
SRC = [{"id": "u:1", "type": "user_statement"}]
SCRIPT_DIR = Path(__file__).resolve().parent
VERIFY_ROOT = SCRIPT_DIR.parent
TMPROOT = Path(os.environ.get("TURNKEEP_VERIFY_TMPDIR")
               or SCRIPT_DIR / "tmp-r19")
# A previous failed run keeps its TMPROOT for inspection; a new run
# starts clean (else remember() on the stale DBs raises
# REPLACEMENT_REQUIRED for the reused keys). Concurrent runs must
# use distinct TURNKEEP_VERIFY_TMPDIR values.
if TMPROOT.exists():
    shutil.rmtree(TMPROOT)
TMPROOT.mkdir(parents=True, exist_ok=True)
CHECKS = []
SKIPS = []


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""),
          flush=True)


def skip(name, why):
    SKIPS.append(name)
    print("SKIP " + name + f"  [{why}]")


def _cleanup():
    fails = [n for n, ok in CHECKS if not ok]
    if not fails:
        shutil.rmtree(TMPROOT, ignore_errors=True)
    else:
        print(f"\n(keeping TMPROOT for inspection: {TMPROOT})")


atexit.register(_cleanup)

print(f"turnkeep.__file__ = {turnkeep.__file__}", flush=True)


def _db(name, scope="s"):
    path = TMPROOT / f"{name}.sqlite3"
    db = open_db(path)
    register_scope(db, scope)
    return db, path


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _purge_pending_rows(db):
    return db.execute("SELECT COUNT(*) FROM purge_pending").fetchone()[0]


def _purge_pending_scopes(db):
    return [r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall()]


def _marker_rows(db):
    return db.execute("SELECT COUNT(*) FROM maintenance_pending").fetchone()[0]


def _stored_hashes_for(path):
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        hashes = set()
        for (meta_json,) in conn.execute(
                "SELECT metadata_json FROM events WHERE event_type='erase'").fetchall():
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except ValueError:
                continue
            for h in meta.get("needle_hashes", []) or []:
                if isinstance(h, str):
                    hashes.add(h)
        return hashes
    finally:
        conn.close()


def _hex_candidates(blob):
    cands = set()
    for m in re.finditer(rb"[0-9a-f]{64,}", blob):
        run = m.group(0).decode()
        for off in range(len(run) - 63):
            cands.add(run[off:off + 64])
    return cands


def _six_recovers_from_file(path, hashes):
    """Blind targeted check: does any 32-byte key found in this one
    file's (MAIN) bytes HMAC-verify the six-digit value 864218?
    Independent of the product probe (4-digit PINs only)."""
    blob = Path(path).read_bytes() if Path(path).exists() else b""
    for cand in sorted(_hex_candidates(blob)):
        try:
            key = bytes.fromhex(cand)
        except ValueError:
            continue
        if hmac.new(key, SIX.encode(), hashlib.sha256).hexdigest() in hashes:
            return True, cand
    return False, None


def _hex_in_main(path, hexv):
    main = Path(path).read_bytes() if Path(path).exists() else b""
    return hexv.encode("ascii") in main


def _report_is_not_clean(db, scope):
    rep = residual_report(db, scope)
    return (rep["controlled"]["live_tables_clean"] is False
            and rep["status"] == "CONTROLLED_NOT_CLEAN"), rep


def _report_is_clean(db, scope):
    rep = residual_report(db, scope)
    return (rep["controlled"]["live_tables_clean"] is True
            and rep["status"] != "CONTROLLED_NOT_CLEAN"), rep


def _setup_converged(name):
    """Two records sharing SIX; B PARTIAL (key captured), then A
    COMPLETE + B repair-COMPLETE so B's verification key row is
    DELETED while its needle hashes stay in the append-only events.
    Returns (db, path, a_id, b_id, key_hex)."""
    db, path = _db(name)
    a = remember(db, "s", "a-key", SIX, source_refs=SRC)
    b = remember(db, "s", "b-key", SIX, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    key_hex = db.execute(
        "SELECT secret_hex FROM record_erase_keys WHERE scope_id='s' AND record_id=?",
        (b["id"],)).fetchone()[0]
    assert erase_record(db, "s", a["id"], _rev(db, a["id"]),
                        "rotation")["status"] == "COMPLETE"
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "repair")["status"] == "COMPLETE"
    assert _key_rows(db) == 0, "key row must be deleted at terminal COMPLETE"
    assert _purge_pending_rows(db) == 0
    return db, path, a["id"], b["id"], key_hex


def _setup_fresh_pair(name):
    """Two records sharing SIX, remembered but NOT erased (for A6:
    the OFF child erases them). Returns (db, path, a_id, a_rev,
    b_id, b_rev)."""
    db, path = _db(name)
    a = remember(db, "s", "a-key", SIX, source_refs=SRC)
    b = remember(db, "s", "b-key", SIX, source_refs=SRC)
    return db, path, a["id"], _rev(db, a["id"]), b["id"], _rev(db, b["id"])


def _b_write(path, scope, record_id, key_hex):
    """The concurrent old-version write: a second connection
    re-inserts the deleted verification key row (data it read before
    the erase) and commits. Raw sqlite3, no product code."""
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
            " VALUES(?,?,?,?)",
            (scope, record_id, key_hex, "2026-11-13T00:00:00Z"))
        conn.execute("COMMIT")
    finally:
        conn.close()


def _check_failed_purge(tag, db, path, out, scope="s",
                        expect_row_deleted=True):
    """Common assertions for a purge that must fail closed on the
    race: cleaned=False, the error names the settlement recheck, the
    marker is kept, the report is NOT_CLEAN. The resurrected row (if
    the race was a row re-insert) must have been deleted by the
    purge's settlement, so a bare retry can converge."""
    check(f"{tag}: purge cleaned=False",
          out["cleaned"] is False, f"out={out}")
    check(f"{tag}: error names the settlement recheck",
          isinstance(out.get("error"), str) and "recheck" in out["error"],
          f"error={out.get('error')}")
    check(f"{tag}: marker kept for scope {scope}",
          scope in _purge_pending_scopes(db),
          f"scopes={_purge_pending_scopes(db)}")
    if expect_row_deleted:
        check(f"{tag}: resurrected key row deleted by the purge",
              _key_rows(db) == 0, f"rows={_key_rows(db)}")
    ok, rep = _report_is_not_clean(db, scope)
    check(f"{tag}: residual_report NOT_CLEAN", ok, f"status={rep['status']}")


def _check_clean_retry(tag, db, path, out, key_hex, scope="s"):
    """Common assertions for the converging retry: cleaned=True,
    busy==0 checkpoint, markers cleared, report clean, the deleted
    key's bytes gone from the main file (six-digit does not recover).
    `db` is closed here (clean checkpoint) before the byte scan; a
    fresh connection is used for the report."""
    check(f"{tag}: retry cleaned=True", out["cleaned"] is True, f"out={out}")
    check(f"{tag}: retry checkpoint busy==0",
          out["checkpoint"] is not None and out["checkpoint"][0] == 0,
          f"checkpoint={out['checkpoint']}")
    check(f"{tag}: retry markers cleared",
          _purge_pending_rows(db) == 0, f"rows={_purge_pending_rows(db)}")
    check(f"{tag}: retry key rows 0", _key_rows(db) == 0,
          f"rows={_key_rows(db)}")
    db.close()
    check(f"{tag}: key hex absent from main file after retry",
          not _hex_in_main(path, key_hex))
    hashes = _stored_hashes_for(path)
    found, _ = _six_recovers_from_file(path, hashes)
    check(f"{tag}: main-file bytes do not HMAC-verify {SIX} after retry",
          not found)
    db2 = open_db(path)
    try:
        ok, rep = _report_is_clean(db2, scope)
        check(f"{tag}: retry residual_report clean", ok,
              f"status={rep['status']}")
    finally:
        db2.close()


class _ProbeHook:
    """Hook E._probe_files: on the Nth call, run `fire()` AFTER the
    real probe returns (so the check genuinely ran first), then
    return the real result. The Nth call is the post-checkpoint
    probe when N == 2 (purge_residue calls it before VACUUM and
    after the checkpoint). Used for A2/A4/A5/A6."""

    def __init__(self, n, fire):
        self.n = n
        self.fire = fire
        self.calls = 0
        self.fired = False
        self._real = None

    def __enter__(self):
        self._real = E._probe_files

        def hooked(conn, hashes):
            self.calls += 1
            result = self._real(conn, hashes)
            if self.calls == self.n and not self.fired:
                self.fired = True
                self.fire()
            return result

        E._probe_files = hooked
        return self

    def __exit__(self, *exc):
        E._probe_files = self._real
        return False


# ---------------------------------------------------------------- §A1
print("\n== §A1 lock-timed old-version write, global purge ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("a1-race")
stop = threading.Event()


def _b_lock_timed():
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        time.sleep(0.5)
        conn.execute(
            "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
            " VALUES(?,?,?,?)",
            ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
        conn.execute("COMMIT")
    finally:
        conn.close()


t = threading.Thread(target=_b_lock_timed, daemon=True)
t.start()
out_a1 = purge_residue(db)  # global; VACUUM waits on B's RESERVED lock
t.join(timeout=15)
check("A1: writer thread finished", not t.is_alive())
_check_failed_purge("A1", db, path, out_a1)
out_a1r = purge_residue(db)  # retry after the writer is gone
_check_clean_retry("A1", db, path, out_a1r, key_hex)

# ---------------------------------------------------------------- §A2
print("\n== §A2 pure check->settle window, global purge (hook-timed) ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("a2-race")
with _ProbeHook(2, lambda: _b_write(path, "s", b_id, key_hex)) as hook:
    out_a2 = purge_residue(db)  # global
check("A2: hook fired on the 2nd _probe_files call", hook.fired is True)
_check_failed_purge("A2", db, path, out_a2)
out_a2r = purge_residue(db)  # retry converges
_check_clean_retry("A2", db, path, out_a2r, key_hex)

# ---------------------------------------------------------------- §A4
print("\n== §A4 pure check->settle window, EXPLICIT scope (hook-timed) ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("a4-race")
with _ProbeHook(2, lambda: _b_write(path, "s", b_id, key_hex)) as hook4:
    out_a4 = purge_residue(db, "s")  # explicit scope
check("A4: hook fired on the 2nd _probe_files call", hook4.fired is True)
_check_failed_purge("A4", db, path, out_a4)
out_a4r = purge_residue(db, "s")  # retry converges
_check_clean_retry("A4", db, path, out_a4r, key_hex)


# ---------------------------------------------------------------- §A3b
print("\n== §A3b sparse writer, global purge ==", flush=True)
hits = 0
attempts = 0
MAX_ATTEMPTS = 8
last = None
while attempts < MAX_ATTEMPTS:
    attempts += 1
    db, path, a_id, b_id, key_hex = _setup_converged(f"a3b-race-{attempts}")
    for i in range(300):
        db.execute(
            "INSERT INTO purge_pending(scope_id, detail, created_at) VALUES(?,?,?)",
            (f"ghost-{i}", "race-width", "2026-11-13T00:00:00Z"))
    db.commit()
    stop_ev = threading.Event()
    commit_times = []

    def _b_sparse():
        conn = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
        try:
            while not stop_ev.is_set():
                time.sleep(random.uniform(0.005, 0.030))
                if stop_ev.is_set():
                    break
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT OR REPLACE INTO record_erase_keys"
                        "(scope_id, record_id, secret_hex, created_at)"
                        " VALUES(?,?,?,?)",
                        ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
                    conn.execute("COMMIT")
                    commit_times.append(time.monotonic())
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
        finally:
            conn.close()

    wt = threading.Thread(target=_b_sparse, daemon=True)
    wt.start()
    out_a3b = purge_residue(db)
    t_ret = time.monotonic()
    stop_ev.set()
    wt.join(timeout=15)
    landed = [c for c in commit_times if c < t_ret]
    # The DB stays open here (no clean close): the main-file byte
    # scan must see the file as the purge left it.
    main = Path(path).read_bytes()
    hashes = _stored_hashes_for(path)
    found, _ = _six_recovers_from_file(path, hashes)
    hit = (out_a3b["cleaned"] is True and landed
           and key_hex.encode("ascii") in main and found)
    print(f"  attempt {attempts}: cleaned={out_a3b['cleaned']} "
          f"checkpoint={out_a3b['checkpoint']} commits={len(commit_times)} "
          f"before_return={len(landed)} key_in_main="
          f"{key_hex.encode('ascii') in main} six_recovers={found}",
          flush=True)
    if hit:
        hits += 1
    last = (db, path, key_hex)
    db.close()
check("A3b: no cleaned=True with the key in the main file",
      hits == 0, f"attempts={attempts} hits={hits}")
# Convergence after the writer stops: the last DB may still hold a
# live resurrected row (last insert before the stop); the first
# retry deletes it and fails closed, the next one reports clean.
db, path, key_hex = last
db = open_db(path)
converged = None
for _ in range(3):
    converged = purge_residue(db)
    if converged["cleaned"]:
        break
check("A3b: purges converge to cleaned=True after the writer stops",
      converged is not None and converged["cleaned"] is True,
      f"out={converged}")
if converged is not None and converged["cleaned"]:
    _check_clean_retry("A3b", db, path, converged, key_hex)
else:
    db.close()

# ---------------------------------------------------------------- §A3
print("\n== §A3 tight-loop writer, fail-closed check ==", flush=True)
for attempt in (1, 2):
    db, path, a_id, b_id, key_hex = _setup_converged(f"a3-tight-{attempt}")
    for i in range(300):
        db.execute(
            "INSERT INTO purge_pending(scope_id, detail, created_at) VALUES(?,?,?)",
            (f"ghost-{i}", "race-width", "2026-11-13T00:00:00Z"))
    db.commit()
    stop_ev = threading.Event()

    def _b_loop():
        conn = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
        try:
            while not stop_ev.is_set():
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT OR REPLACE INTO record_erase_keys"
                        "(scope_id, record_id, secret_hex, created_at)"
                        " VALUES(?,?,?,?)",
                        ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    time.sleep(0.001)
        finally:
            conn.close()

    wt = threading.Thread(target=_b_loop, daemon=True)
    wt.start()
    out_a3 = purge_residue(db)  # global
    stop_ev.set()
    wt.join(timeout=15)
    print(f"  attempt {attempt}: cleaned={out_a3['cleaned']} "
          f"checkpoint={out_a3['checkpoint']} error={out_a3['error']}",
          flush=True)
    check(f"A3: attempt {attempt} cleaned=False (fail-closed, "
          "VACUUM cannot get the lock or the checkpoint is busy)",
          out_a3["cleaned"] is False, f"out={out_a3}")
    check(f"A3: attempt {attempt} reports an error",
          out_a3["error"] is not None, f"error={out_a3['error']}")
    db.close()


# ---------------------------------------------------------------- §A5
print("\n== §A5 second interleaving variant: new marker after the "
      "global physical check ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("a5-marker-race")
# The reader is created INSIDE the hook (after the global VACUUM +
# checkpoint), so it blocks only the concurrent explicit purge —
# not the global purge's own physical cleanup. A reader held before
# the global call would fail the global VACUUM busy too, and the
# settlement recheck would never be exercised.
_state5 = {"explicit_out": None}


def _fire_explicit_purge():
    reader = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM memories").fetchone()
        db2 = open_db(path)
        try:
            _state5["explicit_out"] = purge_residue(db2, "ghost-scope")
        finally:
            db2.close()
    finally:
        reader.execute("ROLLBACK")
        reader.close()


with _ProbeHook(2, _fire_explicit_purge) as hook5:
    out_a5 = purge_residue(db)  # global
check("A5: hook fired on the 2nd _probe_files call", hook5.fired is True)
check("A5: concurrent explicit purge failed busy and wrote its marker",
      _state5["explicit_out"] is not None
      and _state5["explicit_out"]["cleaned"] is False
      and _state5["explicit_out"]["checkpoint"] is not None
      and _state5["explicit_out"]["checkpoint"][0] == 1
      and "ghost-scope" in _purge_pending_scopes(db),
      f"explicit_out={_state5['explicit_out']}")
# The global call must NOT clear the new marker on the stale verdict.
_check_failed_purge("A5", db, path, out_a5, expect_row_deleted=False)
check("A5: ghost-scope marker kept (not cleared on the stale verdict)",
      "ghost-scope" in _purge_pending_scopes(db))
out_a5r = purge_residue(db)  # retry converges
_check_clean_retry("A5", db, path, out_a5r, key_hex)

# ---------------------------------------------------------------- §A6
print("\n== §A6 GPT main variant: legacy OFF erase chain after the "
      "global checkpoint ==", flush=True)
child = SCRIPT_DIR / "off_residue_child.py"
if not child.exists():
    # Fall back: search upward from the package for scripts/.
    pkg_dir = Path(turnkeep.__file__).resolve().parent
    for parent in [pkg_dir.parent, pkg_dir.parent.parent,
                   pkg_dir.parent.parent.parent]:
        cand = parent / "scripts" / "off_residue_child.py"
        if cand.exists():
            child = cand
            break
if not child.exists():
    skip("A6: OFF child (generation change)", "off_residue_child.py not found")
    skip("A6: retry converges after the OFF chain", "off_residue_child.py not found")
else:
    db, path, a_id, a_rev, b_id, b_rev = _setup_fresh_pair("a6-off-chain")
    key_out = TMPROOT / "a6-deleted-key.hex"
    src_dir = str(Path(turnkeep.__file__).resolve().parent.parent)
    _state6 = {"proc": None}

    def _fire_off_child():
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir
        proc = subprocess.run(
            [sys.executable, str(child), str(path), a_id, str(a_rev),
             b_id, str(b_rev), str(key_out)],
            capture_output=True, text=True, timeout=120, env=env)
        _state6["proc"] = proc

    with _ProbeHook(2, _fire_off_child) as hook6:
        out_a6 = purge_residue(db)  # global
    check("A6: hook fired on the 2nd _probe_files call", hook6.fired is True)
    check("A6: OFF child crashed as expected (exit 77)",
          _state6["proc"] is not None
          and _state6["proc"].returncode == 77,
          f"rc={_state6['proc'].returncode if _state6['proc'] else None}")
    off_key_hex = key_out.read_text(encoding="ascii").strip()
    check("A6: OFF child recorded the deleted key hex",
          len(off_key_hex) == 64, f"key_out={key_out}")
    # The generation changed (new erase events after the checkpoint),
    # so the settlement recheck must fail the purge even though the
    # probe is blind to the six-digit value and no key row is live.
    _check_failed_purge("A6", db, path, out_a6, expect_row_deleted=False)
    out_a6r = purge_residue(db)  # retry converges; OFF residue purged
    _check_clean_retry("A6", db, path, out_a6r, off_key_hex)


# ---------------------------------------------------------------- §B1
print("\n== §B1 erase/repair settlement race: repair re-run ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("b1-erase-race")
# Hook (scratch construction, proven 8/8 in
# ~/workspace/r19-work/erase-settle-race.py): wrap E._probe_files to
# flag the probe-only branch, and wrap E.transaction to fire the
# writer on the first transaction() call after that flag — i.e.
# strictly after _verify_purge, strictly before the settlement
# BEGIN IMMEDIATE.
_b1 = {"probe_fired": False, "writer_fired": False}
_real_probe_b1 = E._probe_files


def _hooked_probe_b1(conn, hashes):
    _b1["probe_fired"] = True
    return _real_probe_b1(conn, hashes)


def _writer_b1():
    _b_write(path, "s", b_id, key_hex)


_real_txn_b1 = E.transaction


@contextlib.contextmanager
def _hooked_txn_b1(db_conn, *, write=False):
    if _b1["probe_fired"] and not _b1["writer_fired"]:
        _b1["writer_fired"] = True
        _writer_b1()
    with _real_txn_b1(db_conn, write=write) as c:
        yield c


E._probe_files = _hooked_probe_b1
E.transaction = _hooked_txn_b1
try:
    out_b1 = erase_record(db, "s", b_id, _rev(db, b_id), "repair")
finally:
    E._probe_files = _real_probe_b1
    E.transaction = _real_txn_b1
check("B1: hook: probe-only branch fired", _b1["probe_fired"] is True)
check("B1: hook: writer fired before settlement", _b1["writer_fired"] is True)
check("B1: repair re-run returns PARTIAL (not the false COMPLETE)",
      out_b1["status"] == "PARTIAL", f"out={out_b1}")
check("B1: maintenance note names the in-transaction re-verification",
      "re-verification" in "; ".join(out_b1.get("maintenance") or []),
      f"maintenance={out_b1.get('maintenance')}")
check("B1: maintenance_pending True", out_b1.get("maintenance_pending") is True)
check("B1: maintenance_pending marker set",
      _marker_rows(db) == 1, f"rows={_marker_rows(db)}")
ok, rep = _report_is_not_clean(db, "s")
check("B1: residual_report NOT_CLEAN", ok, f"status={rep['status']}")
# Convergence: a clean repair re-run deletes the resurrected row and
# completes.
out_b1r = erase_record(db, "s", b_id, _rev(db, b_id), "repair")
check("B1: clean repair re-run returns COMPLETE",
      out_b1r["status"] == "COMPLETE", f"out={out_b1r}")
check("B1: clean repair re-run clears the marker",
      _marker_rows(db) == 0, f"rows={_marker_rows(db)}")
ok, rep = _report_is_clean(db, "s")
check("B1: clean repair re-run leaves the report clean", ok,
      f"status={rep['status']}")
check("B1: key rows 0 after convergence", _key_rows(db) == 0)
db.close()

# ---------------------------------------------------------------- §B2
print("\n== §B2 first-erase normal path (no false positive) ==", flush=True)
db, path = _db("b2-first-erase")
c = remember(db, "s", "c-key", SIX, source_refs=SRC)
out_b2 = erase_record(db, "s", c["id"], _rev(db, c["id"]), "rotation")
check("B2: first erase returns COMPLETE",
      out_b2["status"] == "COMPLETE", f"out={out_b2}")
check("B2: first erase sets no maintenance_pending marker",
      out_b2.get("maintenance_pending") is False
      and _marker_rows(db) == 0)
check("B2: first erase leaves no key row", _key_rows(db) == 0)
db.close()

# ---------------------------------------------------------------- §C
print("\n== §C R18 original-scope spot checks (no concurrency) ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("c1-clean-global")
out_c1 = purge_residue(db)  # global
_check_clean_retry("C1", db, path, out_c1, key_hex)
db, path, a_id, b_id, key_hex = _setup_converged("c2-clean-explicit")
out_c2 = purge_residue(db, "s")  # explicit scope
_check_clean_retry("C2", db, path, out_c2, key_hex)
# C3: R17 P2-R17-01 — ghost-scope marker cleared by a global run.
db, path = _db("c3-ghost")
reader = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
reader.execute("BEGIN")
reader.execute("SELECT COUNT(*) FROM memories").fetchone()
out_g = purge_residue(db, "ghost-scope")
check("C3: explicit ghost-scope fails under reader (busy==1)",
      out_g["cleaned"] is False and out_g["checkpoint"] is not None
      and out_g["checkpoint"][0] == 1, f"out={out_g}")
check("C3: ghost-scope marker written",
      "ghost-scope" in _purge_pending_scopes(db))
reader.execute("ROLLBACK")
reader.close()
out_all = purge_residue(db)
check("C3: global purge succeeds after reader released",
      out_all["cleaned"] is True, f"out={out_all}")
check("C3: ghost-scope marker cleared by global run",
      _purge_pending_rows(db) == 0)
rep = residual_report(db, "ghost-scope")
check("C3: residual_report(ghost-scope) clean",
      rep["controlled"]["live_tables_clean"] is True
      and rep["status"] != "CONTROLLED_NOT_CLEAN")
db.close()

# ------------------------------------------------------- §D docs + §E schema
print("\n== §D docs (§31) + §E schema ==", flush=True)
pkg_dir = Path(turnkeep.__file__).resolve().parent
root = None
for parent in [pkg_dir.parent, pkg_dir.parent.parent,
               pkg_dir.parent.parent.parent]:
    if (parent / "docs" / "KNOWN_LIMITATIONS.md").exists():
        root = parent
        break
if root is None:
    for n in ("D1 §31 exists", "D2 §30 synced to sent + FAIL",
              "D3 §31 content", "D4 no TBD",
              "D5 probe_before/probe_after historical only",
              "D6 no 're-discovers'", "D7 commit->return boundary"):
        skip(n, "docs not adjacent (wheel?)")
    skip("E1 no new migration", "docs not adjacent (wheel?)")
else:
    kl = (root / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    lines = kl.splitlines()
    check("D1: §31 exists",
          any("§31" in l for l in lines))
    check("D2: §30 synced (no 'R18 not yet sent'; R18 FINAL_VERDICT: FAIL)",
          "R18 not yet sent" not in kl and "FINAL_VERDICT: FAIL" in kl)
    sec31 = kl.split("§31", 1)[1] if "§31" in kl else ""
    need = ["generation", "data_version", "recheck", "resurrected",
            "first-erase", "after return"]
    missing = [w for w in need if w.lower() not in sec31.lower()]
    check("D3: §31 covers generation + data_version rejection + "
          "recheck + resurrected-row deletion + first-erase immunity "
          "+ post-return boundary",
          not missing, f"missing={missing}")
    sec_path = root / "docs" / "SECURITY.md"
    sec_text = sec_path.read_text(encoding="utf-8") if sec_path.exists() else ""
    check("D4: no TBD placeholder in KNOWN_LIMITATIONS.md or SECURITY.md",
          "TBD" not in kl and "TBD" not in sec_text)
    pb_lines = [l for l in lines if "probe_before" in l or "probe_after" in l]
    check("D5: probe_before/probe_after occur only as historical narrative",
          all(("R16" in l or "contract" in l or "renamed" in l or "old" in l.lower())
              for l in pb_lines) and len(pb_lines) >= 1,
          f"lines={len(pb_lines)}")
    erasure_src = (root / "src" / "turnkeep" / "erasure.py").read_text(encoding="utf-8")
    mig7 = (root / "src" / "turnkeep" / "migrations" / "007_purge_pending.sql").read_text(encoding="utf-8")
    check("D6: erasure.py + migration 007 carry no 're-discovers'",
          "re-discovers" not in erasure_src and "re-discovers" not in mig7)
    check("D7: §31 states the commit->return boundary (verdict correct "
          "as of the settlement commit; later writes are a NEW generation)",
          "NEW generation" in sec31 or "after that commit" in sec31)
    mig_dir = root / "src" / "turnkeep" / "migrations"
    migs = sorted(p.name for p in mig_dir.glob("*.sql"))
    check("E1: no new migration (001-007 only)",
          migs == [f"00{i}_" + n for i, n in
                   [(1, "initial.sql"), (2, "record_provenance.sql"),
                    (3, "scope_secrets.sql"), (4, "erase_flags.sql"),
                    (5, "record_erase_keys.sql"),
                    (6, "maintenance_pending.sql"),
                    (7, "purge_pending.sql")]],
          f"migrations={migs}")
    db_e, path_e = _db("e-schema")
    ver = db_e.execute("PRAGMA user_version").fetchone()[0]
    check("E2: fresh DB user_version == 7 (schema v7)",
          ver == 7, f"user_version={ver}")
    db_e.close()

# ---------------------------------------------------------------- summary
fails = [n for n, ok in CHECKS if not ok]
print(f"\n== summary ==\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed"
      + (f" ({len(SKIPS)} skipped: {', '.join(SKIPS)})" if SKIPS else ""))
if fails:
    print("FAILED:", *fails, sep="\n  ")
    sys.exit(1)
