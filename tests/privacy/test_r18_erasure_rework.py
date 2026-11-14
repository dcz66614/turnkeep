"""R18 rework tests for P2-R17-01 / P2-R17-02 / §29:598
(docs/KNOWN_LIMITATIONS.md §30).

P2-R17-01: a global purge_residue(db) collected scopes only from
erase events, so a purge_pending marker belonging to a scope with
no erase event (e.g. an unregistered --scope whose explicit cleanup
failed) was stranded: the global run reported cleaned=True while
residual_report() for that scope still said CONTROLLED_NOT_CLEAN.
The fix: the global collection is the UNION of erase-event scopes
and purge_pending scopes. VACUUM + checkpoint + the file probe are
file-wide, so a global success covers every scope in the union and
clears all their markers; an explicit scope call still settles only
that scope.

P2-R17-02: §29:594 overstated probe rediscovery ("the next
purge_residue's probe_before re-discovers it"). The fix rewrites
§29:594 and sweeps the same-class claims GPT did not list: the two
erasure.py docstrings, the migration 007 comment header, §28:574's
return dict (probe_before/probe_after -> residue_before/
residue_after), and §29:594's stale key name.

§29:598: CHOSE TO ADD ASSERTIONS (see test_r17's CLI tests, which
now assert stderr + caplog carry no PIN in success and failure).
"""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from turnkeep.storage import open_db, SCHEMA_VERSION
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.erasure import (
    erase_record, residual_report, purge_residue,
)

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
REPO_ROOT = Path(__file__).resolve().parents[2]
OFF_RESIDUE_CHILD = REPO_ROOT / "scripts" / "off_residue_child.py"


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _purge_pending_scopes(db):
    return sorted(r[0] for r in db.execute(
        "SELECT scope_id FROM purge_pending").fetchall())


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
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120, env=env)


def _make_off_residue(tmp_path, name, value):
    """An F0 historical-residue DB: the OFF chain (B PARTIAL, A
    COMPLETE, repair B COMPLETE) crashed without closing, so the
    deleted key's bytes stay in the main DB file. Returns
    (db, path, key_hex) with the connection open — the caller must
    keep it open (a clean close would checkpoint the residue away).
    key_hex is B's deleted verification key, recorded by the child
    before the repair deleted the row."""
    path = tmp_path / f"{name}.sqlite3"
    db = _db(tmp_path, name)
    a = remember(db, "s", "a-key", value, source_refs=SRC)
    b = remember(db, "s", "b-key", value, source_refs=SRC)
    a_rev, b_rev = _rev(db, a["id"]), _rev(db, b["id"])
    key_out = tmp_path / f"{name}.keyhex"
    db.close()
    res = _spawn_off_residue(path, a["id"], a_rev, b["id"], b_rev, key_out)
    assert res.returncode == 77, res.stderr
    db = open_db(path)
    return db, path, key_out.read_text(encoding="ascii")


def _make_ghost_marker(db, path, scope):
    """Write a purge_pending marker for a scope that has NO erase
    event: hold a reader so the explicit cleanup fails (busy
    checkpoint), which persists the marker. Returns the failed
    entry. The caller keeps the returned reader open until it
    releases it."""
    reader = _hold_reader(path)
    out = purge_residue(db, scope)
    assert out["cleaned"] is False
    assert out["checkpoint"] is not None
    assert out["checkpoint"][0] == 1  # busy only — log/checkpointed vary
    assert scope in _purge_pending_scopes(db)
    return reader, out


# --- (a) P2-R17-01: global purge collects the union of scopes ---

def test_global_purge_clears_ghost_scope_marker(tmp_path):
    """(a1) Ghost-scope marker (verification §1.1 construction):
    an empty DB, no erase events at all; a failed explicit cleanup
    for "ghost-scope" writes its marker; a GLOBAL purge_residue
    clears it and residual_report(db, "ghost-scope") is clean."""
    path = tmp_path / "t.sqlite3"
    db = open_db(path)
    reader, _ = _make_ghost_marker(db, path, "ghost-scope")
    assert residual_report(db, "ghost-scope")["controlled"]["live_tables_clean"] is False
    reader.close()
    out = purge_residue(db)
    assert out["cleaned"] is True
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 0
    assert _purge_pending_scopes(db) == []
    rep = residual_report(db, "ghost-scope")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["file_purge_pending"] == []
    db.close()


def test_explicit_scope_purge_clears_only_that_scope(tmp_path):
    """(a2) Explicit-scope isolation: markers for scope-a and
    scope-b; an explicit purge of scope-a clears only scope-a's
    marker, scope-b's stays and its report stays NOT_CLEAN; a
    later global purge clears scope-b too."""
    path = tmp_path / "t.sqlite3"
    db = open_db(path)
    reader, _ = _make_ghost_marker(db, path, "scope-a")
    reader2, _ = _make_ghost_marker(db, path, "scope-b")
    assert _purge_pending_scopes(db) == ["scope-a", "scope-b"]
    reader.close()
    reader2.close()
    out = purge_residue(db, "scope-a")
    assert out["cleaned"] is True
    assert _purge_pending_scopes(db) == ["scope-b"]
    rep_b = residual_report(db, "scope-b")
    assert rep_b["controlled"]["live_tables_clean"] is False
    assert rep_b["status"] == "CONTROLLED_NOT_CLEAN"
    assert [p["record_id"] for p in
            rep_b["controlled"]["file_purge_pending"]] == [
        "purge_residue:scope-b"]
    out = purge_residue(db)
    assert out["cleaned"] is True
    assert _purge_pending_scopes(db) == []
    assert residual_report(db, "scope-b")["controlled"]["live_tables_clean"] is True
    db.close()


