#!/usr/bin/env python3
"""R9 final-review repro: the R8 rework, re-runnable by the reviewer.

  python scripts/repro_r9.py            # from a source checkout
  python -m turnkeep ...                # (installed wheel: same checks,
                                        #  import falls back to src/ below)

Runs against throwaway SQLite files in a TemporaryDirectory — no
network, no existing database, no home-directory paths. Every check
asserts CORRECT behaviour after the R8 rework; a FAIL line means the
defect reproduces. Prints PASS/FAIL per check plus a total; exits 1
if any check fails.

Coverage (P0-R8-01 / P0-R8-02 / P1 / P2-R8-03, plus R7/R6 spot checks):
  A1  key == payload: import rejects it (verbatim and Base64); a legacy
      record (pre-fix import, no remember event) erases COMPLETE and the
      chain is clean — DB, residual report, records.jsonl, events.jsonl —
      and the erase event carries no raw key (key_redacted).
  A2  source id == payload: import rejects it; the remember chain erases
      COMPLETE, the receipt dump's source-ref id is replaced, the
      record's own source links are deleted, export is clean.
  A3  source id == Base64(payload): import rejects it; the chain erases
      COMPLETE and the export is clean (exact equality cannot stop an
      encoding; the hash check can).
  A4  cross-record: X's value is the payload, Y's source id is the
      payload. Erasing X COMPLETEs, Y's live link survives, Y is
      excluded from export. Harder variant (Y has an idempotency
      receipt): erasing X is PARTIAL — the honest refusal, same as a
      second live record holding the payload — residual NOT clean, and
      the export still excludes Y.
  A5  tombstone export: a tombstone row whose key/source_refs were the
      payload is excluded from export (the R7 check was vacuous for
      tombstone rows — tombstone-blindness).
  B1  timestamp exemption is structural now: GPT's A/B repro (record B
      value_json {"created_at": <legal ts>}) makes erasing A PARTIAL and
      names B; nested created_at and the non-timestamp-key control are
      PARTIAL too; a receipt dump's "value" subtree is never exempt;
      the audit positions (events metadata valid_until, receipt top-level
      timestamps) stay exempt, so "202" + a real expiry still COMPLETEs.
  C1  receipt source_refs tracking (P1): _receipt_holds_value sees a
      source-ref id that IS the payload, and the repair erase cleans it.
  D1  short-needle identifier rule (P2): a random uuid in the receipt
      merely containing "202" never refuses an erase — 200x pinned-id
      loop + 200x random-id loop, both COMPLETE every time. (Corrected
      mechanism: the flake is a "202" substring at an arbitrary position
      in a random uuid in the receipt — cause_event_id — NOT a record id
      starting "2020".) A key merely containing "202" also COMPLETEs.
  E   R7/R6 spot checks still hold (import timestamp-field rejection,
      "202"/"2026" erasures COMPLETE, timezone-aware sweep, glued and
      Base64 erase reasons rejected, clean label erases COMPLETE).
  F   runs.task encoding channel (RETAINED, no code fix): the encoding
      stays in the live DB's runs.task, NOT in the export.

The unified rule (docs/KNOWN_LIMITATIONS.md §21): a whole value EQUAL
to the payload (or its encoding) is a copy and must be handled; a random
id that merely CONTAINS a short needle substring is not a copy and must
be allowed through. The erase scan, the import gate, the receipt /
snapshot scrub, and the export exclusion all apply that one rule.
"""
import base64
import codecs
import hashlib
import json
import sys
import tempfile
from pathlib import Path

try:
    import turnkeep  # noqa: F401  (installed wheel)
except ImportError:  # source checkout: the RC root holds src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import turnkeep  # noqa: F401

from turnkeep.storage import open_db, _json, _now, insert_record
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retention import set_expiry, sweep_expired
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, _cell_holds_needle,
    _receipt_holds_value,
)
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin

