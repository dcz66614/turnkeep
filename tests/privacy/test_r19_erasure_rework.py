"""R19 rework tests for P0-R18-01 (docs/KNOWN_LIMITATIONS.md §31).

P0-R18-01: a concurrent old-version write made purge_residue
falsely report success. Root cause: the file check (hashes,
probe_before, VACUUM, checkpoint, probe_after) ran first and the
scope union + marker settlement ran after it, with no global lock
or generation recheck between check and settlement — a write
landing in that window was never re-checked. The fix: the
settlement — final recheck + marker clear — runs inside ONE write
transaction (BEGIN IMMEDIATE), and the recheck (see
_purge_settlement_recheck) compares the generation captured before
VACUUM (erase-event ids + purge_pending rows) with the generation
inside the transaction, checks for resurrected verification-key
rows for terminal-COMPLETE records, re-probes the files, and
checks the resurrected keys' hex is absent from the main file /
WAL. Any failure keeps/refreshes the markers and reports
cleaned=False. A resurrected row found by the recheck is deleted
in the same settlement transaction (it is a row that by design
must be absent, so deleting it restores the invariant and is what
makes a bare purge retry converge). The recheck does NOT use
PRAGMA data_version: the purge's own commits also advance it, so
the expected delta cannot distinguish our commits from concurrent
ones without fragile exact accounting (see §31).

The same in-transaction recheck closes the erase/repair
settlement points (item 2, reproduced first in
~/workspace/r19-work/erase-settle-race.py, 8/8): a write landing
between _verify_purge and the settlement transaction turned a
repair re-run COMPLETE with the key live again. The first-erase
path cannot be hit the same way (its key is created and deleted
inside one transaction), but the same recheck protects it too.

The A3 tight-loop writer is a fail-closed check, NOT a race
regression (verification §A3): it holds a RESERVED lock almost
continuously, so VACUUM cannot get the lock and the purge fails
closed. A3b's sparse writer is the correct regression shape.
"""
import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue,
)

SIX = "864218"
SRC = [{"id": "u:1", "type": "user_statement"}]
REPO_ROOT = Path(__file__).resolve().parents[2]
OFF_RESIDUE_CHILD = REPO_ROOT / "scripts" / "off_residue_child.py"


def _db(tmp_path, name="t", scope="s"):
    path = tmp_path / f"{name}.sqlite3"
    db = open_db(path)
    register_scope(db, scope)
    return db, path


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _purge_pending_scopes(db):
    return sorted(r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall())


def _marker_rows(db):
    return db.execute("SELECT COUNT(*) FROM maintenance_pending").fetchone()[0]


def _setup_converged(tmp_path, name):
    """Two records sharing SIX; B PARTIAL (key captured), then A
    COMPLETE + B repair-COMPLETE so B's verification key row is
    DELETED while its needle hashes stay in the append-only events.
    Returns (db, path, a_id, b_id, key_hex)."""
    db, path = _db(tmp_path, name)
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
    assert _key_rows(db) == 0
    assert _purge_pending_scopes(db) == []
    return db, path, a["id"], b["id"], key_hex


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


def _hex_in_main(path, hexv):
    main = Path(path).read_bytes() if Path(path).exists() else b""
    return hexv.encode("ascii") in main


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


def _six_recovers_from_main(path):
    """Blind targeted check on the MAIN file only: does any 32-byte
    key found there HMAC-verify SIX? Independent of the product
    probe (4-digit PINs only)."""
    hashes = _stored_hashes_for(path)
    blob = Path(path).read_bytes() if Path(path).exists() else b""
    for m in re.finditer(rb"[0-9a-f]{64,}", blob):
        run = m.group(0).decode()
        for off in range(len(run) - 63):
            cand = run[off:off + 64]
            try:
                key = bytes.fromhex(cand)
            except ValueError:
                continue
            if hmac.new(key, SIX.encode(), hashlib.sha256).hexdigest() in hashes:
                return True
    return False


class _ProbeHook:
    """Hook E._probe_files: on the Nth call, run `fire()` AFTER the
    real probe returns, then return the real result. N == 2 is the
    post-checkpoint probe (purge_residue calls it before VACUUM and
    after the checkpoint)."""

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