def test_global_purge_failure_keeps_all_markers(tmp_path):
    """(a3) Global unclean, no misreport: a ghost-scope marker plus
    a real F0 residue (scope "s"); with a reader held the global
    purge fails and BOTH markers are kept, both reports NOT_CLEAN;
    after the reader releases, a global purge succeeds and clears
    both markers, both reports clean. Neither the reverse error
    (global cleaned=True with a marker standing and a clean report)
    nor the false-positive residue (global cleaned=True with a
    report still NOT_CLEAN) may occur."""
    db, path, _key_hex = _make_off_residue(tmp_path, "t", PIN)
    reader, _ = _make_ghost_marker(db, path, "ghost-scope")
    out = purge_residue(db)
    assert out["cleaned"] is False
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 1
    assert _purge_pending_scopes(db) == ["ghost-scope", "s"]
    assert residual_report(db, "s")["status"] == "CONTROLLED_NOT_CLEAN"
    assert residual_report(db, "ghost-scope")["status"] == \
        "CONTROLLED_NOT_CLEAN"
    reader.close()
    out = purge_residue(db)
    assert out["cleaned"] is True
    assert out["checkpoint"] is not None and out["checkpoint"][0] == 0
    assert _purge_pending_scopes(db) == []
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    assert residual_report(db, "ghost-scope")["controlled"]["live_tables_clean"] is True
    db.close()


# --- (b) P2-R17-02 + §29:598: documentation sweep ---

def test_known_limitations_section_30():
    """(b) §30 exists and carries no TBD placeholder; §29:594 is
    rewritten (residue_before, 4-digit PIN range, no probe_before);
    §28:574's return dict uses residue_*; both erasure.py
    docstrings and the migration 007 comment carry no unqualified
    "re-discovers" probe claim; every remaining probe_before /
    probe_after occurrence is historical narrative of the R16
    contract change (the old keys), never a current claim."""
    docs = REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md"
    text = docs.read_text(encoding="utf-8")
    assert "## 30." in text
    # No placeholder anywhere in the file: the older doc locks
    # (test_r16 sec28, test_r17 sec29) split without an upper bound,
    # so a placeholder in §30 would break them too.
    assert "TBD" not in text
    assert "R17 not yet sent" not in text
    # §29:594 — the rewritten crash-window bullet (single long line).
    line594 = [ln for ln in text.splitlines()
               if ln.startswith("- **Marker write timing")][0]
    assert "residue_before" in line594
    assert "4-digit PIN range" in line594
    assert "probe_before" not in line594
    assert "re-discovers" not in line594
    assert "must never be claimed to re-discover all residue" in line594
    # §28:574 — the return dict (single long line, the "(4)
    # Historical-residue cleanup" bullet).
    line574 = [ln for ln in text.splitlines()
               if ln.startswith("- **(4) Historical-residue cleanup:")][0]
    assert "residue_before" in line574 and "residue_after" in line574
    assert "probe_before" not in line574 and "probe_after" not in line574
    # The two erasure.py docstrings + the migration 007 comment.
    era = (REPO_ROOT / "src" / "turnkeep" / "erasure.py").read_text(
        encoding="utf-8")
    assert "re-discovers" not in era
    mig = (REPO_ROOT / "src" / "turnkeep" / "migrations" /
           "007_purge_pending.sql").read_text(encoding="utf-8")
    assert "re-discovers" not in mig
    # Remaining probe_before / probe_after occurrences: historical
    # narrative only. Each occurrence's line must carry one of the
    # historical markers (old key / contract change / the old repro
    # or the old buggy code being described).
    hist_markers = ("old", "contract", "gone", "→",
                    "judged success from", "R15 claim",
                    "returned the recovered PIN in")
    for fpath in (docs, REPO_ROOT / "src" / "turnkeep" / "erasure.py"):
        for ln in fpath.read_text(encoding="utf-8").splitlines():
            if "probe_before" in ln or "probe_after" in ln:
                assert any(m in ln for m in hist_markers), ln
    # "re-discovers" survives only inside §30's own quoted record of
    # the old wording — never as a current claim.
    for ln in text.splitlines():
        if "re-discovers" in ln:
            assert "sweep" in ln, ln
    sec = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "TBD" not in sec


def test_schema_still_v7_no_new_migration():
    """(b) This rework adds no migration: schema stays v7 and the
    migrations directory still holds exactly 001–007."""
    assert SCHEMA_VERSION == 7
    migs = sorted(p.name for p in
                  (REPO_ROOT / "src" / "turnkeep" / "migrations").glob("*.sql"))
    assert migs == [f"{i:03d}_{n}.sql" for i, n in [
        (1, "initial"), (2, "record_provenance"), (3, "scope_secrets"),
        (4, "erase_flags"), (5, "record_erase_keys"),
        (6, "maintenance_pending"), (7, "purge_pending")]], migs
