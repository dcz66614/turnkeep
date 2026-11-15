"""Regression repro for the Turnkeep R20 rework (P0-R19-01 fix).

The R19 verdict (fourth conversation, round 4/5): FINAL_VERDICT: FAIL,
1 new P0 (P0-R19-01) — the purge_residue failure branch (cleaned=False)
collected the scope union with _purge_settlement_scopes and then wrote
one _set_purge_pending marker per scope, with no write transaction and
no generation re-check between collection and write, skipping
_purge_settlement_recheck (which only the success branch calls). Two
scenarios, both verified TRUE byte-exact on the unpacked R19 RC:
  (A) a scope whose erase events land after the scope collection gets
      no marker (empty snapshot + OFF chain; V1: non-empty snapshot
      ["s"] + a concurrent OFF chain adding scope "t") — the later
      residual_report() reads false-clean while the new scope's key
      hex is still in the main file and HMAC-verifies 864218;
  (B) a concurrent write lock makes _set_purge_pending() fail and the
      exception is caught and turned into an error string only — the
      writer commits before the purge returns, the key row is live
      again, purge_pending is 0, and the report reads clean.
The R20 fix closes both with _settle_failure_markers (erasure.py):
the failure branch settles inside ONE write transaction
(BEGIN IMMEDIATE), re-collecting the scope union + generation INSIDE
it, and retries lock contention bounded (10 s budget, 100 ms per
attempt, 0.05 s sleep); a generation change is noted in the marker
detail ("; generation changed after physical check: N new erase
event(s)"); attempts > 1 are noted in error ("purge marker write
retried (N attempts)"); a spent budget is reported explicitly
("marker could not be persisted after N attempts"). See §32.

Hook construction (why GPT's R19 script does NOT transfer verbatim):
GPT's script hooks _purge_settlement_scopes to run the OFF child —
on the fixed code that hook never fires in the failure branch (the
collection moved inside the BEGIN IMMEDIATE settlement transaction,
and the OFF child cannot take the write lock while the purge holds
RESERVED, so it exits without writing key_out). repro_r20 instead
hooks _probe_files (2nd call: release the reader, set probe_fired)
+ E.transaction (only once probe_fired is set and the child has not
run yet: capture the pre-child scope snapshot, run the OFF child to
exit 77 first, then hand the call to the real transaction()). On
unfixed code the failure branch never calls transaction(), so the
child never runs and the P0 reproduces (no marker). B1 hooks
E.transaction by count instead (1st call passes through and fails,
2nd call commits the writer first, then passes through): on unfixed
code the writer never commits and the marker write fails with no
marker; on fixed code the 2nd attempt persists the marker and the
error notes "retried (2 attempts)". See §32 (hook-construction
bullet).

Sections:
  A1: GPT scenario A, empty snapshot + OFF child (hook-timed):
      the OFF chain lands after the physical check; the failure
      settlement must re-collect scope "s" inside its transaction
      and mark it (detail notes the generation change) -> cleaned=
      False, checkpoint busy==1, key hex in the main file and
      HMAC-verifying 864218, report NOT_CLEAN; a bare retry
      converges in one attempt.
  A2: verification V1, non-empty snapshot ["s"] + new scope "t":
      scope "s" already has a PARTIAL erase (so it IS in the
      snapshot); the OFF child runs in "t" (scope-parameterised
      off_residue_child.py, 8th argv). Both "s" and "t" must get
      markers and both reports must be NOT_CLEAN; the retry
      converges for "t" in one attempt ("s" may stay NOT_CLEAN:
      its PARTIAL key row is live by design — only "t" is
      asserted on the retry).
  B1: GPT scenario B, marker write fails under lock, then retries:
      the writer holds a RESERVED lock; the 1st settlement
      attempt fails (BEGIN IMMEDIATE cannot start), the 2nd
      attempt commits the writer first and persists the marker ->
      cleaned=False, error names "database is locked" and notes
      "retried (2 attempts)", live key row 1, HMAC verifies
      864218, report NOT_CLEAN. Retry 1 then deletes the
      resurrected row (still cleaned=False); retry 2 converges.
  B2: retry budget exhausted (documented boundary): the writer
      holds the lock past the (monkeypatched 0.3 s) budget ->
      cleaned=False, error says the marker "could not be
      persisted", purge_pending 0, report still clean (the error
      says so; the caller must retry after the writer releases).
      After the writer rolls back, purges converge.
  B3: had_txn — the marker joins the caller's transaction (C-5):
      purge_residue called inside a caller transaction (VACUUM
      cannot run inside one) -> cleaned=False, the marker is
      written into the caller's transaction (visible on that
      connection, invisible to others until the caller commits),
      and the purge must not commit the caller's uncommitted work.
      After the caller commits, the marker persists and the
      report is NOT_CLEAN; a bare retry converges.
  C1-C3: R18 original-scope spot checks (no concurrency): clean
      global purge, clean explicit purge, and the R17 P2-R17-01
      ghost-scope marker cleared by a global run.
  D1-D7: docs (§32 exists, §31 synced to sent + FAIL, §32 content,
      no TBD, probe_before/probe_after historical only, no
      "re-discovers", commit->return + failure-branch boundary).
  E1-E2: schema still v7 (no new migration; fresh DB user_version 7).

Usage:
  TURNKEEP_SRC=<src dir> python3 repro_r20.py   (source / RC)
  <wheel-venv>/bin/python repro_r20.py           (wheel)
For the wheel run, copy this script AND off_residue_child.py to a
directory OUTSIDE the source tree first (else the D section finds
the source docs and runs full, and A1/A2 cannot find the child).
Temp dirs live under this file's directory (persistent disk, never
/tmp); removed on success via atexit, kept on failure for inspection.
"""
import atexit
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
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
               or SCRIPT_DIR / "tmp-r20")
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