def _assert_failed_purge(db, out, scope="s", expect_row_deleted=True):
    """A purge that must fail closed on the race: cleaned=False, the
    error names the settlement recheck, the marker is kept, the
    report is NOT_CLEAN, and (when the race was a row re-insert)
    the resurrected row was deleted by the settlement."""
    assert out["cleaned"] is False, out
    assert isinstance(out.get("error"), str) and "recheck" in out["error"], out
    assert scope in _purge_pending_scopes(db)
    if expect_row_deleted:
        assert _key_rows(db) == 0
    rep = residual_report(db, scope)
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"


def _assert_clean_retry(db, path, out, key_hex, scope="s"):
    """The converging retry: cleaned=True, busy==0, markers cleared,
    report clean, the deleted key's bytes gone from the main file.
    `db` is closed here (clean checkpoint) before the byte scan."""
    assert out["cleaned"] is True, out
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 0
    assert _purge_pending_scopes(db) == []
    assert _key_rows(db) == 0
    db.close()
    assert not _hex_in_main(path, key_hex)
    assert not _six_recovers_from_main(path)
    db2 = open_db(path)
    try:
        rep = residual_report(db2, scope)
        assert rep["controlled"]["live_tables_clean"] is True
        assert rep["status"] != "CONTROLLED_NOT_CLEAN"
    finally:
        db2.close()


def _hold_reader(db_path):
    db2 = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    db2.execute("BEGIN")
    db2.execute("SELECT COUNT(*) FROM memories").fetchone()
    return db2


def _spawn_off_residue(db_path, a_id, a_rev, b_id, b_rev, key_out=None):
    argv = [sys.executable, str(OFF_RESIDUE_CHILD), str(db_path), a_id,
            str(a_rev), b_id, str(b_rev)]
    if key_out is not None:
        argv.append(str(key_out))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env)


# --- (1) purge_residue check->settle race ---

def test_a1_lock_timed_global_purge_fails_closed_and_converges(tmp_path):
    """(A1) The writer holds BEGIN IMMEDIATE, then commits the
    re-inserted key while purge_residue waits inside VACUUM. This
    purge must report cleaned=False (the settlement recheck catches
    the resurrected row), keep the marker, and report NOT_CLEAN;
    the row is deleted by the settlement, so a retry after the
    writer is gone converges to cleaned=True and a clean report."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "a1")

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
    out = purge_residue(db)  # global; VACUUM waits on B's RESERVED lock
    t.join(timeout=15)
    assert not t.is_alive()
    _assert_failed_purge(db, out)
    _assert_clean_retry(db, path, purge_residue(db), key_hex)


def test_a2_hook_window_global_purge(tmp_path):
    """(A2) The write lands strictly after probe_after and strictly
    before the settlement (hook on the 2nd _probe_files call). The
    purge must fail closed; the retry converges."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "a2")
    with _ProbeHook(2, lambda: _b_write(path, "s", b_id, key_hex)) as hook:
        out = purge_residue(db)
    assert hook.fired
    _assert_failed_purge(db, out)
    _assert_clean_retry(db, path, purge_residue(db), key_hex)


def test_a4_hook_window_explicit_scope_purge(tmp_path):
    """(A4) The explicit-scope path shares the race (verification
    A4): the same hook construction against purge_residue(db, "s")
    must fail closed, and the retry must converge. The fix is on
    the shared path, not the global branch alone."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "a4")
    with _ProbeHook(2, lambda: _b_write(path, "s", b_id, key_hex)) as hook:
        out = purge_residue(db, "s")
    assert hook.fired
    _assert_failed_purge(db, out)
    _assert_clean_retry(db, path, purge_residue(db, "s"), key_hex)


def test_a3b_sparse_writer_no_false_clean(tmp_path):
    """(A3b) A sparse writer (one commit every 5-30 ms) with 300
    ghost markers widening the settlement: over several attempts
    there must be NO cleaned=True with the key in the main file
    (a hit is a failure). After the writer stops, purges converge
    to clean."""
    hits = 0
    last = None
    for attempt in range(4):
        db, path, a_id, b_id, key_hex = _setup_converged(
            tmp_path, f"a3b-{attempt}")
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
        out = purge_residue(db)
        t_ret = time.monotonic()
        stop_ev.set()
        wt.join(timeout=15)
        landed = [c for c in commit_times if c < t_ret]
        main = Path(path).read_bytes()
        if (out["cleaned"] is True and landed
                and key_hex.encode("ascii") in main
                and _six_recovers_from_main(path)):
            hits += 1
        last = (db, path, key_hex)
        db.close()
    assert hits == 0, "sparse writer hit the race (false clean)"
    db, path, key_hex = last
    db = open_db(path)
    converged = None
    for _ in range(3):
        converged = purge_residue(db)
        if converged["cleaned"]:
            break
    assert converged is not None and converged["cleaned"] is True, converged
    _assert_clean_retry(db, path, converged, key_hex)


def test_a3_tight_loop_writer_fail_closed(tmp_path):
    """(A3) Fail-closed check, NOT a race regression: a tight-loop
    writer holds a RESERVED lock almost continuously, so VACUUM
    cannot get the lock and the purge must report cleaned=False
    (never a false success). One attempt: the VACUUM busy_timeout
    makes each attempt ~10s."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "a3")
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
    out = purge_residue(db)
    stop_ev.set()
    wt.join(timeout=15)
    assert out["cleaned"] is False, out
    assert out["error"] is not None, out
    db.close()