SRC = [{"id": "u:1", "type": "user_statement"}]
SECRET = "sk-TESTSECRET-abcdef"
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r9-"))
CHECKS = []


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def _db(name, scope="s"):
    db = open_db(TMPROOT / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _mk_package(records, scope="s"):
    d = TMPROOT / f"pkg-{len(list(TMPROOT.glob('pkg-*')))}"
    d.mkdir()
    rec_text = "".join(_json(r) + "\n" for r in records)
    (d / "records.jsonl").write_text(rec_text, encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    files = {"records.jsonl": hashlib.sha256(rec_text.encode()).hexdigest(),
             "events.jsonl": hashlib.sha256(b"").hexdigest()}
    manifest = {"format": "turnkeep-export", "schema_version": 1,
                "scope_id": scope, "exported_at": _now(), "tool": "repro-r9",
                "record_count": len(records), "event_count": 0, "files": files}
    (d / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
    return d


def _rec(rid, key, value, **kw):
    r = {"id": rid, "key": key, "value": value, "kind": "fact",
         "state": "active", "revision": 1,
         "created_at": LEGAL_CREATED, "updated_at": LEGAL_CREATED,
         "valid_until": None, "cause_event_id": "x",
         "source_refs": SRC, "parent_ids": [], "supersedes_ids": []}
    r.update(kw)
    return r


def _export_text(db, name):
    out = TMPROOT / f"exp-{name}"
    export_scope(db, "s", out)
    return ((out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _legacy_record(db, rid, key, value, sources=()):
    """A record as a pre-fix import left it: present in memories +
    sources, with NO remember event (the import event never recorded
    keys)."""
    insert_record(db, "s", key, _json(value), "fact", list(sources), [],
                  {}, state="active", cause_event_id="legacy-import",
                  record_id=rid)
    return rid


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _chain_clean(db, name):
    """DB + residual report + both export files free of the payload."""
    rep = residual_report(db, "s")
    ok = rep["controlled"]["live_tables_clean"] is True
    ok = ok and SECRET not in _json(rep)
    recs, evs = _export_text(db, name)
    ok = ok and SECRET not in recs and SECRET not in evs
    ok = ok and _b64(SECRET) not in recs and _b64(SECRET) not in evs
    return ok, recs, evs


# ---------------------------------------------------------------- A1: key == payload

def a1():
    print("\n== A1: key == payload ==")
    db = _db("a1-import")
    for label, key in (("verbatim", SECRET), ("base64", _b64(SECRET))):
        pkg = _mk_package([_rec("o1", key, {"api_key": SECRET})])
        try:
            import_package(db, pkg, target_scope="s")
            check(f"A1 import key==payload ({label}) rejected", False, "imported")
        except TurnkeepError as e:
            check(f"A1 import key==payload ({label}) rejected", e.code == "INVALID_INPUT", e.code)
    db.close()

    db = _db("a1-chain")
    _legacy_record(db, "r1", SECRET, {"api_key": SECRET}, sources=SRC)
    begin(db, "s", task="check key record", idempotency_key="run1")
    out = erase_record(db, "s", "r1", _rev(db, "r1"), "rotation")
    check("A1 legacy key==payload erases COMPLETE", out["status"] == "COMPLETE",
          f"status={out['status']} remaining={out['remaining']}")
    ev_meta = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase'"
        " ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    meta = json.loads(ev_meta)
    check("A1 erase event omits the raw key (key_redacted)",
          "key" not in meta and meta.get("key_redacted") is True and SECRET not in ev_meta)
    snap = db.execute("SELECT snapshot_json FROM runs WHERE scope_id='s'").fetchone()[0]
    check("A1 run snapshot key replaced", SECRET not in snap)
    ok, recs, _ = _chain_clean(db, "a1-chain")
    check("A1 chain clean (DB + residual + records.jsonl + events.jsonl)", ok)
    check("A1 tombstone row excluded from export", "r1" not in recs)
    db.close()


# ---------------------------------------------------------------- A2/A3: source id == payload / base64

def a2_a3():
    print("\n== A2/A3: source id == payload / base64(payload) ==")
    for label, sid in (("verbatim", SECRET), ("base64", _b64(SECRET))):
        db = _db(f"a23-import-{label}")
        pkg = _mk_package([_rec("o1", "k1", {"api_key": SECRET},
                                source_refs=[{"id": sid, "type": "user_statement"}])])
        try:
            import_package(db, pkg, target_scope="s")
            check(f"A2/A3 import source id==payload ({label}) rejected", False, "imported")
        except TurnkeepError as e:
            check(f"A2/A3 import source id==payload ({label}) rejected",
                  e.code == "INVALID_INPUT", e.code)
        db.close()

        db = _db(f"a23-chain-{label}")
        rec = remember(db, "s", "k1", {"api_key": SECRET},
                       source_refs=[{"id": sid, "type": "user_statement"}],
                       idempotency_key="k1")
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        check(f"A2/A3 source id==payload ({label}) erases COMPLETE",
              out["status"] == "COMPLETE", f"status={out['status']}")
        check(f"A2/A3 source links cleared ({label})", out["cleared_source_ids"] >= 1,
              f"cleared={out['cleared_source_ids']}")
        check(f"A2/A3 receipt identifiers scrubbed ({label})",
              out["scrubbed_identifiers"] >= 1, f"scrubbed={out['scrubbed_identifiers']}")
        ok, _, _ = _chain_clean(db, f"a23-chain-{label}")
        check(f"A2/A3 chain clean ({label})", ok)
        db.close()


# ---------------------------------------------------------------- A4: cross-record

def a4():
    print("\n== A4: cross-record source id ==")
    db = _db("a4")
    pkg = _mk_package([
        _rec("rx", "kx", {"api_key": SECRET}),
        _rec("ry", "ky", {"note": "other"},
             source_refs=[{"id": SECRET, "type": "user_statement"}]),
    ])
    plan = import_package(db, pkg, target_scope="s")
    check("A4 import accepted (imported=2)", plan.get("imported") == 2)
    xrid = db.execute("SELECT id FROM memories WHERE scope_id='s' AND key='kx'").fetchone()[0]
    yrid = db.execute("SELECT id FROM memories WHERE scope_id='s' AND key='ky'").fetchone()[0]
    out = erase_record(db, "s", xrid, _rev(db, xrid), "rotation")
    check("A4 erase of X COMPLETE", out["status"] == "COMPLETE", f"status={out['status']}")
    check("A4 Y's live link survives",
          db.execute("SELECT 1 FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
                     (yrid, SECRET)).fetchone() is not None)
    ok, recs, _ = _chain_clean(db, "a4")
    check("A4 chain clean", ok)
    check("A4 Y excluded from export", "ky" not in recs)
    db.close()

    db = _db("a4-hard")
    x = remember(db, "s", "kx", {"api_key": SECRET}, source_refs=SRC, idempotency_key="kx")
    remember(db, "s", "ky", {"note": "other"},
             source_refs=[{"id": SECRET, "type": "user_statement"}], idempotency_key="ky")
    out = erase_record(db, "s", x["id"], _rev(db, x["id"]), "rotation")
    check("A4-hard erase of X PARTIAL (Y's receipt holds the payload)",
          out["status"] == "PARTIAL" and "idempotency" in out["remaining"],
          f"status={out['status']} remaining={out['remaining']}")
    rep = residual_report(db, "s")
    check("A4-hard residual NOT clean", rep["controlled"]["live_tables_clean"] is False)
    recs, evs = _export_text(db, "a4-hard")
    check("A4-hard export still clean (Y excluded)", SECRET not in recs and SECRET not in evs
          and "ky" not in recs)
    db.close()


# ---------------------------------------------------------------- B1: timestamp exemption, structural

def b1():
    print("\n== B1: timestamp exemption is structural ==")
    db = _db("b1-ab")
    a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
    b = remember(db, "s", "kb", {"created_at": "2026-11-12T22:00:00+00:00"},
                 source_refs=SRC, idempotency_key="kb")
    out = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    b_rowid = db.execute("SELECT rowid FROM memories WHERE scope_id='s' AND id=?",
                         (b["id"],)).fetchone()[0]
    check("B1 A/B: erase of A PARTIAL, names B",
          out["status"] == "PARTIAL" and b_rowid in out["remaining"].get("memories", []),
          f"status={out['status']} remaining={out['remaining']}")
    db.close()

    for label, value in (
            ("nested", {"outer": {"created_at": "2026-11-12T22:00:00+00:00"}}),
            ("control", {"note": "2026-11-12T22:00:00+00:00"})):
        db = _db(f"b1-{label}")
        a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
        remember(db, "s", "kb", value, source_refs=SRC, idempotency_key="kb")
        out = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
        check(f"B1 {label} created_at in user data: PARTIAL", out["status"] == "PARTIAL",
              f"status={out['status']}")
        db.close()

    db = _db("b1-receipt")
    a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
    remember(db, "s", "kb", {"created_at": "2026-11-12T22:00:00+00:00"},
             source_refs=SRC, idempotency_key="kb")
    begin(db, "s", task="check kb", idempotency_key="run1")
    out = erase_record(db, "s", a["id"], _rev(db, a["id"]), "rotation")
    check("B1 receipt value-subtree never exempt: PARTIAL", out["status"] == "PARTIAL",
          f"status={out['status']}")
    db.close()

    db = _db("b1-audit")
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check("B1 audit positions still exempt: '202' + real expiry COMPLETEs",
          out["status"] == "COMPLETE", f"status={out['status']}")
    db.close()


# ---------------------------------------------------------------- C1: receipt source_refs tracking

def c1():
    print("\n== C1: receipt source_refs residual tracking ==")
    db = _db("c1")
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC, idempotency_key="k1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check("C1 erase COMPLETE", out["status"] == "COMPLETE")
    check("C1 receipt holds nothing after erase",
          _receipt_holds_value(db, "s", rec["id"]) is False)
    row = db.execute("SELECT rowid, response_json FROM idempotency WHERE scope_id='s'").fetchone()
    data = json.loads(row[1])
    data["source_refs"] = [{"id": SECRET, "type": "user_statement"}]
    db.execute("UPDATE idempotency SET response_json=? WHERE rowid=?", (_json(data), row[0]))
    db.commit()
    check("C1 _receipt_holds_value sees the dirty source-ref id",
          _receipt_holds_value(db, "s", rec["id"]) is True)
    out2 = erase_record(db, "s", rec["id"], out["revision"], "repair")
    check("C1 repair erase cleans it (COMPLETE)",
          out2["status"] == "COMPLETE" and _receipt_holds_value(db, "s", rec["id"]) is False,
          f"status={out2['status']}")
    ok, _, _ = _chain_clean(db, "c1")
    check("C1 chain clean after repair", ok)
    db.close()


# ---------------------------------------------------------------- D1: short-needle identifier rule

def d1():
    print("\n== D1: short-needle identifier rule (P2) ==")
    cell_sub = _json({"id": "abc", "key": "k1", "cause_event_id": "88e72202af49f077f",
                      "value": {"note": "plain"}, "created_at": LEGAL_CREATED})
    cell_eq = _json({"id": "abc", "key": "k1", "cause_event_id": "202",
                     "value": {"note": "plain"}, "created_at": LEGAL_CREATED})
    check("D1 uuid merely containing '202' is not a hit",
          _cell_holds_needle(cell_sub, "202", "idempotency", "response_json") is False)
    check("D1 identifier EQUAL to '202' is a hit",
          _cell_holds_needle(cell_eq, "202", "idempotency", "response_json") is True)
    check("D1 key merely containing '202' is not a hit",
          _cell_holds_needle(_json({"key": "k202x", "reason": "rotation"}),
                             "202", "events", "metadata_json") is False)

    fails = 0
    for i in range(200):
        db = _db(f"d1-pinned-{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1",
                       record_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        if out["status"] != "COMPLETE":
            fails += 1
        db.close()
    check("D1 pinned-id loop 200/200 COMPLETE", fails == 0, f"fails={fails}")

    fails = 0
    for i in range(200):
        db = _db(f"d1-random-{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        if out["status"] != "COMPLETE":
            fails += 1
        db.close()
    check("D1 random-id loop 200/200 COMPLETE (fresh cause_event_ids)", fails == 0,
          f"fails={fails}")


# ---------------------------------------------------------------- E: R7/R6 spot checks

def e_spots():
    print("\n== E: R7/R6 spot checks ==")
    db = _db("e-r7")
    pkg = _mk_package([_rec("o1", "k1", {"api_key": SECRET},
                            created_at="x" + SECRET + "y")])
    try:
        import_package(db, pkg, target_scope="s")
        check("E R7 import created_at with verbatim secret rejected", False, "imported")
    except TurnkeepError as ex:
        check("E R7 import created_at with verbatim secret rejected",
              ex.code == "INVALID_INPUT", ex.code)
    rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key="k1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check("E R7 value '2026' erases COMPLETE", out["status"] == "COMPLETE",
          f"status={out['status']}")
    db.close()

    db = _db("e-sweep")
    r1 = remember(db, "s", "k1", "v1", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", r1["id"], "2026-11-12T22:00:00+02:00", r1["revision"])
    r2 = remember(db, "s", "k2", "v2", source_refs=SRC, idempotency_key="k2")
    set_expiry(db, "s", r2["id"], "2026-11-12T19:00:00-02:00", r2["revision"])
    swept = sweep_expired(db, "s", now="2026-11-12T20:30:00+00:00")["expired"]
    swept_keys = [db.execute("SELECT key FROM memories WHERE scope_id='s' AND id=?",
                             (rid,)).fetchone()[0] for rid in swept]
    check("E R7 sweep is timezone-aware (+02:00 expired, -02:00 not)",
          swept_keys == ["k1"], f"swept={swept_keys}")
    db.close()

    db = _db("e-r6")
    rec = remember(db, "s", "k1", SECRET, source_refs=SRC, idempotency_key="k1")
    for label, reason in (("glued", "rotatedabcagain"),
                          ("base64", _b64(SECRET)),
                          ("percent", "%73%6b%2d%54%45%53%54%53%45%43%52%45%54%2d%61%62%63%64%65%66")):
        try:
            erase_record(db, "s", rec["id"], _rev(db, rec["id"]), reason)
            check(f"E R6 erase reason {label} rejected", False, "accepted")
        except TurnkeepError as ex:
            check(f"E R6 erase reason {label} rejected", ex.code == "INVALID_INPUT", ex.code)
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check("E R6 clean label erases COMPLETE", out["status"] == "COMPLETE",
          f"status={out['status']}")
    db.close()


# ---------------------------------------------------------------- F: runs.task retained

def f_runs_task():
    print("\n== F: runs.task encoding channel (retained) ==")
    db = _db("f")
    rec = remember(db, "s", "k1", SECRET, source_refs=SRC, idempotency_key="k1")
    begin(db, "s", task="check " + _b64(SECRET), idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    task = db.execute("SELECT task FROM runs WHERE scope_id='s'").fetchone()[0]
    check("F encoding stays in live DB runs.task (disclosed limitation)",
          _b64(SECRET) in task and out["status"] == "COMPLETE",
          f"status={out['status']}")
    recs, evs = _export_text(db, "f")
    check("F encoding NOT in export (runs are not exported)",
          _b64(SECRET) not in recs and _b64(SECRET) not in evs)
    db.close()


def main():
    a1()
    a2_a3()
    a4()
    b1()
    c1()
    d1()
    e_spots()
    f_runs_task()
    total = len(CHECKS)
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
