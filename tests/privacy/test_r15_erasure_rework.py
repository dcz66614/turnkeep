"""R15 rework regression tests: P0-R14-01 (crash consistency — the
maintenance_pending marker must be written in the same transaction
as the verification-key deletion).

Fix rules (verification report §3):
  1. The marker is written in the erase transaction itself, BEFORE
     the key is deleted, in both erase paths (first erase + repair),
     and cleared only after the WAL checkpoint succeeds. A crash
     after the erase transaction commits but before the checkpoint
     must still leave a persisted marker — the old order (commit,
     then checkpoint, then a second transaction writing the marker)
     had a window with no persisted protection at all.
  2. The marker write is UNCONDITIONAL for every terminal COMPLETE
     erase: it covers the deleted key's bytes AND the old payload
     pages the checkpoint has not yet purged. On the first-erase
     path the payload bytes sit in the WAL from the remember frame
     even though its key was created and deleted inside the same
     transaction and never hit disk (verification §1.3 holds only
     for the key+HMAC chain — the raw payload is still recoverable
     pre-checkpoint, which is why the first-erase path needs the
     marker too). The conditional alternative (write only when the
     key row pre-existed) was evaluated and rejected: the checkpoint
     outcome is not known inside the erase transaction, so a
     first-erase busy checkpoint would need a post-commit marker
     write — reintroducing exactly the crash window P0-R14-01 is
     about.
  3. Stale-marker semantics: a marker that stands after a crash
     between checkpoint success and the marker clear means "purge
     UNCONFIRMED" — the file may already be clean; it is a
     fail-closed false positive, never "file dirty". Remedy: a repair
     re-run re-runs the checkpoint and clears the marker on busy==0;
     the clear is idempotent and retryable; a marker is never
     silently cleared without re-running the checkpoint.
  4. Busy-time key restore is unchanged: on busy the captured key
     row is restored and the marker refreshed. After a crash +
     reopen, a repair re-run captures key_row = None (key already
     deleted), so the restore is a no-op and the marker is kept if
     busy again, cleared if the checkpoint succeeds. The stored-hash
     consumers already treat "hashes exist but the key is gone" as a
     non-match, never an error, and repair-reason vetting still
     works via the key-independent erase_flags.
  5. Pending-write failure: the marker write is inside the erase
     transaction, so a write failure rolls the whole erase back
     (key not deleted, event not published) — the erase fails
     atomically, with no window.
  6. open_db crash-recovery: NOT done (option one) — convergence
     stays via a repair re-run; a reopened DB may report NOT_CLEAN
     until someone re-runs repair. Documented in §27.
  7. Crash-injection regression: four crash points x two erase
     paths, each an independent subprocess + os._exit(77) (see
     scripts/crash_child.py). The crash tests' second connection is
     an ordinary reader (BEGIN + SELECT COUNT(*) FROM memories).
     A test that closes the reader must REOPEN the DB first: closing
     the reader would be the last live connection closing, and
     SQLite checkpoints the WAL on a clean last close — purging the
     very key/payload bytes the test must still find.

The per-record-key architecture is NOT overturned (GPT's R15
guidance, agreed): only the crash-consistency loop around it is
closed. No schema change in this rework (still v6).
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep import erasure as E
from turnkeep.erasure import (
    erase_record, residual_report, _run_maintenance, _purge_failed,
    _maintenance_pending, _record_verification_keys,
    _stored_needle_hashes, _check_reason_against_stored_hashes,
    _erased_record_flags,
)
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
PAYLOAD = "PAYLOAD-TOKEN-9f3a7c2e1b5d-UNIQUE-7741"
REPO_ROOT = Path(__file__).resolve().parents[2]
CRASH_CHILD = REPO_ROOT / "scripts" / "crash_child.py"


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


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN against the persisted
    needle hashes, using every key the live DB holds."""
    keys = []
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
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
    """Recover the PIN from the live DB file + WAL bytes alone, with NO
    prior knowledge of any key: a sliding window over every >=64-char
    hex run collects candidate 32-byte keys (a fixed 64-char regex
    window misaligns — the key hex nests inside longer hex strings),
    then each candidate is tried against the event needle hashes."""
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
    """Run scripts/crash_child.py as an independent subprocess; the
    expected exit code is 77 (crashed at the requested point)."""
    argv = [sys.executable, str(CRASH_CHILD), str(db_path), record_id,
            str(revision), mode, point]
    if reason is not None:
        argv.append(reason)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env)