def test_a5_marker_variant_global_purge(tmp_path):
    """(A5) Second interleaving variant (GPT R18 .md): a concurrent
    explicit purge writes a purge_pending marker after the global
    physical check; the global call must NOT clear that new marker
    on the stale verdict. Global cleaned=False, the ghost marker
    is kept, the report is NOT_CLEAN; the retry converges."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "a5")
    # The reader is created INSIDE the hook (after the global VACUUM
    # + checkpoint), so it blocks only the concurrent explicit purge
    # — not the global purge's own physical cleanup. A reader held
    # before the global call would fail the global VACUUM busy too,
    # and the settlement recheck would never be exercised.
    state = {"explicit_out": None}

    def _fire_explicit_purge():
        reader = _hold_reader(path)
        try:
            db2 = open_db(path)
            try:
                state["explicit_out"] = purge_residue(db2, "ghost-scope")
            finally:
                db2.close()
        finally:
            reader.close()

    with _ProbeHook(2, _fire_explicit_purge) as hook:
        out = purge_residue(db)
    assert hook.fired
    assert state["explicit_out"] is not None
    assert state["explicit_out"]["cleaned"] is False
    assert state["explicit_out"]["checkpoint"] is not None
    assert state["explicit_out"]["checkpoint"][0] == 1  # busy only
    _assert_failed_purge(db, out, expect_row_deleted=False)
    assert "ghost-scope" in _purge_pending_scopes(db)
    _assert_clean_retry(db, path, purge_residue(db), key_hex)


def test_a6_off_chain_generation_change(tmp_path):
    """(A6) GPT main variant: a legacy OFF erase chain (child
    subprocess) lands after the global checkpoint. The probe is
    blind to the six-digit value and no key row is live — only the
    generation comparison (new erase events after the checkpoint)
    catches it. Global cleaned=False, marker kept, report
    NOT_CLEAN; the retry converges and the OFF residue is gone."""
    path = tmp_path / "a6.sqlite3"
    db = open_db(path)
    register_scope(db, "s")
    a = remember(db, "s", "a-key", SIX, source_refs=SRC)
    b = remember(db, "s", "b-key", SIX, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    key_out = tmp_path / "a6.keyhex"
    state = {"proc": None}

    def _fire_off_child():
        state["proc"] = _spawn_off_residue(
            path, a["id"], a_rev, b["id"], b_rev, key_out)

    with _ProbeHook(2, _fire_off_child) as hook:
        out = purge_residue(db)
    assert hook.fired
    assert state["proc"] is not None and state["proc"].returncode == 77
    off_key_hex = key_out.read_text(encoding="ascii").strip()
    assert len(off_key_hex) == 64
    _assert_failed_purge(db, out, expect_row_deleted=False)
    _assert_clean_retry(db, path, purge_residue(db), off_key_hex)


# --- (2) erase/repair settlement race ---

def test_b1_repair_settlement_race(tmp_path):
    """(B1) A write landing between _verify_purge and the settlement
    transaction must turn the repair re-run PARTIAL (marker set,
    report NOT_CLEAN), not the false COMPLETE. A clean repair
    re-run converges to COMPLETE. Hook construction from
    ~/workspace/r19-work/erase-settle-race.py (8/8 on unpatched)."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "b1")
    state = {"probe_fired": False, "writer_fired": False}
    real_probe = E._probe_files

    def _hooked_probe(conn, hashes):
        state["probe_fired"] = True
        return real_probe(conn, hashes)

    real_txn = E.transaction

    @contextlib.contextmanager
    def _hooked_txn(db_conn, *, write=False):
        if state["probe_fired"] and not state["writer_fired"]:
            state["writer_fired"] = True
            _b_write(path, "s", b_id, key_hex)
        with real_txn(db_conn, write=write) as c:
            yield c

    E._probe_files = _hooked_probe
    E.transaction = _hooked_txn
    try:
        out = erase_record(db, "s", b_id, _rev(db, b_id), "repair")
    finally:
        E._probe_files = real_probe
        E.transaction = real_txn
    assert state["probe_fired"] and state["writer_fired"]
    assert out["status"] == "PARTIAL", out
    assert "re-verification" in "; ".join(out.get("maintenance") or []), out
    assert out.get("maintenance_pending") is True
    assert _marker_rows(db) == 1
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    out2 = erase_record(db, "s", b_id, _rev(db, b_id), "repair")
    assert out2["status"] == "COMPLETE", out2
    assert _marker_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["status"] != "CONTROLLED_NOT_CLEAN"
    assert _key_rows(db) == 0
    db.close()


