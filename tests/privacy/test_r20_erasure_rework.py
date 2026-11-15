"""R20 rework tests for P0-R19-01 (docs/KNOWN_LIMITATIONS.md §32).

P0-R19-01: the purge_residue FAILURE branch (cleaned=False) dropped
the NOT_CLEAN marker for a concurrently added scope. Root cause: the
old failure branch (erasure.py:3993-4003) collected the scope union
and then wrote one _set_purge_pending marker per scope, with no
write transaction and no generation re-check between collection and
write, skipping _purge_settlement_recheck (which only the success
branch calls). Two scenarios, both reproduced deterministically on
the R19 RC (verification report
~/workspace/turnkeep-gpt-R19-verification-2026-11-13.md): (A) a
scope whose erase events land after the scope collection gets no
marker (GPT's script, empty snapshot; verification V1, non-empty
snapshot ["s"] + a concurrent OFF erase chain adding scope "t");
(B) a concurrent write lock makes _set_purge_pending() fail and the
exception is caught and turned into an error string only, with no
persisted marker. Both left cleaned=False + the key hex in the main
file + HMAC-verifiable 864218 + purge_pending=0 + a false-clean
residual_report.

The fix (_settle_failure_markers, erasure.py): the failure branch
settles inside ONE write transaction (BEGIN IMMEDIATE); the scope
union + generation are re-collected INSIDE that transaction (never
the stale pre-cleanup set), and the markers are written in the same
transaction. A generation change is not a reason to refuse on the
failure path (the cleanup already failed) — it means MORE scopes to
mark, and the marker detail notes it ("; generation changed after
physical check: N new erase event(s)", counts only, never ids or
keys). Lock contention is retried, bounded (budget 10 s, module
constant _FAILURE_MARKER_RETRY_BUDGET_S; 100 ms per attempt,
_FAILURE_MARKER_ATTEMPT_TIMEOUT_MS; 0.05 s between attempts,
_FAILURE_MARKER_RETRY_SLEEP_S): attempts > 1 note "purge marker
write retried (N attempts)" in "error"; a spent budget reports
"marker could not be persisted after N attempts" with cleaned=False
and no marker, never as if it had been written. With a caller
transaction open (had_txn) the marker joins the caller's
transaction instead (disclosed C-5 window: a write failure there
cannot be retried without touching the caller's transaction).

Hook construction (why the GPT script's hooks do not transfer
verbatim — see §32 "Hook construction"): the GPT R19 script hooks
_purge_settlement_scopes to run the OFF child. On the fixed code
that hook never fires in the failure branch — the collection moved
inside the BEGIN IMMEDIATE settlement transaction, and the OFF
child cannot take the write lock while the purge holds RESERVED
(it exits without writing key_out: KeyError 'key_hex'). These
tests instead hook _probe_files (2nd call: release the reader, set
probe_fired) + E.transaction (only once probe_fired is set and the
child has not run yet: run the OFF child to exit 77 first, then
hand the call to the real transaction()). On unfixed code the
failure branch never calls transaction(), so the child never runs
and the P0 reproduces (no marker); on fixed code the child commits
before BEGIN IMMEDIATE and the settlement re-collection sees its
erase events. B1 hooks E.transaction by count instead (1st call
passes through and fails, 2nd call commits the writer first, then
passes through): on unfixed code the writer never commits and the
marker write fails with no marker; on fixed code the 2nd attempt
persists the marker and the error notes "retried (2 attempts)".
The scope "snapshot" (["s"] in A2, [] in A1) is asserted on the
pre-cleanup erase-event scopes — the fixed code never computes a
separate stale scope list (collection happens only inside the
settlement transaction), so there is no _purge_settlement_scopes
hook in these tests at all.

These tests assert the FIXED behaviour: on the unfixed R19 code
they FAIL (that failure is the P0 reproduction).
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
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
# The in-tree script takes an optional 8th positional scope argument
# (R20 rework, P0-R19-01 A2), so A2 runs it directly — no external
# copy is needed.
OFF_RESIDUE_CHILD_SCOPE = OFF_RESIDUE_CHILD


def _db(tmp_path, name="t", scope="s"):
    path = tmp_path / f"{name}.sqlite3"
    db = open_db(path)
    register_scope(db, scope)
    return db, path


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _rev_scope(db, scope, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id=? AND id=?",
                      (scope, rid)).fetchone()[0]


def _key_rows(db):
    return db.execute("SELECT COUNT(*) FROM record_erase_keys").fetchone()[0]


def _purge_pending_scopes(db):
    return sorted(r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall())


def _marker_rows(db):
    return db.execute("SELECT COUNT(*) FROM maintenance_pending").fetchone()[0]


def _marker_detail(db, scope):
    row = db.execute(
        "SELECT detail FROM purge_pending WHERE scope_id=?",
        (scope,)).fetchone()
    return row[0] if row is not None else None


def _erase_event_scopes(db):
    """Scopes with erase events right now — the pre-cleanup
    generation's scope set (the GPT/V1 "snapshot": [] in A1, ["s"]
    in A2). Asserted before the purge; the fixed code never computes
    a separate stale scope list, so this is the snapshot."""
    return sorted({r[0] for r in db.execute(
        "SELECT DISTINCT scope_id FROM events "
        "WHERE event_type='erase'").fetchall()})


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


def _key_verifies_scope_hash(db, key_hex, value, scope):
    """Precise check: does THIS key verify a stored needle hash of
    THIS scope? (Scope-consistent; the product probe additionally
    excludes live keys and only covers 4-digit values.)"""
    digest = hmac.new(bytes.fromhex(key_hex), value.encode(),
                      hashlib.sha256).hexdigest()
    return digest in E._all_stored_needle_hashes(db, scope)


def _live_key_verifies(db, scope, record_id, value):
    """The live record_erase_keys row for (scope, record_id) verifies
    `value` against that scope's stored needle hashes."""
    row = db.execute(
        "SELECT secret_hex FROM record_erase_keys "
        "WHERE scope_id=? AND record_id=?",
        (scope, record_id)).fetchone()
    if row is None:
        return False
    return _key_verifies_scope_hash(db, row[0], value, scope)


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
                          timeout=120, env=env, encoding="utf-8")