def _rev_sc(db, scope, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id=? AND id=?",
                      (scope, rid)).fetchone()[0]


def _rev(db, rid):
    return _rev_sc(db, "s", rid)


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _purge_pending_rows(db):
    return db.execute("SELECT COUNT(*) FROM purge_pending").fetchone()[0]


def _purge_pending_scopes(db):
    return [r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall()]


def _purge_pending_detail(db, scope):
    row = db.execute("SELECT detail FROM purge_pending WHERE scope_id=?",
                     (scope,)).fetchone()
    return row[0] if row is not None else None


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


def _key_hex_verifies_scope_hash(db, key_hex, scope):
    """Precise check: does THIS key verify a stored needle hash of
    THIS scope? (Scope-consistent; the product probe additionally
    excludes live keys and only covers 4-digit values.)"""
    digest = hmac.new(bytes.fromhex(key_hex), SIX.encode(),
                      hashlib.sha256).hexdigest()
    return digest in _all_stored_needle_hashes(db, scope)


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
    """Two records sharing SIX, remembered but NOT erased (for A1:
    the OFF child erases them). Returns (db, path, a_id, a_rev,
    b_id, b_rev)."""
    db, path = _db(name)
    a = remember(db, "s", "a-key", SIX, source_refs=SRC)
    b = remember(db, "s", "b-key", SIX, source_refs=SRC)
    return db, path, a["id"], _rev(db, a["id"]), b["id"], _rev(db, b["id"])