def _setup_pin(tmp_path, name):
    """Repair-path setup: two records sharing the PIN, B erased
    PARTIAL (A still holds the payload)."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    return db, path, a, b


def _setup_payload(tmp_path, name):
    """First-erase-path setup: two records sharing a distinctive
    payload, B erased PARTIAL."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", PAYLOAD, source_refs=SRC)
    b = remember(db, "s", "b-key", PAYLOAD, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    return db, path, a, b


def _converge_pin(db, path, a, b):
    """Erase A, repair B, and assert the report is clean and nothing
    is enumerable from the live files."""
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    assert _enumerate_pin(db) is None
    assert _file_byte_enumerate(str(path), db) is None


def test_crash_pre_pending_repair(tmp_path):
    # (i) Crash inside the erase transaction, at the marker write:
    # atomic rollback — B keeps its pre-erase state, its key row is
    # intact, no marker, no repair event.
    db, path, a, b = _setup_pin(tmp_path, "a-pre-pending")
    rev_b = _rev(db, b["id"])
    db.close()
    proc = _spawn_child(path, b["id"], rev_b, "repair", "pre_pending")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _state(db, b["id"]) == "erased"
    assert _rev(db, b["id"]) == rev_b
    assert _key_rows(db) == 1
    assert _pending_ids(db) == []
    assert _erase_event_count(db) == 1
    rep = residual_report(db, "s")
    assert len(rep["controlled"]["controlled_residuals"]) > 0
    _converge_pin(db, path, a, b)
    db.close()


def test_crash_post_commit_repair(tmp_path):
    # (ii) Crash right after the erase transaction commits (at
    # _run_maintenance): the marker is persisted, the key is deleted,
    # the checkpoint never ran. The key bytes are still in the WAL:
    # the file-byte probe recovers the PIN while the report says
    # NOT_CLEAN (honest, not clean).
    db, path, a, b = _setup_pin(tmp_path, "a-post-commit")
    rev_b = _rev(db, b["id"])
    db.close()
    proc = _spawn_child(path, b["id"], rev_b, "repair", "post_commit")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _pending_ids(db) == [b["id"]]
    assert _key_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert _file_byte_enumerate(str(path), db) == PIN
    assert _enumerate_pin(db) is None
    _converge_pin(db, path, a, b)
    db.close()


def test_crash_post_busy_repair(tmp_path):
    # (iii) Crash after the checkpoint returned busy (at the key
    # restore): the restore never ran, so the key stays deleted and
    # the marker stands. The reader is held from BEFORE B's first
    # erase, so no checkpoint in this DB ever succeeds and the WAL is
    # never reset — B's key INSERT frame stays in the WAL, so the
    # file-byte probe deterministically recovers the PIN. A repair
    # re-run with the reader still held must stay PARTIAL with the
    # marker kept (key_row None -> restore is a no-op).
    path = tmp_path / "a-post-busy.sqlite3"
    db = _db(tmp_path, "a-post-busy")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    reader = _hold_reader(path)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    rev_b = _rev(db, b["id"])
    db.close()
    proc = _spawn_child(path, b["id"], rev_b, "repair", "post_busy")
    # Reopen FIRST, while the reader is still held (see the module
    # docstring, rule 7): closing the reader would be the last live
    # connection closing, and SQLite checkpoints the WAL on a clean
    # last close — purging the key bytes the probe must still find.
    db = open_db(path)
    reader.execute("COMMIT")
    reader.close()
    assert proc.returncode == 77, proc.stdout + proc.stderr
    assert _pending_ids(db) == [b["id"]]
    assert _key_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert _file_byte_enumerate(str(path), db) == PIN
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    reader = _hold_reader(path)
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "PARTIAL"
    assert out_r["maintenance_pending"] is True
    assert _pending_ids(db) == [b["id"]]
    reader.execute("COMMIT")
    reader.close()
    out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r2["status"] == "COMPLETE"
    assert out_r2["maintenance_pending"] is False
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert _file_byte_enumerate(str(path), db) is None
    db.close()


def test_crash_post_checkpoint_repair(tmp_path):
    # (iv) Crash after the checkpoint succeeded, before the marker
    # clears: a STALE marker — the file is actually clean, the report
    # stays NOT_CLEAN (fail-closed false positive), and a repair
    # re-run clears it.
    db, path, a, b = _setup_pin(tmp_path, "a-post-checkpoint")
    rev_b = _rev(db, b["id"])
    db.close()
    proc = _spawn_child(path, b["id"], rev_b, "repair", "post_checkpoint")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _pending_ids(db) == [b["id"]]
    assert _key_rows(db) == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert _file_byte_enumerate(str(path), db) is None
    assert _enumerate_pin(db) is None
    _converge_pin(db, path, a, b)
    db.close()


def test_crash_pre_pending_first(tmp_path):
    # (i) First-erase path: crash inside the erase transaction — the
    # record stays live with its payload, no key row for it, no
    # marker, no erase event for it.
    db, path, a, b = _setup_payload(tmp_path, "b-pre-pending")
    rev_a = _rev(db, a["id"])
    db.close()
    proc = _spawn_child(path, a["id"], rev_a, "first", "pre_pending")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _state(db, a["id"]) == "active"
    assert json.loads(_value(db, a["id"])) == PAYLOAD
    assert _rev(db, a["id"]) == rev_a
    # Only B's PARTIAL key row exists; A's key was never created.
    assert _key_rows(db) == 1
    assert _key_record_ids(db) == {b["id"]}
    assert _pending_ids(db) == []
    assert _erase_event_count(db) == 1
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert not _payload_in_files(str(path), PAYLOAD)
    db.close()


def test_crash_post_commit_first(tmp_path):
    # (ii) First-erase path: crash after the erase transaction commits.
    # The key never hit disk (§1.3: no key-recovery chain), but the raw
    # payload bytes sit in the WAL from the remember frame — the marker
    # is what keeps the report honest until the repair re-run purges
    # them (the §27 correction to §1.3's narrowing).
    db, path, a, b = _setup_payload(tmp_path, "b-post-commit")
    rev_a = _rev(db, a["id"])
    db.close()
    proc = _spawn_child(path, a["id"], rev_a, "first", "post_commit")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _pending_ids(db) == [a["id"]]
    assert _key_rows(db) == 1
    assert _key_record_ids(db) == {b["id"]}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert _payload_in_files(str(path), PAYLOAD)
    assert _file_byte_enumerate(str(path), db) is None
    out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_rb["status"] == "COMPLETE"
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert not _payload_in_files(str(path), PAYLOAD)
    db.close()


def test_crash_post_busy_first(tmp_path):
    # (iii) First-erase path, busy checkpoint: same payload-in-WAL
    # state as (ii), reached via the busy path. The reader is held
    # from BEFORE B's first erase, so the WAL is never reset and the
    # payload's remember frames stay in it.
    path = tmp_path / "b-post-busy.sqlite3"
    db = _db(tmp_path, "b-post-busy")
    a = remember(db, "s", "a-key", PAYLOAD, source_refs=SRC)
    b = remember(db, "s", "b-key", PAYLOAD, source_refs=SRC)
    reader = _hold_reader(path)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    rev_a = _rev(db, a["id"])
    db.close()
    proc = _spawn_child(path, a["id"], rev_a, "first", "post_busy")
    db = open_db(path)
    reader.execute("COMMIT")
    reader.close()
    assert proc.returncode == 77, proc.stdout + proc.stderr
    assert _pending_ids(db) == [a["id"]]
    assert _key_rows(db) == 1
    assert _key_record_ids(db) == {b["id"]}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert _payload_in_files(str(path), PAYLOAD)
    assert _file_byte_enumerate(str(path), db) is None
    out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_rb["status"] == "COMPLETE"
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert not _payload_in_files(str(path), PAYLOAD)
    db.close()


def test_crash_post_checkpoint_first(tmp_path):
    # (iv) First-erase path, stale marker: the checkpoint already
    # purged the payload, so the file is clean and the report's
    # NOT_CLEAN is a fail-closed false positive.
    db, path, a, b = _setup_payload(tmp_path, "b-post-checkpoint")
    rev_a = _rev(db, a["id"])
    db.close()
    proc = _spawn_child(path, a["id"], rev_a, "first", "post_checkpoint")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _pending_ids(db) == [a["id"]]
    assert _key_rows(db) == 1
    assert _key_record_ids(db) == {b["id"]}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert not _payload_in_files(str(path), PAYLOAD)
    assert _file_byte_enumerate(str(path), db) is None
    out_r = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    out_rb = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_rb["status"] == "COMPLETE"
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert _pending_ids(db) == []
    db.close()


def test_pending_write_failure_rolls_back(tmp_path, monkeypatch):
    # (1e) A pending-write failure rolls the whole erase back
    # atomically: the key is not deleted, the event is not published,
    # the record keeps its prior state, and the erase is retryable.
    # (The repair scan is COMPLETE here — the repair path only checks
    # its own receipts, not a second live record's copy — so the
    # marker write is reached and the injected failure fires.)
    db, path, a, b = _setup_pin(tmp_path, "c-rollback")
    rev_b = _rev(db, b["id"])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected pending-write failure")

    monkeypatch.setattr(E, "_set_maintenance_pending", _boom)
    with pytest.raises(RuntimeError, match="injected pending-write failure"):
        erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert _state(db, b["id"]) == "erased"
    assert _rev(db, b["id"]) == rev_b
    assert _key_rows(db) == 1
    assert _pending_ids(db) == []
    assert _erase_event_count(db) == 1
    monkeypatch.undo()
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_consumers_with_key_deleted_marker_present(tmp_path):
    # (1d) The stored-hash consumers in the "key deleted, marker
    # present" state: repair-reason vetting still works via the
    # key-independent erase_flags, and "hashes exist but the key is
    # gone" is a non-match, never an error. Payload "repair" is a
    # label-equals-payload case, so the flags are the only thing
    # vetting the reason after the crash.
    path = tmp_path / "c-consumers.sqlite3"
    db = _db(tmp_path, "c-consumers")
    c = remember(db, "s", "c-key", "repair", source_refs=SRC)
    d = remember(db, "s", "d-key", "repair", source_refs=SRC)
    assert erase_record(db, "s", d["id"], _rev(db, d["id"]),
                        "rotation")["status"] == "PARTIAL"
    assert _erased_record_flags(db, "s", d["id"])["blocked_repair_reasons"] == frozenset({"repair"})
    assert erase_record(db, "s", c["id"], _rev(db, c["id"]),
                        "rotation")["status"] == "COMPLETE"
    rev_d = _rev(db, d["id"])
    db.close()
    # The child repairs D with reason "rotation": the default repair
    # reason "repair" IS the payload, so the reason gate would reject
    # it before any crash point is reached.
    proc = _spawn_child(path, d["id"], rev_d, "repair", "post_commit", "rotation")
    assert proc.returncode == 77, proc.stdout + proc.stderr
    db = open_db(path)
    assert _pending_ids(db) == [d["id"]]
    assert _key_rows(db) == 0
    assert _record_verification_keys(db, "s", d["id"]) == []
    assert len(_stored_needle_hashes(db, "s", d["id"])) == 2
    with pytest.raises(TurnkeepError) as exc:
        _check_reason_against_stored_hashes(db, "s", d["id"], "repair")
    assert exc.value.code == "INVALID_INPUT"
    assert _check_reason_against_stored_hashes(db, "s", d["id"], "rotation") is None
    with pytest.raises(TurnkeepError) as exc:
        erase_record(db, "s", d["id"], _rev(db, d["id"]), "repair")
    assert exc.value.code == "INVALID_INPUT"
    out_r = erase_record(db, "s", d["id"], _rev(db, d["id"]), "rotation")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance_pending"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_no_reader_complete_path_unchanged(tmp_path):
    # GPT acceptance item 5: the normal no-concurrent-reader COMPLETE
    # path must not regress — the marker is written in-transaction
    # and cleared after the checkpoint, leaving no stale marker.
    db, path, a, b = _setup_pin(tmp_path, "c")
    out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    assert out_a["status"] == "COMPLETE"
    assert out_a["maintenance"] == []
    assert out_a["maintenance_pending"] is False
    assert _pending_ids(db) == []
    out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r["status"] == "COMPLETE"
    assert out_r["maintenance"] == []
    assert out_r["maintenance_pending"] is False
    assert _pending_ids(db) == []
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert _key_rows(db) == 0
    assert _enumerate_pin(db) is None
    assert _file_byte_enumerate(str(path), db) is None
    db.close()


def test_short_payloads_still_complete(tmp_path):
    for payload in ("202", "2026"):
        db = _db(tmp_path, f"e-{payload}")
        rec = remember(db, "s", "k", payload, source_refs=SRC)
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        assert out["status"] == "COMPLETE", payload
        db.close()


def test_section_26_27_docs():
    text = (REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    s26 = text.split("## 26.", 1)[1].split("## 27.", 1)[0]
    s27 = text.split("## 27.", 1)[1]
    assert "TBD" not in s26
    assert "UNCONDITIONAL" in s27
    assert "purge UNCONFIRMED" in s27
    assert "open_db crash-recovery" in s27
    # R16 conforming update (not a weakening): §27:565's TBD was
    # rewritten by the R15 rework (KNOWN_LIMITATIONS §28), so the
    # placeholder no longer exists — the section now carries the R15
    # verdict and P0-R15-01 instead.
    assert "TBD after R15 verdict" not in s27
    assert "P0-R15-01" in s27
    assert "secure_delete" in s27


def test_stale_marker_semantics_in_report_scope(tmp_path):
    # The report's live_tables_clean_scope says what a marker means:
    # "purge not yet confirmed complete", never "file dirty".
    db = _db(tmp_path, "s")
    rep = residual_report(db, "s")
    scope = rep["controlled"]["live_tables_clean_scope"]
    assert "purge not yet confirmed complete" in scope
    assert "file dirty" in scope
    db.close()


def test_busy_checkpoint_chain_still_works(tmp_path):
    # R14 A1 chain (count-only reader) after the restructure: busy ->
    # PARTIAL + marker, report not clean, key rows restored; reader
    # released -> repair re-run -> COMPLETE, marker cleared, key rows
    # 0, enumeration finds nothing.
    path = tmp_path / "chain.sqlite3"
    db = _db(tmp_path, "chain")
    a = remember(db, "s", "a-key", PIN, source_refs=SRC)
    b = remember(db, "s", "b-key", PIN, source_refs=SRC)
    assert erase_record(db, "s", b["id"], _rev(db, b["id"]),
                        "rotation")["status"] == "PARTIAL"
    reader = _hold_reader(path)
    try:
        out_a = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
        assert out_a["status"] == "PARTIAL"
        assert any("wal_checkpoint" in n and "busy" in n for n in out_a["maintenance"])
        assert out_a["maintenance_pending"] is True
        out_r = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
        assert out_r["status"] == "PARTIAL"
        assert out_r["maintenance_pending"] is True
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        assert rep["status"] == "CONTROLLED_NOT_CLEAN"
        assert set(_pending_ids(db)) == {a["id"], b["id"]}
        assert _key_rows(db) == 2
        assert _enumerate_pin(db) == PIN
        assert _maintenance_pending(db, "s")
        assert all("4827" not in p["detail"] for p in _maintenance_pending(db, "s"))
    finally:
        reader.execute("COMMIT")
        reader.close()
    out_r2 = erase_record(db, "s", b["id"], _rev(db, b["id"]), "repair")
    assert out_r2["status"] == "COMPLETE"
    assert out_r2["maintenance"] == []
    assert out_r2["maintenance_pending"] is False
    out_a2 = erase_record(db, "s", a["id"], _rev(db, a["id"]), "repair")
    assert out_a2["status"] == "COMPLETE"
    assert out_a2["maintenance_pending"] is False
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert rep2["controlled"]["file_purge_pending"] == []
    assert _key_rows(db) == 0
    assert _enumerate_pin(db) is None
    assert _file_byte_enumerate(str(path), db) is None
    db.close()