def _spawn_off_residue_scope(db_path, a_id, a_rev, b_id, b_rev, key_out, scope):
    """The scope-parameterised OFF child: the in-tree script takes the
    scope as the 8th positional argument (R20 rework), after the
    optional [key_out] sidecar."""
    argv = [sys.executable, str(OFF_RESIDUE_CHILD_SCOPE), str(db_path),
            a_id, str(a_rev), b_id, str(b_rev), str(key_out), scope]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env, encoding="utf-8")


@contextlib.contextmanager
def _txn_fires_once(state, fire):
    """Wrap E.transaction: the first time it is called after
    state["probe_fired"] is set, run fire() BEFORE delegating to the
    real transaction(). Restores E.transaction on exit.

    The child (A1/A2) must run BEFORE BEGIN IMMEDIATE: the fixed
    failure settlement collects scopes inside its transaction, so a
    child fired later would run while the purge holds RESERVED and
    could not take the write lock. On unfixed code the failure
    branch never calls transaction(), so fire() never runs and the
    P0 reproduces (no marker)."""
    real_txn = E.transaction

    @contextlib.contextmanager
    def _hooked(db_conn, *, write=False):
        if state.get("probe_fired") and not state.get("fired"):
            state["fired"] = True
            fire()
        with real_txn(db_conn, write=write) as c:
            yield c

    E.transaction = _hooked
    try:
        yield state
    finally:
        E.transaction = real_txn


