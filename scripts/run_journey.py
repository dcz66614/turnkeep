#!/usr/bin/env python3
"""Shared journey runner (T019). Runs a named journey through a named mode.

  python scripts/run_journey.py --mode cli --journey J01 --no-model
  python scripts/run_journey.py --mode sdk --journey J01 --no-model   (T020+)
  python scripts/run_journey.py --mode ui  --journey J03 --no-model   (T031+, NOT_RUN until then)

J01 (CLI): init -> remember requirement -> begin -> correct requirement ->
stale finish rejected (STALE_SNAPSHOT with changed_keys) -> resume ->
recompute -> finish -> identical replay returns the receipt.
J05 (CLI): export a scope (with a corrected requirement, a decision that
depends on it, and one secret-like record that must be excluded) -> import
into a fresh DB -> re-export; the two exports must match after id/timestamp
normalisation; a second import of the same package is a no-op.
J03 (ui-api): the same change-and-recovery story driven through the local
HTTP workbench API (T027) with a real `turnkeep serve` subprocess: remember
-> begin -> correct -> stale finish rejected -> resume diff -> recompute
-> finish -> identical replay returns the receipt; impact paths checked
via GET /api/v1/impact.
No model calls in any mode (--no-model is required; a real model mode does
not exist in this runner). Exit 0 = journey passed, 1 = failed, 2 = NOT_RUN
(mode/journey not implemented yet — never a fake pass).
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(dbpath, *args, expect_fail=False):
    cmd = [sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json", *args]
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if expect_fail:
        assert r.returncode != 0, (cmd, r.stdout, r.stderr)
        return json.loads(r.stderr)
    assert r.returncode == 0, (cmd, r.stdout, r.stderr)
    return json.loads(r.stdout)


def journey_j01_cli(tmp):
    dbpath = Path(tmp) / "j01.sqlite3"
    steps = []
    _run_cli(dbpath, "init", "--scope", "j01")
    req = _run_cli(dbpath, "remember", "--scope", "j01", "--key", "build.python_version",
                   "--value", "3.11", "--kind", "requirement", "--source", "u:1:user_statement")
    steps.append(f"remember {req['id']}")
    run = _run_cli(dbpath, "begin", "--scope", "j01", "--task", "prepare release")
    _run_cli(dbpath, "correct", "--scope", "j01", "--id", req["id"],
             "--expected-revision", "1", "--value", "3.12")
    err = _run_cli(dbpath, "finish", "--scope", "j01", "--run", run["run_id"],
                   "--key", "release", "--value", "Python 3.11", expect_fail=True)
    assert err["error"]["code"] == "STALE_SNAPSHOT", err
    assert "build.python_version" in err["error"]["details"].get("changed_keys", []), err
    steps.append("stale finish rejected with changed_keys")
    diff = _run_cli(dbpath, "resume", "--scope", "j01", "--run", run["run_id"])
    assert diff["changed_keys"] == ["build.python_version"]
    run2 = _run_cli(dbpath, "begin", "--scope", "j01", "--task", "recompute release")
    receipt = _run_cli(dbpath, "finish", "--scope", "j01", "--run", run2["run_id"],
                       "--key", "release", "--value", "Python 3.12",
                       "--idempotency-key", "j01-key-1")
    replay = _run_cli(dbpath, "finish", "--scope", "j01", "--run", run2["run_id"],
                      "--key", "release", "--value", "Python 3.12",
                      "--idempotency-key", "j01-key-1")
    assert replay == receipt
    steps.append("recomputed finish committed; identical replay returned receipt")
    return {"journey": "J01", "mode": "cli", "status": "PASS", "steps": steps,
            "receipt": receipt}


def _http(base, token, method, path, body=None, expect_status=200):
    import urllib.request, urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == expect_status, (path, r.status)
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        assert e.code == expect_status, (path, e.code, e.read().decode())
        return json.loads(e.read().decode())


def journey_j03_ui_api(tmp):
    import os
    dbpath = Path(tmp) / "j03.sqlite3"
    steps = []
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath), "serve",
         "--scope", "j03", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        info = json.loads(proc.stdout.readline())
        base, token = info["url"], info["token"]
        _http(base, token, "POST", "/api/v1/remember",
              {"key": "build.python_version", "value": "3.11", "kind": "requirement"})
        steps.append("remember 3.11")
        req = _http(base, token, "GET", "/api/v1/recall?query=build.python_version")
        rid = req["records"][0]["id"]
        _http(base, token, "POST", "/api/v1/remember",
              {"key": "release", "value": "Python 3.11", "parents": [rid]})
        run = _http(base, token, "POST", "/api/v1/begin", {"task": "prepare release"})
        _http(base, token, "POST", "/api/v1/correct",
              {"record_id": rid, "expected_revision": 1, "new_value": "3.12"})
        err = _http(base, token, "POST", "/api/v1/finish",
                    {"run_id": run["run_id"], "key": "release", "value": "Python 3.11"},
                    expect_status=409)
        assert err["error"]["code"] == "STALE_SNAPSHOT", err
        assert "build.python_version" in err["error"]["details"]["changed_keys"], err
        steps.append("stale finish rejected with changed_keys")
        imp = _http(base, token, "GET", f"/api/v1/impact?record_id={rid}")
        assert len(imp["paths"]) == 1 and imp["paths"][0]["key"] == "release", imp
        steps.append("impact path req -> release")
        diff = _http(base, token, "POST", "/api/v1/resume", {"run_id": run["run_id"]})
        # The requirement changed AND its child (release) is tainted by the
        # AND-parent rule, so both keys appear — correct semantics.
        assert "build.python_version" in diff["changed_keys"], diff
        assert any(c["key"] == "build.python_version" and
                   c["from_state"] == "active" and c["to_state"] == "superseded"
                   for c in diff["changed"]), diff
        steps.append("resume diff shows the change")
        run2 = _http(base, token, "POST", "/api/v1/begin", {"task": "recompute release"})
        receipt = _http(base, token, "POST", "/api/v1/finish",
                        {"run_id": run2["run_id"], "key": "release", "value": "Python 3.12",
                         "idempotency_key": "j03-key-1"})
        replay = _http(base, token, "POST", "/api/v1/finish",
                       {"run_id": run2["run_id"], "key": "release", "value": "Python 3.12",
                        "idempotency_key": "j03-key-1"})
        assert replay == receipt
        steps.append("recomputed finish committed; identical replay returned receipt")
        return {"journey": "J03", "mode": "ui-api", "status": "PASS", "steps": steps,
                "receipt": receipt}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _start_serve(tmp, scope, principal, port=0):
    """Start a real `turnkeep serve` subprocess; return (proc, base, token)."""
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "turnkeep", "--db", str(Path(tmp) / "j02.sqlite3"),
         "serve", "--scope", scope, "--principal", principal, "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    info = json.loads(proc.stdout.readline())
    return proc, info["url"], info["token"]


def journey_j02_ui_api(tmp):
    """Two API sessions (two principals, one shared DB) hand a task over:
    A remembers + finishes + writes a brief; B reads the brief, resumes
    A's run, recomputes and finishes. Then both servers stop, one restarts,
    and the runs/records are still there (recovery is in the DB, not in
    browser localStorage). No model calls."""
    steps = []
    # A: first client
    proc_a, base_a, tok_a = _start_serve(tmp, "demo", "handoff-a")
    try:
        _http(base_a, tok_a, "POST", "/api/v1/remember",
              {"key": "export.format", "value": "zip", "kind": "decision"})
        steps.append("A remembers the export-format decision")
        req = _http(base_a, tok_a, "GET", "/api/v1/recall?query=export.format")
        rid = req["records"][0]["id"]
        run_a = _http(base_a, tok_a, "POST", "/api/v1/begin", {"task": "produce export manifest"})
        receipt_a = _http(base_a, tok_a, "POST", "/api/v1/finish",
                          {"run_id": run_a["run_id"], "key": "export.manifest",
                           "value": "manifest v1", "idempotency_key": "j02-a-1"})
        brief = _http(base_a, tok_a, "GET", "/api/v1/brief?goal=produce+export+manifest")
        assert any(d["key"] == "export.format" for d in brief["valid_decisions"]), brief
        steps.append("A finishes and writes a brief naming the decision")
    finally:
        proc_a.terminate(); proc_a.wait(timeout=10)
    # B: second client, fresh session, nothing copied by hand
    proc_b, base_b, tok_b = _start_serve(tmp, "demo", "handoff-b")
    try:
        brief_b = _http(base_b, tok_b, "GET", "/api/v1/brief?goal=produce+export+manifest")
        assert brief_b["valid_decisions"] == brief["valid_decisions"], brief_b
        steps.append("B reads the same brief (same decisions, same record ids)")
        hits = _http(base_b, tok_b, "GET", "/api/v1/recall?query=export.format")
        assert hits["records"][0]["value"] == "zip"
        diff = _http(base_b, tok_b, "POST", "/api/v1/resume", {"run_id": run_a["run_id"]})
        assert diff["old_result_still_valid"] is False
        run_b = _http(base_b, tok_b, "POST", "/api/v1/begin", {"task": "verify export manifest"})
        receipt_b = _http(base_b, tok_b, "POST", "/api/v1/finish",
                          {"run_id": run_b["run_id"], "key": "export.verified",
                           "value": "verified manifest v1", "idempotency_key": "j02-b-1"})
        assert rid in receipt_b["parent_ids"], receipt_b
        steps.append("B resumes A's run and finishes a verified result")
    finally:
        proc_b.terminate(); proc_b.wait(timeout=10)
    # Restart: runs and records survive in the DB, not in localStorage.
    proc_c, base_c, tok_c = _start_serve(tmp, "demo", "handoff-c")
    try:
        runs = _http(base_c, tok_c, "GET", "/api/v1/runs")
        ids = {r["id"] for r in runs["runs"]}
        assert run_a["run_id"] in ids and run_b["run_id"] in ids, runs
        hits = _http(base_c, tok_c, "GET", "/api/v1/recall?query=export.verified")
        assert hits["records"][0]["value"] == "verified manifest v1"
        steps.append("after restart: runs and records recovered from the DB")
    finally:
        proc_c.terminate(); proc_c.wait(timeout=10)
    return {"journey": "J02", "mode": "ui-api", "status": "PASS", "steps": steps,
            "receipt": receipt_b}


def journey_j05_cli(tmp):
    """J05 (CLI): export -> import into a fresh DB -> re-export; the two
    exports must be equivalent after normalising ids and timestamps.
    Also: second import of the same package is a no-op (already_imported),
    and a secret-like value is excluded from the package."""
    import hashlib
    import json as _json

    def _norm(pkgdir):
        recs = [_json.loads(l) for l in (Path(pkgdir) / "records.jsonl").read_text().splitlines() if l.strip()]
        by_id = {r["id"]: r for r in recs}
        out = {}
        for r in recs:
            parents = sorted(by_id[pid]["key"] for pid in r["parent_ids"])
            out[r["key"] + "|" + r["state"]] = (r["value"], r["kind"], r["revision"],
                                                parents, sorted(s["type"] for s in r["source_refs"]))
        return out

    steps = []
    db1 = Path(tmp) / "j05a.sqlite3"
    _run_cli(db1, "init", "--scope", "j05")
    req = _run_cli(db1, "remember", "--scope", "j05", "--key", "export.format",
                   "--value", "zip", "--kind", "requirement", "--source", "u:1:user_statement")
    dec = _run_cli(db1, "remember", "--scope", "j05", "--key", "release.manifest",
                   "--value", "name=export", "--kind", "decision", "--source", "u:2:user_statement",
                   "--parent", req["id"])
    _run_cli(db1, "correct", "--scope", "j05", "--id", req["id"],
             "--expected-revision", "1", "--value", "tar")
    _run_cli(db1, "remember", "--scope", "j05", "--key", "ci.api_key",
             "--value", "api_key=sk-test-123", "--kind", "fact")
    pkg = Path(tmp) / "pkg"
    exp = _run_cli(db1, "export", "--scope", "j05", "--out", str(pkg))
    assert exp["records_exported"] == 3, exp
    assert len(exp["records_excluded_secrets"]) == 1, exp
    assert exp["records_excluded_secrets"][0]["key"] == "ci.api_key", exp
    steps.append("export excluded the secret-like record by key only")
    db2 = Path(tmp) / "j05b.sqlite3"
    _run_cli(db2, "init", "--scope", "j05")
    imp = _run_cli(db2, "import", "--package", str(pkg))
    assert imp["imported"] == 3 and not imp["already_imported"], imp
    assert imp["quarantined"] == [], imp
    steps.append("import wrote 3 records, none quarantined")
    imp2 = _run_cli(db2, "import", "--package", str(pkg))
    assert imp2["already_imported"] and imp2["imported"] == 0, imp2
    steps.append("second import of the same package is a no-op (already_imported)")
    pkg2 = Path(tmp) / "pkg2"
    _run_cli(db2, "export", "--scope", "j05", "--out", str(pkg2))
    assert _norm(pkg) == _norm(pkg2), (_norm(pkg), _norm(pkg2))
    steps.append("export -> import -> export is equivalent after normalisation")
    return {"journey": "J05", "mode": "cli", "status": "PASS", "steps": steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--journey", required=True)
    ap.add_argument("--no-model", action="store_true")
    args = ap.parse_args()
    if not args.no_model:
        print("NOT_RUN: --no-model is required; no real-model mode exists in this runner")
        return 2
    if args.mode == "cli" and args.journey == "J01":
        with tempfile.TemporaryDirectory(prefix="turnkeep-j01-") as tmp:
            result = journey_j01_cli(tmp)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.mode == "ui-api" and args.journey == "J02":
        with tempfile.TemporaryDirectory(prefix="turnkeep-j02-") as tmp:
            result = journey_j02_ui_api(tmp)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.mode == "cli" and args.journey == "J05":
        with tempfile.TemporaryDirectory(prefix="turnkeep-j05-") as tmp:
            result = journey_j05_cli(tmp)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.mode == "ui-api" and args.journey == "J03":
        with tempfile.TemporaryDirectory(prefix="turnkeep-j03-") as tmp:
            result = journey_j03_ui_api(tmp)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"NOT_RUN: journey {args.journey} in mode {args.mode} is not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