def test_b2_first_erase_no_false_positive(tmp_path):
    """(B2) The in-transaction recheck must not false-positive on
    the normal first-erase path: a fresh record's first erase still
    returns COMPLETE with no marker and no key row."""
    db, path = _db(tmp_path, "b2")
    c = remember(db, "s", "c-key", SIX, source_refs=SRC)
    out = erase_record(db, "s", c["id"], _rev(db, c["id"]), "rotation")
    assert out["status"] == "COMPLETE", out
    assert out.get("maintenance_pending") is False
    assert _marker_rows(db) == 0
    assert _key_rows(db) == 0
    db.close()


# --- (C) R18 original scope: clean purges still succeed ---

def test_c_clean_global_and_explicit_purge(tmp_path):
    """(C) No concurrency: a clean global purge and a clean
    explicit-scope purge still report cleaned=True with a busy==0
    checkpoint, and the six-digit key's bytes are gone from the
    main file."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "c1")
    _assert_clean_retry(db, path, purge_residue(db), key_hex)
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "c2")
    _assert_clean_retry(db, path, purge_residue(db, "s"), key_hex)


# --- (D) docs: §31 ---

def test_known_limitations_section_31():
    """(D) §31 exists and is synced: §30 no longer says R18 was
    not sent (it records FINAL_VERDICT: FAIL); §31 covers the
    generation protocol, the data_version rejection, the
    settlement recheck, the resurrected-row deletion, the
    first-erase immunity, and the post-return boundary; no TBD
    placeholder anywhere in KNOWN_LIMITATIONS.md or SECURITY.md;
    probe_before/probe_after occur only as historical narrative;
    erasure.py + migration 007 carry no "re-discovers"."""
    docs = REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md"
    text = docs.read_text(encoding="utf-8")
    assert "## 31." in text or "§31" in text
    assert "R18 not yet sent" not in text
    assert "FINAL_VERDICT: FAIL" in text
    assert "TBD" not in text
    sec31 = text.split("§31", 1)[1]
    for word in ("generation", "data_version", "recheck", "resurrected",
                 "first-erase", "after return"):
        assert word.lower() in sec31.lower(), word
    assert "NEW generation" in sec31 or "after that commit" in sec31
    sec = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "TBD" not in sec
    for fpath in (docs, REPO_ROOT / "src" / "turnkeep" / "erasure.py"):
        for ln in fpath.read_text(encoding="utf-8").splitlines():
            if "probe_before" in ln or "probe_after" in ln:
                assert any(m in ln for m in
                           ("old", "contract", "gone", "→",
                            "judged success from", "R15 claim",
                            "returned the recovered PIN in")), ln
    era = (REPO_ROOT / "src" / "turnkeep" / "erasure.py").read_text(
        encoding="utf-8")
    assert "re-discovers" not in era
    mig = (REPO_ROOT / "src" / "turnkeep" / "migrations" /
           "007_purge_pending.sql").read_text(encoding="utf-8")
    assert "re-discovers" not in mig