@contextlib.contextmanager
def _txn_commit_writer_on_second(state, writer):
    """Wrap E.transaction by count for the B1 lock-retry shape: the
    1st call just delegates (its BEGIN IMMEDIATE fails on the
    writer's RESERVED lock, exercising the retry path); the 2nd
    call commits the writer FIRST, then delegates, so the 2nd
    attempt can take the lock and persist the marker. On unfixed
    code the failure branch never calls transaction(), so the
    writer never commits and the marker write fails with no
    marker. Restores E.transaction on exit."""
    real_txn = E.transaction

    @contextlib.contextmanager
    def _hooked(db_conn, *, write=False):
        state["txn_calls"] = state.get("txn_calls", 0) + 1
        if state["txn_calls"] == 2:
            writer.execute("COMMIT")
            state["writer_committed_at"] = time.monotonic()
        with real_txn(db_conn, write=write) as c:
            yield c

    E.transaction = _hooked
    try:
        yield state
    finally:
        E.transaction = real_txn


# --- (A) failure branch: a scope added after the physical check ---

def test_a1_gpt_scenario_a_empty_snapshot_new_scope_gets_marker(tmp_path):
    """(A1) GPT scenario A, R20 hook shape: scope s has two records
    and NO erase event yet (empty pre-cleanup snapshot). A reader
    makes the checkpoint busy; the 2nd _probe_files call releases
    it and arms the transaction hook; the OFF child then runs
    BEFORE the failure settlement's BEGIN IMMEDIATE (exit 77) and
    lands a legacy OFF erase chain in s. The settlement must
    re-collect inside its transaction, see the 3 new erase events,
    and persist a marker for s (detail noting the generation
    change) — residual_report("s") stays CONTROLLED_NOT_CLEAN and
    the key hex stays HMAC-recoverable from the main file. A bare
    retry then converges. On unfixed code the child never runs (the
    failure branch never calls transaction()) and s gets no
    marker — the P0 reproduces."""
    path = tmp_path / "a1.sqlite3"
    db = open_db(path)
    register_scope(db, "s")
    a = remember(db, "s", "a-key", SIX, source_refs=SRC)
    b = remember(db, "s", "b-key", SIX, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    assert _erase_event_scopes(db) == []  # empty snapshot (GPT A)
    reader = _hold_reader(path)
    key_out = tmp_path / "a1.key.hex"
    state = {"probe_fired": False, "fired": False, "proc": None}

    def _fire_child():
        state["proc"] = _spawn_off_residue(
            path, a["id"], a_rev, b["id"], b_rev, key_out)

    def _release_reader():
        reader.execute("ROLLBACK")
        reader.close()
        state["probe_fired"] = True

    try:
        with _ProbeHook(2, _release_reader), _txn_fires_once(state, _fire_child):
            out = purge_residue(db)
    finally:
        reader.close()
    assert state["fired"] and state["proc"] is not None, state
    assert state["proc"].returncode == 77, state["proc"]
    assert out["cleaned"] is False, out
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 1, out
    assert isinstance(out.get("error"), str) and "busy" in out["error"], out
    key_hex = key_out.read_text(encoding="ascii").strip()
    assert len(key_hex) == 64
    assert _purge_pending_scopes(db) == ["s"]
    assert _marker_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    detail = _marker_detail(db, "s")
    assert detail is not None and "generation changed" in detail, detail
    assert key_hex not in detail
    m = re.search(r"(\d+) new erase event\(s\)", detail)
    assert m is not None and int(m.group(1)) == 3, detail
    assert _hex_in_main(path, key_hex)
    assert _six_recovers_from_main(path)
    _assert_clean_retry(db, path, purge_residue(db), key_hex)


def test_a2_v1_nonempty_snapshot_new_scope_t_gets_marker(tmp_path):
    """(A2) Verification variant V1, R20 hook shape: scope s already
    has a PARTIAL erase (so it IS in the pre-cleanup snapshot
    ["s"]), scope t has two records and no erase event. A reader
    forces the busy-checkpoint failure branch; the OFF child runs
    in t BEFORE the settlement's BEGIN IMMEDIATE (exit 77). The
    settlement must re-collect inside its transaction and persist
    markers for BOTH s and t — residual_report("t") (and ("s"))
    stays CONTROLLED_NOT_CLEAN and t's key hex stays in the main
    file, HMAC-verifying against t's stored hashes. A bare retry
    then converges for t (s may stay NOT_CLEAN on its legitimate
    PARTIAL residue, so only t and the markers are asserted on the
    retry). On unfixed code the child never runs and t gets no
    marker — the P0 reproduces."""
    path = tmp_path / "a2.sqlite3"
    db = open_db(path)
    register_scope(db, "s")
    register_scope(db, "t")
    sa = remember(db, "s", "s-a-key", SIX, source_refs=SRC)
    sb = remember(db, "s", "s-b-key", SIX, source_refs=SRC)
    assert erase_record(db, "s", sb["id"], _rev(db, sb["id"]),
                        "rotation")["status"] == "PARTIAL"
    ta = remember(db, "t", "t-a-key", SIX, source_refs=SRC)
    tb = remember(db, "t", "t-b-key", SIX, source_refs=SRC)
    ta_rev = _rev_scope(db, "t", ta["id"])
    tb_rev = _rev_scope(db, "t", tb["id"])
    assert _erase_event_scopes(db) == ["s"]  # non-empty snapshot (V1)
    reader = _hold_reader(path)
    key_out = tmp_path / "a2-t.key.hex"
    state = {"probe_fired": False, "fired": False, "proc": None}

    def _fire_child():
        state["proc"] = _spawn_off_residue_scope(
            path, ta["id"], ta_rev, tb["id"], tb_rev, key_out, "t")

    def _release_reader():
        reader.execute("ROLLBACK")
        reader.close()
        state["probe_fired"] = True

    try:
        with _ProbeHook(2, _release_reader), _txn_fires_once(state, _fire_child):
            out = purge_residue(db)
    finally:
        reader.close()
    assert state["fired"] and state["proc"] is not None, state
    assert state["proc"].returncode == 77, state["proc"]
    assert out["cleaned"] is False, out
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 1, out
    t_key_hex = key_out.read_text(encoding="ascii").strip()
    assert len(t_key_hex) == 64
    assert _purge_pending_scopes(db) == ["s", "t"]
    assert _marker_rows(db) == 0
    rep_s = residual_report(db, "s")
    assert rep_s["controlled"]["live_tables_clean"] is False
    assert rep_s["status"] == "CONTROLLED_NOT_CLEAN"
    rep_t = residual_report(db, "t")
    assert rep_t["controlled"]["live_tables_clean"] is False
    assert rep_t["status"] == "CONTROLLED_NOT_CLEAN"
    detail_t = _marker_detail(db, "t")
    assert detail_t is not None and "generation changed" in detail_t, detail_t
    assert t_key_hex not in detail_t
    m = re.search(r"(\d+) new erase event\(s\)", detail_t)
    assert m is not None and int(m.group(1)) == 3, detail_t
    assert _hex_in_main(path, t_key_hex)
    assert _key_verifies_scope_hash(db, t_key_hex, SIX, "t")
    # Bare-retry convergence for t only: s keeps its legitimate
    # PARTIAL key row, so _assert_clean_retry (which requires 0 key
    # rows and a clean report for the scope) does not apply here.
    out2 = purge_residue(db)
    assert out2["cleaned"] is True, out2
    assert out2["checkpoint"] is not None and out2["checkpoint"][0] == 0
    assert _purge_pending_scopes(db) == []
    db.close()
    assert not _hex_in_main(path, t_key_hex)
    db2 = open_db(path)
    try:
        rep_t2 = residual_report(db2, "t")
        assert rep_t2["controlled"]["live_tables_clean"] is True
        assert rep_t2["status"] != "CONTROLLED_NOT_CLEAN"
    finally:
        db2.close()


# --- (B) failure branch: marker write under lock contention ---

def test_b1_marker_write_fails_under_lock_then_retries(tmp_path):
    """(B1) GPT scenario B, R20 hook shape: the converged DB has no
    live key row; a writer holds BEGIN IMMEDIATE + an uncommitted
    re-insert of B's key, and the purge connection has
    busy_timeout=100, so VACUUM fails with "database is locked"
    and the failure branch is entered. The transaction hook passes
    the 1st attempt through (its BEGIN IMMEDIATE fails on the
    writer's lock, exercising the retry path) and commits the
    writer before the 2nd attempt, which then persists the marker.
    The error notes "retried (2 attempts)"; the live key row (1)
    HMAC-verifies 864218; the report stays CONTROLLED_NOT_CLEAN.
    A first bare retry hits the success-branch recheck (it deletes
    the resurrected row and still reports cleaned=False); a second
    converges. On unfixed code the failure branch never calls
    transaction(), so the writer never commits and no marker is
    persisted — the P0 reproduces."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "b1")
    db.execute("PRAGMA busy_timeout=100")
    writer = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
        " VALUES(?,?,?,?)",
        ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
    state = {"txn_calls": 0, "writer_committed_at": None}
    try:
        with _txn_commit_writer_on_second(state, writer):
            out = purge_residue(db)
            returned = time.monotonic()
    finally:
        writer.close()
    assert state["txn_calls"] == 2, state
    assert state["writer_committed_at"] is not None
    # <=, not <: the writer commits inside the 2nd transaction call,
    # so it strictly precedes the return in real time; equality is
    # only clock granularity (coarse timer resolution on Windows CI),
    # not a late commit. The intent — the commit happened no later
    # than the purge's return — is preserved.
    assert state["writer_committed_at"] <= returned
    assert out["cleaned"] is False, out
    assert isinstance(out.get("error"), str), out
    assert "database is locked" in out["error"], out
    assert "retried (2 attempts)" in out["error"], out
    assert _key_rows(db) == 1
    assert _live_key_verifies(db, "s", b_id, SIX)
    assert _purge_pending_scopes(db) == ["s"]
    assert _marker_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    # Retry 1: the success-branch recheck finds the resurrected row
    # and deletes it, still reporting cleaned=False.
    _assert_failed_purge(db, purge_residue(db))
    # Retry 2: converges.
    _assert_clean_retry(db, path, purge_residue(db), key_hex)


def test_b2_retry_budget_exhausted_reports_unpersisted(tmp_path, monkeypatch):
    """(B2) The writer holds BEGIN IMMEDIATE + an uncommitted key
    re-insert past the whole purge, with the retry budget patched
    down to 0.3 s (pytest monkeypatch fixture, restored
    automatically): every settlement attempt fails on the lock, the
    budget is spent, and the purge reports cleaned=False with an
    error saying the marker could not be persisted — and NO marker
    is written (purge_pending stays empty: the failure is reported,
    not papered over). After the writer rolls back, a bare retry
    converges (at most 3 attempts)."""
    monkeypatch.setattr(E, "_FAILURE_MARKER_RETRY_BUDGET_S", 0.3)
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "b2")
    db.execute("PRAGMA busy_timeout=100")
    writer = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO record_erase_keys(scope_id, record_id, secret_hex, created_at)"
        " VALUES(?,?,?,?)",
        ("s", b_id, key_hex, "2026-11-13T00:00:00Z"))
    try:
        out = purge_residue(db)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert out["cleaned"] is False, out
    assert isinstance(out.get("error"), str), out
    assert "database is locked" in out["error"], out
    assert "could not be persisted" in out["error"], out
    assert _purge_pending_scopes(db) == []
    assert _key_rows(db) == 0  # the writer's row was never committed
    converged = None
    for _ in range(3):
        converged = purge_residue(db)
        if converged["cleaned"]:
            break
    assert converged is not None and converged["cleaned"] is True, converged
    _assert_clean_retry(db, path, converged, key_hex)


def test_b3_had_txn_marker_joins_caller_transaction(tmp_path):
    """(B3) The caller already has a transaction open when
    purge_residue runs: VACUUM cannot run inside it, so the purge
    fails closed — but the marker still joins the caller's
    transaction (same-connection visible, then durable after the
    caller's commit) instead of being dropped. Only the successful
    marker-write path is covered here: a write failure under
    had_txn is the disclosed C-5 window, not asserted."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "b3")
    db.execute("BEGIN")
    db.execute("SELECT COUNT(*) FROM memories").fetchone()
    out = purge_residue(db)
    assert out["cleaned"] is False, out
    assert out.get("error") is not None, out
    # Same connection: the marker is visible (uncommitted, in the
    # caller's transaction) and the report stays NOT_CLEAN.
    assert "s" in _purge_pending_scopes(db)
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    db.commit()
    db.close()
    # A fresh connection still sees the marker: it was persisted by
    # the caller's commit.
    db2 = open_db(path)
    assert "s" in _purge_pending_scopes(db2)
    rep2 = residual_report(db2, "s")
    assert rep2["status"] == "CONTROLLED_NOT_CLEAN"
    _assert_clean_retry(db2, path, purge_residue(db2), key_hex)


# --- (C) R19 scope preserved: clean purges still succeed ---

def test_c_clean_global_and_explicit_purge(tmp_path):
    """(C) No concurrency: a clean global purge and a clean
    explicit-scope purge still report cleaned=True with a busy==0
    checkpoint, and the six-digit key's bytes are gone from the
    main file."""
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "c1")
    _assert_clean_retry(db, path, purge_residue(db), key_hex)
    db, path, a_id, b_id, key_hex = _setup_converged(tmp_path, "c2")
    _assert_clean_retry(db, path, purge_residue(db, "s"), key_hex)


# --- (D) docs: §32 ---

def test_d_known_limitations_section_32():
    """(D) §32 exists and is synced with the R20 rework: §31 no
    longer says R19 was not sent (it records FINAL_VERDICT: FAIL
    and points to §32); §32 records the failure-branch settlement
    (one BEGIN IMMEDIATE transaction, re-collection inside it),
    the failure-path generation semantics, the bounded retry, and
    the unpersisted-marker report; the §31 C-1 wording is
    corrected in place and §32 carries the correction; no TBD
    placeholder anywhere in KNOWN_LIMITATIONS.md or SECURITY.md;
    probe_before/probe_after occur only as historical narrative;
    erasure.py + migration 007 carry no "re-discovers". No
    acceptance numbers are asserted here (§32's R20 acceptance
    bullet carries no pytest total yet)."""
    docs = REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md"
    text = docs.read_text(encoding="utf-8")
    assert "## 32." in text
    sec31 = text.split("## 31.", 1)[1].split("## 32.", 1)[0]
    sec32 = text.split("## 32.", 1)[1]
    # §31 title sync.
    assert "R19 not yet sent" not in sec31
    assert "FINAL_VERDICT: FAIL" in sec31
    assert "see §32" in sec31
    # §32: failure-branch settlement + generation semantics +
    # bounded retry + unpersisted-marker report (actual §32 text).
    low32 = sec32.lower()
    for word in ("begin immediate", "generation",
                 "generation changed after physical check",
                 "re-collect", "failure branch", "retry",
                 "10 s budget", "could not be persisted",
                 "c-1", "final_verdict: fail", "after the return"):
        assert word in low32, word
    # C-1: the old §31 wording must not stand as a current claim —
    # any line keeping it must mark it corrected, and §32 must
    # carry the correction.
    for section in (sec31, sec32):
        for ln in section.splitlines():
            if "that verdict is already a failure" in ln:
                assert "correct" in ln.lower(), ln
    assert "C-1" in sec32 and "correct" in low32
    # No placeholders; historical probe keys only; no re-discovers.
    assert "TBD" not in text
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
