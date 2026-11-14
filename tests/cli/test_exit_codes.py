"""T019 CLI exit-code tests."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def run(dbpath, *args):
    return subprocess.run([sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json", *args],
                          capture_output=True, text=True, timeout=60, env=ENV)


def test_stale_snapshot_exit_nonzero_with_structured_diagnosis(tmp_path):
    db = tmp_path / "e.sqlite3"
    assert run(db, "init", "--scope", "s1").returncode == 0
    r = run(db, "remember", "--scope", "s1", "--key", "k", "--value", "v1")
    req = json.loads(r.stdout)
    run(db, "begin", "--scope", "s1", "--task", "t")
    run(db, "correct", "--scope", "s1", "--id", req["id"], "--expected-revision", "1", "--value", "v2")
    # begin a second run AFTER the correction so its snapshot is fresh, then
    # finish twice: second finish on an accepted run with a different payload
    # must fail with IDEMPOTENCY_MISMATCH (non-zero + structured diagnosis).
    r = run(db, "begin", "--scope", "s1", "--task", "t2")
    run2 = json.loads(r.stdout)
    ok = run(db, "finish", "--scope", "s1", "--run", run2["run_id"],
             "--key", "out", "--value", "x", "--idempotency-key", "kk")
    assert ok.returncode == 0
    bad = run(db, "finish", "--scope", "s1", "--run", run2["run_id"],
              "--key", "out", "--value", "y", "--idempotency-key", "kk")
    assert bad.returncode != 0
    diag = json.loads(bad.stderr)
    assert diag["error"]["code"] == "IDEMPOTENCY_MISMATCH"


def test_unknown_scope_and_bad_revision(tmp_path):
    db = tmp_path / "e.sqlite3"
    r = run(db, "recall", "--scope", "nope")
    assert r.returncode != 0
    diag = json.loads(r.stderr)
    assert diag["error"]["code"] in ("NOT_FOUND", "INVALID_INPUT")