def _setup_v1(name):
    """V1 (verification variant, for A2): scope "s" has one PARTIAL
    erase (so "s" IS in the failure-path snapshot); scope "t" has
    two records and no erase events at all. Returns (db, path,
    s_a_id, s_a_rev, s_b_id, t_a_id, t_a_rev, t_b_id, t_b_rev)."""
    db, path = _db(name)
    register_scope(db, "t")
    sa = remember(db, "s", "s-a-key", SIX, source_refs=SRC)
    sb = remember(db, "s", "s-b-key", SIX, source_refs=SRC)
    assert erase_record(db, "s", sb["id"], _rev(db, sb["id"]),
                        "rotation")["status"] == "PARTIAL"
    ta = remember(db, "t", "t-a-key", SIX, source_refs=SRC)
    tb = remember(db, "t", "t-b-key", SIX, source_refs=SRC)
    return (db, path, sa["id"], _rev(db, sa["id"]), sb["id"],
            ta["id"], _rev_sc(db, "t", ta["id"]),
            tb["id"], _rev_sc(db, "t", tb["id"]))


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
    after the checkpoint). Used for A1/A2 (fire releases the
    reader and sets probe_fired)."""

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


class _TxnHook:
    """Hook E.transaction: call `fire(db_conn)` on EVERY
    transaction() call (the fire closure decides whether to act),
    then hand the call to the real transaction(). Counts the calls.
    Used for A1/A2 (fire runs the OFF child once, before the real
    BEGIN IMMEDIATE) and B1 (fire commits the writer on the 2nd
    call). The fire runs BEFORE the real transaction starts, so the
    child / writer commit never contends with the purge's RESERVED
    lock — on unfixed code the failure branch never calls
    transaction(), so the fire never runs and the P0 reproduces."""

    def __init__(self, fire):
        self.fire = fire
        self.calls = 0
        self._real = None

    def __enter__(self):
        self._real = E.transaction
        real = self._real
        hook = self

        @contextlib.contextmanager
        def hooked(db_conn, *, write=False):
            hook.calls += 1
            hook.fire(db_conn)
            with real(db_conn, write=write) as c:
                yield c

        E.transaction = hooked
        return self

    def __exit__(self, *exc):
        E.transaction = self._real
        return False


def _find_off_child():
    """Locate scripts/off_residue_child.py: next to this script
    first, else by walking up from the imported package."""
    child = SCRIPT_DIR / "off_residue_child.py"
    if child.exists():
        return child
    pkg_dir = Path(turnkeep.__file__).resolve().parent
    for parent in [pkg_dir.parent, pkg_dir.parent.parent,
                   pkg_dir.parent.parent.parent]:
        cand = parent / "scripts" / "off_residue_child.py"
        if cand.exists():
            return cand
    return None


def _run_off_child(child, path, a_id, a_rev, b_id, b_rev, key_out,
                   scope="s"):
    """Run the OFF child once (subprocess, waits for exit); returns
    the CompletedProcess. NOTE: the child resolves its own source
    tree from its file location (parents[1]/src) and the parent
    sets PYTHONPATH to the imported package's source dir."""
    src_dir = str(Path(turnkeep.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir
    argv = [sys.executable, str(child), str(path), a_id, str(a_rev),
            b_id, str(b_rev), str(key_out)]
    if scope != "s":
        argv.append(scope)
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env, encoding="utf-8")


def _skip_many(names, why):
    for n in names:
        skip(n, why)


# ---------------------------------------------------------------- §A1
print("\n== §A1 GPT scenario A: empty snapshot + OFF child ==", flush=True)
child = _find_off_child()
if child is None:
    _skip_many([
        "A1: probe fired on the 2nd _probe_files call",
        "A1: transaction hook fired (child run attempted)",
        "A1: scope snapshot before the child is []",
        "A1: OFF child crashed as expected (exit 77)",
        "A1: OFF child recorded the deleted key hex",
        "A1: purge cleaned=False",
        "A1: checkpoint busy==1",
        "A1: key hex in main file",
        "A1: main-file bytes HMAC-verify 864218",
        "A1: purge_pending contains s",
        "A1: marker detail notes the generation change",
        "A1: residual_report(s) NOT_CLEAN",
        "A1: retry cleaned=True",
        "A1: retry checkpoint busy==0",
        "A1: retry markers cleared",
        "A1: retry key rows 0",
        "A1: key hex absent from main file after retry",
        "A1: main-file bytes do not HMAC-verify 864218 after retry",
        "A1: retry residual_report clean",
    ], "off_residue_child.py not found")
else:
    db, path, a_id, a_rev, b_id, b_rev = _setup_fresh_pair("a1-off-chain")
    reader = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM memories").fetchone()
    key_out = TMPROOT / "a1-deleted-key.hex"
    st1 = {"probe_fired": False, "child_ran": False, "snapshot": None,
           "snapshot_error": None, "proc": None, "key_hex": None}

    def _fire_probe_a1():
        # The checkpoint has already returned busy. Release the
        # reader so the OFF child (run later, at the settlement
        # transaction) can take the write lock.
        reader.execute("ROLLBACK")
        reader.close()
        st1["probe_fired"] = True

    def _fire_txn_a1(db_conn):
        # Only once probe_fired is set and the child has not run:
        # capture the pre-child scope snapshot, run the OFF child
        # to exit 77, then hand the call to the real transaction().
        # On unfixed code this never runs (the failure branch never
        # calls transaction()), so the child never runs and the P0
        # reproduces (no marker).
        if not st1["probe_fired"] or st1["child_ran"]:
            return
        st1["child_ran"] = True
        try:
            st1["snapshot"] = E._purge_settlement_scopes(db_conn, None)
        except Exception as e:  # noqa: BLE001 - record, do not mask the purge
            st1["snapshot_error"] = str(e)
        st1["proc"] = _run_off_child(child, path, a_id, a_rev, b_id,
                                     b_rev, key_out)
        if key_out.exists():
            st1["key_hex"] = key_out.read_text(encoding="ascii").strip()

    with _ProbeHook(2, _fire_probe_a1) as hook1, \
            _TxnHook(_fire_txn_a1) as txn1:
        out_a1 = purge_residue(db)  # global
    check("A1: probe fired on the 2nd _probe_files call",
          st1["probe_fired"] is True)
    check("A1: transaction hook fired (child run attempted)",
          txn1.calls >= 1 and st1["child_ran"] is True,
          f"txn_calls={txn1.calls} child_ran={st1['child_ran']}")
    check("A1: scope snapshot before the child is []",
          st1["snapshot"] == [],
          f"snapshot={st1['snapshot']} err={st1['snapshot_error']}")
    check("A1: OFF child crashed as expected (exit 77)",
          st1["proc"] is not None and st1["proc"].returncode == 77,
          f"rc={st1['proc'].returncode if st1['proc'] else None} "
          f"stdout={st1['proc'].stdout.strip() if st1['proc'] else None}")
    off_hex_a1 = st1["key_hex"]
    check("A1: OFF child recorded the deleted key hex",
          isinstance(off_hex_a1, str) and len(off_hex_a1) == 64,
          f"key_hex={off_hex_a1!r}")
    check("A1: purge cleaned=False", out_a1["cleaned"] is False,
          f"out={out_a1}")
    check("A1: checkpoint busy==1",
          out_a1["checkpoint"] is not None and out_a1["checkpoint"][0] == 1,
          f"checkpoint={out_a1['checkpoint']}")
    if off_hex_a1 is None:
        check("A1: key hex in main file", False,
              "key_out missing — OFF child never ran (unfixed code)")
        check("A1: main-file bytes HMAC-verify 864218", False,
              "key_out missing — OFF child never ran (unfixed code)")
    else:
        check("A1: key hex in main file", _hex_in_main(path, off_hex_a1))
        hashes_a1 = _stored_hashes_for(path)
        found_a1, _ = _six_recovers_from_file(path, hashes_a1)
        check("A1: main-file bytes HMAC-verify 864218", found_a1)
    check("A1: purge_pending contains s",
          "s" in _purge_pending_scopes(db),
          f"scopes={_purge_pending_scopes(db)}")
    detail_a1 = _purge_pending_detail(db, "s")
    check("A1: marker detail notes the generation change",
          detail_a1 is not None and re.search(
              r"generation changed after physical check: "
              r"3 new erase event\(s\)", detail_a1) is not None,
          f"detail={detail_a1!r}")
    ok, rep = _report_is_not_clean(db, "s")
    check("A1: residual_report(s) NOT_CLEAN", ok, f"status={rep['status']}")
    if off_hex_a1 is None:
        _skip_many([
            "A1: retry cleaned=True",
            "A1: retry checkpoint busy==0",
            "A1: retry markers cleared",
            "A1: retry key rows 0",
            "A1: key hex absent from main file after retry",
            "A1: main-file bytes do not HMAC-verify 864218 after retry",
            "A1: retry residual_report clean",
        ], "OFF child never ran (unfixed code); nothing to converge")
    else:
        out_a1r = purge_residue(db)  # 1 bare retry converges
        _check_clean_retry("A1", db, path, out_a1r, off_hex_a1)

# ---------------------------------------------------------------- §A2
print("\n== §A2 V1: non-empty snapshot [\"s\"] + new scope \"t\" ==", flush=True)
if child is None:
    _skip_many([
        "A2: probe fired on the 2nd _probe_files call",
        "A2: transaction hook fired (child run attempted)",
        "A2: scope snapshot before the child is ['s']",
        "A2: OFF child crashed as expected (exit 77)",
        "A2: OFF child recorded the deleted key hex (t)",
        "A2: purge cleaned=False",
        "A2: checkpoint busy==1",
        "A2: t key hex in main file",
        "A2: t key hex HMAC-verifies 864218 against t's scope hashes",
        "A2: purge_pending contains s and t",
        "A2: marker detail for t notes the generation change",
        "A2: residual_report(s) NOT_CLEAN",
        "A2: residual_report(t) NOT_CLEAN",
        "A2: retry cleaned=True",
        "A2: retry checkpoint busy==0",
        "A2: retry markers cleared",
        "A2: retry t key hex absent from main file",
        "A2: retry residual_report(t) clean",
    ], "off_residue_child.py not found")
else:
    (db, path, s_a_id, s_a_rev, s_b_id,
     t_a_id, t_a_rev, t_b_id, t_b_rev) = _setup_v1("a2-new-scope-t")
    reader = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM memories").fetchone()
    key_out = TMPROOT / "a2-t-deleted-key.hex"
    st2 = {"probe_fired": False, "child_ran": False, "snapshot": None,
           "snapshot_error": None, "proc": None, "key_hex": None}

    def _fire_probe_a2():
        reader.execute("ROLLBACK")
        reader.close()
        st2["probe_fired"] = True

    def _fire_txn_a2(db_conn):
        if not st2["probe_fired"] or st2["child_ran"]:
            return
        st2["child_ran"] = True
        try:
            st2["snapshot"] = E._purge_settlement_scopes(db_conn, None)
        except Exception as e:  # noqa: BLE001 - record, do not mask the purge
            st2["snapshot_error"] = str(e)
        st2["proc"] = _run_off_child(child, path, t_a_id, t_a_rev,
                                     t_b_id, t_b_rev, key_out, scope="t")
        if key_out.exists():
            st2["key_hex"] = key_out.read_text(encoding="ascii").strip()

    with _ProbeHook(2, _fire_probe_a2) as hook2, \
            _TxnHook(_fire_txn_a2) as txn2:
        out_a2 = purge_residue(db)  # global
    check("A2: probe fired on the 2nd _probe_files call",
          st2["probe_fired"] is True)
    check("A2: transaction hook fired (child run attempted)",
          txn2.calls >= 1 and st2["child_ran"] is True,
          f"txn_calls={txn2.calls} child_ran={st2['child_ran']}")
    check("A2: scope snapshot before the child is ['s']",
          st2["snapshot"] == ["s"],
          f"snapshot={st2['snapshot']} err={st2['snapshot_error']}")
    check("A2: OFF child crashed as expected (exit 77)",
          st2["proc"] is not None and st2["proc"].returncode == 77,
          f"rc={st2['proc'].returncode if st2['proc'] else None} "
          f"stdout={st2['proc'].stdout.strip() if st2['proc'] else None}")
    off_hex_a2 = st2["key_hex"]
    check("A2: OFF child recorded the deleted key hex (t)",
          isinstance(off_hex_a2, str) and len(off_hex_a2) == 64,
          f"key_hex={off_hex_a2!r}")
    check("A2: purge cleaned=False", out_a2["cleaned"] is False,
          f"out={out_a2}")
    check("A2: checkpoint busy==1",
          out_a2["checkpoint"] is not None and out_a2["checkpoint"][0] == 1,
          f"checkpoint={out_a2['checkpoint']}")
    if off_hex_a2 is None:
        check("A2: t key hex in main file", False,
              "key_out missing — OFF child never ran (unfixed code)")
        check("A2: t key hex HMAC-verifies 864218 against t's scope hashes",
              False, "key_out missing — OFF child never ran (unfixed code)")
    else:
        check("A2: t key hex in main file", _hex_in_main(path, off_hex_a2))
        check("A2: t key hex HMAC-verifies 864218 against t's scope hashes",
              _key_hex_verifies_scope_hash(db, off_hex_a2, "t"))
    pending_a2 = sorted(_purge_pending_scopes(db))
    check("A2: purge_pending contains s and t",
          "s" in pending_a2 and "t" in pending_a2,
          f"scopes={pending_a2}")
    detail_a2 = _purge_pending_detail(db, "t")
    check("A2: marker detail for t notes the generation change",
          detail_a2 is not None and re.search(
              r"generation changed after physical check: "
              r"3 new erase event\(s\)", detail_a2) is not None,
          f"detail={detail_a2!r}")
    ok, rep = _report_is_not_clean(db, "s")
    check("A2: residual_report(s) NOT_CLEAN", ok, f"status={rep['status']}")
    ok, rep = _report_is_not_clean(db, "t")
    check("A2: residual_report(t) NOT_CLEAN", ok, f"status={rep['status']}")
    if off_hex_a2 is None:
        _skip_many([
            "A2: retry cleaned=True",
            "A2: retry checkpoint busy==0",
            "A2: retry markers cleared",
            "A2: retry t key hex absent from main file",
            "A2: retry residual_report(t) clean",
        ], "OFF child never ran (unfixed code); nothing to converge")
    else:
        # One bare retry converges for "t". "s" is NOT asserted
        # here: its PARTIAL key row is live by design, so its
        # report may stay NOT_CLEAN — only "t" + the markers are.
        out_a2r = purge_residue(db)
        check("A2: retry cleaned=True", out_a2r["cleaned"] is True,
              f"out={out_a2r}")
        check("A2: retry checkpoint busy==0",
              out_a2r["checkpoint"] is not None
              and out_a2r["checkpoint"][0] == 0,
              f"checkpoint={out_a2r['checkpoint']}")
        check("A2: retry markers cleared",
              _purge_pending_rows(db) == 0,
              f"rows={_purge_pending_rows(db)}")
        db.close()
        check("A2: retry t key hex absent from main file",
              not _hex_in_main(path, off_hex_a2))
        db2 = open_db(path)
        try:
            ok, rep = _report_is_clean(db2, "t")
            check("A2: retry residual_report(t) clean", ok,
                  f"status={rep['status']}")
        finally:
            db2.close()


# ---------------------------------------------------------------- §B1
print("\n== §B1 marker write fails under lock, then retries ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("b1-marker-lock")
db.execute("PRAGMA busy_timeout=100")
writer = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
writer.execute("BEGIN IMMEDIATE")
writer.execute(
    "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
    " VALUES(?,?,?,?)", ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
stb1 = {"writer_committed": False, "writer_commit": None}

# The writer commits ONLY when the settlement transaction is
# attempted a 2nd time: the 1st attempt passes through and fails
# (BEGIN IMMEDIATE cannot start under the writer's RESERVED lock),
# exercising the retry path; the 2nd attempt commits the writer
# first, then passes through, so the marker persists. On unfixed
# code transaction() is never called, the writer never commits, and
# the marker write fails with no marker (P0 scenario B).
def _fire_txn_b1(db_conn):
    if txn_b1.calls == 2 and not stb1["writer_committed"]:
        stb1["writer_committed"] = True
        writer.execute("COMMIT")
        stb1["writer_commit"] = time.monotonic()


with _TxnHook(_fire_txn_b1) as txn_b1:
    t0 = time.monotonic()
    out_b1 = purge_residue(db)
    t_ret = time.monotonic()
check("B1: settlement transaction attempted twice (retry happened)",
      txn_b1.calls == 2, f"txn_calls={txn_b1.calls}")
check("B1: purge cleaned=False", out_b1["cleaned"] is False,
      f"out={out_b1}")
check("B1: error names database is locked",
      isinstance(out_b1.get("error"), str)
      and "database is locked" in out_b1["error"],
      f"error={out_b1.get('error')}")
check("B1: error notes the retry (retried (2 attempts))",
      isinstance(out_b1.get("error"), str)
      and "retried (2 attempts)" in out_b1["error"],
      f"error={out_b1.get('error')}")
check("B1: writer committed before the purge returned",
      stb1["writer_commit"] is not None
      and stb1["writer_commit"] < t_ret,
      f"writer_commit={stb1['writer_commit']} t_return={t_ret}")
live_b1 = db.execute(
    "SELECT secret_hex FROM record_erase_keys WHERE scope_id='s' AND record_id=?",
    (b_id,)).fetchone()
check("B1: live key row 1", live_b1 is not None,
      f"row={live_b1}")
check("B1: live key row HMAC-verifies 864218",
      live_b1 is not None
      and _key_hex_verifies_scope_hash(db, live_b1[0], "s"))
check("B1: purge_pending contains s (marker persisted by the retry)",
      "s" in _purge_pending_scopes(db),
      f"scopes={_purge_pending_scopes(db)}")
ok, rep = _report_is_not_clean(db, "s")
check("B1: residual_report(s) NOT_CLEAN", ok, f"status={rep['status']}")
# The writer is committed by the hook on fixed code; on unfixed code
# it still holds the lock — roll it back before the bare retries so
# they are not blocked by this test's own writer.
try:
    writer.execute("ROLLBACK")
except sqlite3.Error:
    pass
writer.close()
# Convergence: retry 1 runs the SUCCESS branch — its settlement
# recheck finds the resurrected key row and deletes it, still
# reporting cleaned=False; retry 2 converges to cleaned=True.
out_b1r1 = purge_residue(db)
check("B1: retry 1 cleaned=False (recheck deletes the resurrected row)",
      out_b1r1["cleaned"] is False, f"out={out_b1r1}")
out_b1r2 = purge_residue(db)
check("B1: retry 2 cleaned=True", out_b1r2["cleaned"] is True,
      f"out={out_b1r2}")
if out_b1r2["cleaned"] is True:
    _check_clean_retry("B1", db, path, out_b1r2, key_hex)
else:
    check("B1: retry 2 converges (clean-retry checks)", False,
          f"out={out_b1r2}")

# ---------------------------------------------------------------- §B2
print("\n== §B2 retry budget exhausted (documented boundary) ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("b2-budget")
writer = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
writer.execute("BEGIN IMMEDIATE")
writer.execute(
    "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
    " VALUES(?,?,?,?)", ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
# Shrink the retry budget so the test does not wait 10 s; the writer
# holds the lock past it. Restore afterwards (the constant does not
# exist on unfixed code — creating it there is harmless).
_had_budget = hasattr(E, "_FAILURE_MARKER_RETRY_BUDGET_S")
_old_budget = getattr(E, "_FAILURE_MARKER_RETRY_BUDGET_S", None)
E._FAILURE_MARKER_RETRY_BUDGET_S = 0.3
try:
    out_b2 = purge_residue(db)
finally:
    if _had_budget:
        E._FAILURE_MARKER_RETRY_BUDGET_S = _old_budget
    else:
        delattr(E, "_FAILURE_MARKER_RETRY_BUDGET_S")
check("B2: purge cleaned=False", out_b2["cleaned"] is False,
      f"out={out_b2}")
check("B2: error says the marker could not be persisted",
      isinstance(out_b2.get("error"), str)
      and "could not be persisted" in out_b2["error"],
      f"error={out_b2.get('error')}")
check("B2: purge_pending is 0 (marker genuinely not persisted)",
      _purge_pending_rows(db) == 0, f"rows={_purge_pending_rows(db)}")
ok, rep = _report_is_clean(db, "s")
check("B2: residual_report(s) still clean (boundary: the marker "
      "could not persist while the lock is held; the error says so "
      "and the caller must retry after the writer releases)",
      ok, f"status={rep['status']}")
try:
    writer.execute("ROLLBACK")
except sqlite3.Error:
    pass
writer.close()
converged_b2 = None
for _ in range(3):
    converged_b2 = purge_residue(db)
    if converged_b2["cleaned"]:
        break
check("B2: purges converge to cleaned=True after the writer releases",
      converged_b2 is not None and converged_b2["cleaned"] is True,
      f"out={converged_b2}")
if converged_b2 is not None and converged_b2["cleaned"]:
    _check_clean_retry("B2", db, path, converged_b2, key_hex)
else:
    check("B2: convergence (clean-retry checks)", False,
          f"out={converged_b2}")

# ---------------------------------------------------------------- §B3
print("\n== §B3 had_txn: marker joins the caller's transaction ==", flush=True)
db, path, a_id, b_id, key_hex = _setup_converged("b3-had-txn")
db.execute("BEGIN")
db.execute(
    "INSERT INTO purge_pending(scope_id, detail, created_at) VALUES(?,?,?)",
    ("caller-scope", "caller work", "2026-11-13T00:00:00Z"))
out_b3 = purge_residue(db)  # VACUUM cannot run inside a caller txn
check("B3: purge cleaned=False (VACUUM cannot run inside a caller "
      "transaction)", out_b3["cleaned"] is False, f"out={out_b3}")
check("B3: marker for s visible on the caller connection",
      "s" in _purge_pending_scopes(db),
      f"scopes={_purge_pending_scopes(db)}")
db2 = open_db(path)
try:
    scopes2 = [r[0] for r in db2.execute(
        "SELECT scope_id FROM purge_pending").fetchall()]
    check("B3: marker for s NOT visible on a second connection before "
          "the caller commits (purge did not commit the caller's work)",
          "s" not in scopes2, f"scopes={scopes2}")
    check("B3: caller-owned uncommitted row NOT visible on a second "
          "connection before commit",
          "caller-scope" not in scopes2, f"scopes={scopes2}")
finally:
    db2.close()
db.execute("COMMIT")
db2 = open_db(path)
try:
    scopes2 = [r[0] for r in db2.execute(
        "SELECT scope_id FROM purge_pending").fetchall()]
    check("B3: markers visible on a second connection after the "
          "caller commits",
          "s" in scopes2 and "caller-scope" in scopes2,
          f"scopes={scopes2}")
    ok, rep = _report_is_not_clean(db2, "s")
    check("B3: residual_report(s) NOT_CLEAN on a fresh connection "
          "after commit", ok, f"status={rep['status']}")
finally:
    db2.close()
out_b3r = purge_residue(db)  # bare retry converges (global: clears both)
_check_clean_retry("B3", db, path, out_b3r, key_hex)

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
print("\n== §D docs (§32) + §E schema ==", flush=True)
pkg_dir = Path(turnkeep.__file__).resolve().parent
root = None
for parent in [pkg_dir.parent, pkg_dir.parent.parent,
               pkg_dir.parent.parent.parent]:
    if (parent / "docs" / "KNOWN_LIMITATIONS.md").exists():
        root = parent
        break
if root is None:
    for n in ("D1 §32 exists", "D2 §31 synced to sent + FAIL",
              "D3 §32 content", "D4 no TBD",
              "D5 probe_before/probe_after historical only",
              "D6 no 're-discovers'", "D7 commit->return boundary"):
        skip(n, "docs not adjacent (wheel?)")
    skip("E1 no new migration", "docs not adjacent (wheel?)")
else:
    kl = (root / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    lines = kl.splitlines()
    check("D1: §32 exists",
          any(l.startswith("## 32.") for l in lines))
    h31 = [l for l in lines if l.startswith("## 31.")]
    check("D2: §31 synced (no 'R19 not yet sent'; R19 FINAL_VERDICT: "
          "FAIL; heading says R19 sent + FAIL, see §32)",
          "R19 not yet sent" not in kl and "FINAL_VERDICT: FAIL" in kl
          and len(h31) == 1 and "R19 sent" in h31[0]
          and "FAIL" in h31[0] and "see §32" in h31[0],
          f"heading={h31}")
    sec32 = kl.split("## 32.", 1)[1] if "## 32." in kl else ""
    # Keywords pinned to §32's ACTUAL text (data_version /
    # resurrected / first-erase / "after return" live in §31, not
    # §32, so they are not asserted here). No acceptance numbers
    # are asserted: §32's "R20 acceptance" bullet is filled in
    # after the verification runs.
    need = ["generation", "recheck", "failure branch",
            "BEGIN IMMEDIATE", "could not be persisted", "retried",
            "NEW generation", "after that commit"]
    missing = [w for w in need if w.lower() not in sec32.lower()]
    check("D3: §32 covers generation + failure-branch settlement "
          "(BEGIN IMMEDIATE) + bounded retry (could not be "
          "persisted / retried) + NEW-generation boundary",
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
    check("D7: §32 states the commit->return boundary (verdict correct "
          "as of the failure settlement commit; a later write is a NEW "
          "generation) and the failure-branch boundary",
          ("NEW generation" in sec32 or "after that commit" in sec32)
          and "failure branch" in sec32.lower()
          and "boundary" in sec32.lower())
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
