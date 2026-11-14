#!/usr/bin/env python3
"""R9 rework repro (ships in the RC as evidence for final-review R10).

Run from a source checkout or an installed wheel:
    python scripts/repro_r10.py
All checks run against throwaway SQLite files and assert the FIXED
behaviour for the R9 final-review findings (verified in
~/workspace/turnkeep-gpt-R9-verification-2026-11-12.md):

  A1  P0-R9-01: an encoded key via remember() is caught by the
      decode-aware identifier scan — the erase refuses PARTIAL and
      names the append-only event (all six encodings; the urlsafe /
      percent forms use the short "aa>" payload, where they are
      genuinely distinct from std Base64).
  A2  P0-R9-02: _decoded_forms("YWE-") contains "aa>", and the import
      rejects a URL-safe-Base64 key ("YWE-") as well as the std form;
      the remember() -> erase chain refuses PARTIAL.
  A3  P0-R9-03: an encoded whole-value copy in another record is
      caught — erasing A refuses PARTIAL and names B (all six
      encodings); erasing B then repair-erasing A resolves it, and the
      export already excludes B (urlsafe included).
  A4  P1-R9-04: export / import report entries no longer carry a raw
      payload key or a reversible encoding of one — the key is omitted
      and flagged "key_redacted"; benign keys stay visible; the CLI's
      stdout is clean.
  D   Retained (no code action): a reversible encoding in runs.task
      free text is NOT scanned for (docs/KNOWN_LIMITATIONS.md §8) —
      it stays in the live DB, the residual still says clean, and it
      is NOT in the export.
  E   "202" / "2026" still erase COMPLETE (whole-value decoded
      equality did not bring back the P2-R8-03 false-positive class).
  F   R8 spot checks: the R8 fixes still hold in their original scope
      (key == payload chain, source-id Base64 chain).
"""
import base64
import codecs
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import turnkeep  # noqa: F401  (proves the import resolves: source or wheel)
from turnkeep.storage import open_db, _json, _now
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retention import set_expiry
from turnkeep.erasure import (
    erase_record, residual_report, _decoded_forms, _encoded_forms,
)
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PAYLOAD = "SYNTHETIC-PAYLOAD-9f3a7c2e"
SHORT = "aa>"  # GPT's P0-R9-02 repro value; urlsafe Base64 is "YWE-"
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r10-"))
CHECKS = []


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def _db(name, scope="s"):
    db = open_db(TMPROOT / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _encodings(s):
    return {
        "b64": base64.b64encode(s.encode()).decode(),
        "urlsafe": base64.urlsafe_b64encode(s.encode()).decode(),
        "percent": urllib.parse.quote(s),
        "reversed": s[::-1],
        "hex": s.encode().hex(),
        "rot13": codecs.encode(s, "rot_13"),
    }


def _mk_package(records, scope="s"):
    d = TMPROOT / f"pkg-{len(list(TMPROOT.glob('pkg-*')))}"
    d.mkdir()
    rec_text = "".join(_json(r) + "\n" for r in records)
    (d / "records.jsonl").write_text(rec_text, encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    files = {"records.jsonl": hashlib.sha256(rec_text.encode()).hexdigest(),
             "events.jsonl": hashlib.sha256(b"").hexdigest()}
    manifest = {"format": "turnkeep-export", "schema_version": 1,
                "scope_id": scope, "exported_at": _now(), "tool": "repro-r10",
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


def _export_rep(db, name):
    out = TMPROOT / f"exp-{name}"
    rep = export_scope(db, "s", out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _latest_erase_meta(db):
    meta = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase'"
        " ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    return json.loads(meta)


print(f"turnkeep: {turnkeep.__file__}")
encs = _encodings(PAYLOAD)
encs_short = _encodings(SHORT)
print("encodings of PAYLOAD:", {k: v for k, v in encs.items()})
print("encodings of SHORT:", {k: v for k, v in encs_short.items()})

# ---------------------------------------------------------------- A1: encoded key
print("\n== A1: encoded key via remember() (P0-R9-01) ==")
for label, s, table in (("PAYLOAD", PAYLOAD, encs), ("SHORT", SHORT, encs_short)):
    for name, enc in table.items():
        db = _db(f"a1-{label}-{name}")
        rec = remember(db, "s", enc, s, source_refs=SRC,
                       idempotency_key=f"a1-{label}-{name}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        rep = residual_report(db, "s")
        meta = _latest_erase_meta(db)
        check(f"A1-{label}-{name}: erase PARTIAL, residual NOT clean",
              out["status"] == "PARTIAL"
              and rep["controlled"]["live_tables_clean"] is False
              and "append-only" in out["reason"],
              f"status={out['status']}")
        # R10 rework: the refusal now also names the event holding the
        # payload (its id) — the entry gains "event_id".
        remember_ev = db.execute(
            "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
            ).fetchone()[0]
        check(f"A1-{label}-{name}: PARTIAL event names the record, key redacted",
              meta["erase_status"] == "PARTIAL"
              and meta["append_only_table"] == "events"
              and meta["remaining"] == [{"table": "events", "record_id": rec["id"],
                                         "event_id": remember_ev,
                                         "key_redacted": True}]
              and "key" not in meta and enc not in _json(meta))
        rep2, recs, evs = _export_rep(db, f"a1-{label}-{name}")
        check(f"A1-{label}-{name}: records.jsonl clean (record excluded)",
              enc not in recs)
        # events.jsonl still holds the remember event: append-only
        # history is never rewritten — the fix is the honest refusal,
        # not a silent COMPLETE; the remedy is export-to-fresh-DB.
        check(f"A1-{label}-{name}: events.jsonl still holds the encoded key (append-only, expected)",
              enc in evs)
        db.close()

# ---------------------------------------------------------------- A2: urlsafe
print("\n== A2: urlsafe_b64decode(validate=True) (P0-R9-02) ==")
check("A2-unit: _decoded_forms('YWE-') contains 'aa>'",
      SHORT in _decoded_forms("YWE-"), f"forms={sorted(_decoded_forms('YWE-'))}")
check("A2-unit: _decoded_forms('YWE+') contains 'aa>' (std b64 control)",
      SHORT in _decoded_forms("YWE+"))
for key in ("YWE-", "YWE+"):
    db = _db(f"a2-import-{key}")
    pkg = _mk_package([_rec("r1", key, SHORT)])
    try:
        import_package(db, pkg, target_scope="s")
        check(f"A2-import: key '{key}' rejected", False, "imported")
    except TurnkeepError as e:
        check(f"A2-import: key '{key}' rejected", e.code == "INVALID_INPUT", e.code)
    db.close()
db = _db("a2-chain")
rec = remember(db, "s", "YWE-", SHORT, source_refs=SRC, idempotency_key="a2")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A2-chain: remember(YWE-) -> erase PARTIAL, residual NOT clean",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
rep2, recs, evs = _export_rep(db, "a2-chain")
check("A2-chain: records.jsonl clean, events.jsonl holds the (refused) history",
      "YWE-" not in recs and "YWE-" in evs)
db.close()

# ---------------------------------------------------------------- A3: cross-record
print("\n== A3: encoded copy in another record (P0-R9-03) ==")
for label, s, table in (("PAYLOAD", PAYLOAD, encs), ("SHORT", SHORT, encs_short)):
    for name, enc in table.items():
        db = _db(f"a3-{label}-{name}")
        ra = remember(db, "s", "kA", s, source_refs=SRC,
                      idempotency_key=f"a3a-{label}-{name}")
        rb = remember(db, "s", "kB", enc, source_refs=SRC,
                      idempotency_key=f"a3b-{label}-{name}")
        out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
        rep = residual_report(db, "s")
        check(f"A3-{label}-{name}: erase A PARTIAL, residual NOT clean, names B",
              out["status"] == "PARTIAL"
              and rep["controlled"]["live_tables_clean"] is False
              and any(r["record_id"] == rb["id"]
                      for r in rep["controlled"]["controlled_residuals"]),
              f"status={out['status']}")
        rep2, recs, _ = _export_rep(db, f"a3-{label}-{name}")
        check(f"A3-{label}-{name}: export excludes B (all six encodings)",
              enc not in recs)
        # Resolve: erase B, then repair-erase A.
        outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
        outa = erase_record(db, "s", ra["id"], _rev(db, ra["id"]), "repair")
        rep3 = residual_report(db, "s")
        rep4, recs2, evs2 = _export_rep(db, f"a3r-{label}-{name}")
        check(f"A3-{label}-{name}: erase B + repair A resolves (COMPLETE, clean, export clean)",
              outb["status"] == "COMPLETE" and outa["status"] == "COMPLETE"
              and rep3["controlled"]["live_tables_clean"] is True
              and enc not in recs2 and enc not in evs2
              and s not in recs2 and s not in evs2)
        db.close()

# ---------------------------------------------------------------- A4: report redaction
print("\n== A4: report surfaces (P1-R9-04) ==")
db = _db("a4-case1")
remember(db, "s", PAYLOAD, PAYLOAD, source_refs=SRC, idempotency_key="a4-1")
rep, recs, evs = _export_rep(db, "a4-case1")
check("A4-case1: excluded entry redacts the RAW payload key",
      len(rep["records_excluded_secrets"]) == 1
      and "key" not in rep["records_excluded_secrets"][0]
      and rep["records_excluded_secrets"][0].get("key_redacted") is True
      and PAYLOAD not in _json(rep))
db.close()

db = _db("a4-case2")
enc_b64 = encs["b64"]
rec = remember(db, "s", enc_b64, PAYLOAD, source_refs=SRC, idempotency_key="a4-2")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep, recs, evs = _export_rep(db, "a4-case2")
check("A4-case2: excluded entry redacts the Base64 key (reversible)",
      out["status"] == "PARTIAL"
      and len(rep["records_excluded_secrets"]) == 1
      and "key" not in rep["records_excluded_secrets"][0]
      and rep["records_excluded_secrets"][0].get("key_redacted") is True
      and enc_b64 not in _json(rep) and enc_b64 not in recs)
db.close()

db = _db("a4-stripped")
rec = remember(db, "s", enc_b64, "plain note", source_refs=SRC, idempotency_key="a4-s")
db.execute("UPDATE memories SET valid_until=? WHERE id=?", ("x" + PAYLOAD + "y", rec["id"]))
db.commit()
rep, recs, evs = _export_rep(db, "a4-stripped")
check("A4-stripped: stripped entry redacts the Base64 key",
      len(rep["stripped_illegal_timestamps"]) == 1
      and rep["stripped_illegal_timestamps"][0]["fields"] == ["valid_until"]
      and "key" not in rep["stripped_illegal_timestamps"][0]
      and rep["stripped_illegal_timestamps"][0].get("key_redacted") is True
      and enc_b64 not in _json(rep))
db.close()

db = _db("a4-benign")
remember(db, "s", "ci.api_key", "api_key=<redacted>", source_refs=SRC, idempotency_key="a4-b1")
remember(db, "s", "k1", "plain note", source_refs=SRC, idempotency_key="a4-b2")
db.execute("UPDATE memories SET valid_until=? WHERE key='k1'", ("not-a-timestamp",))
db.commit()
rep, recs, evs = _export_rep(db, "a4-benign")
check("A4-benign: benign keys stay visible (ci.api_key, k1)",
      [e["key"] for e in rep["records_excluded_secrets"]] == ["ci.api_key"]
      and {"key": "k1", "fields": ["valid_until"]} in rep["stripped_illegal_timestamps"])
db.close()

db = _db("a4-import-q")
pkg = _mk_package([_rec("r1", enc_b64, PAYLOAD, parent_ids=["missing"])])
plan = import_package(db, pkg, target_scope="s")
check("A4-import-quarantined: entry redacts the Base64 key",
      len(plan["quarantined"]) == 1
      and "key" not in plan["quarantined"][0]
      and plan["quarantined"][0].get("key_redacted") is True
      and enc_b64 not in _json(plan) and PAYLOAD not in _json(plan))
db.close()

db = _db("a4-cli")
remember(db, "s", PAYLOAD, PAYLOAD, source_refs=SRC, idempotency_key="a4-cli")
db.close()
env = dict(os.environ)
env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
proc = subprocess.run([sys.executable, "-m", "turnkeep", "--db", str(TMPROOT / "a4-cli.sqlite3"),
                       "export", "--scope", "s", "--out", str(TMPROOT / "exp-a4-cli")],
                      capture_output=True, text=True, env=env,
                      cwd=str(Path(__file__).resolve().parents[1]))
check("A4-cli: CLI stdout carries no raw payload and no Base64 of it",
      proc.returncode == 0 and PAYLOAD not in proc.stdout and enc_b64 not in proc.stdout,
      f"rc={proc.returncode}")

db = _db("a4-sweep")
rec = remember(db, "s", encs["urlsafe"], PAYLOAD, source_refs=SRC, idempotency_key="a4-sw")
erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep, recs, evs = _export_rep(db, "a4-sweep")
blob = _json(rep)
check("A4-sweep: no encoding of the payload anywhere in the report",
      all(form not in blob for form in _encoded_forms(PAYLOAD)))
db.close()

# ---------------------------------------------------------------- D: runs.task retained
print("\n== D: runs.task encoding channel (retained, §8) ==")
db = _db("d-runs")
rec = remember(db, "s", "k1", PAYLOAD, source_refs=SRC, idempotency_key="d-1")
begin(db, "s", f"revoke {encs['b64']} now", idempotency_key="d-run")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
task = db.execute("SELECT task FROM runs WHERE scope_id='s'").fetchone()[0]
rep2, recs, evs = _export_rep(db, "d-runs")
check("D: erase COMPLETE, residual clean, encoding stays in live runs.task",
      out["status"] == "COMPLETE"
      and rep["controlled"]["live_tables_clean"] is True
      and encs["b64"] in task)
check("D: encoding NOT in the export (runs are not exported)",
      encs["b64"] not in recs and encs["b64"] not in evs)
db.close()

# ---------------------------------------------------------------- E: "202" / "2026"
print('\n== E: "202" / "2026" still COMPLETE (P2-R8-03 class stays fixed) ==')
for val, expiry in (("202", LEGAL_TS), ("2026", "2027-01-01T00:00:00+00:00")):
    db = _db(f"e-{val}")
    rec = remember(db, "s", "k1", val, source_refs=SRC, idempotency_key=f"e-{val}",
                   record_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    set_expiry(db, "s", rec["id"], expiry, rec["revision"])
    begin(db, "s", task="check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check(f"E-{val}: erase COMPLETE (pinned id)", out["status"] == "COMPLETE",
          f"status={out['status']}")
    db.close()
fails = 0
for i in range(25):
    db = _db(f"e-loop-{i}")
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key=f"e-loop-{i}")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    begin(db, "s", task="check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    if out["status"] != "COMPLETE":
        fails += 1
    db.close()
check("E-loop: 25x random-id '202' erases COMPLETE", fails == 0, f"fails={fails}")
db = _db("e-one-char")
rec = remember(db, "s", "k", "x", source_refs=SRC, idempotency_key="e-1char")
out = erase_record(db, "s", rec["id"], rec["revision"], "repair")
check("E-1char: key 'k' rot13-decodes to payload 'x' by coincidence — erase still COMPLETE (length floor)",
      out["status"] == "COMPLETE", f"status={out['status']}")
db.close()

# ---------------------------------------------------------------- F: R8 spot checks
print("\n== F: R8 fixes still hold in their original scope ==")
db = _db("f-a1")
rec = remember(db, "s", PAYLOAD, PAYLOAD, source_refs=SRC, idempotency_key="f-a1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("F-a1: key == payload erases PARTIAL (R8 verbatim control)",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()
db = _db("f-a2")
pkg = _mk_package([_rec("r1", encs["b64"], {"api_key": PAYLOAD})])
try:
    import_package(db, pkg, target_scope="s")
    check("F-a2: import rejects Base64 key == payload", False, "imported")
except TurnkeepError as e:
    check("F-a2: import rejects Base64 key == payload", e.code == "INVALID_INPUT", e.code)
db.close()

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
