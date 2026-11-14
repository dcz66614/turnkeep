"""T019 CLI journey tests (J01 via subprocess, real DB file, no model)."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def cli(dbpath, *args, expect_fail=False):
    r = subprocess.run([sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json", *args],
                       capture_output=True, text=True, timeout=60, env=ENV)
    if expect_fail:
        assert r.returncode != 0
        return json.loads(r.stderr)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout)


def test_j01_full_journey(tmp_path):
    db = tmp_path / "j01.sqlite3"
    cli(db, "init", "--scope", "j01")
    req = cli(db, "remember", "--scope", "j01", "--key", "build.python_version",
              "--value", "3.11", "--kind", "requirement", "--source", "u:1:user_statement")
    run = cli(db, "begin", "--scope", "j01", "--task", "prepare release")
    cli(db, "correct", "--scope", "j01", "--id", req["id"], "--expected-revision", "1", "--value", "3.12")
    err = cli(db, "finish", "--scope", "j01", "--run", run["run_id"],
              "--key", "release", "--value", "Python 3.11", expect_fail=True)
    assert err["error"]["code"] == "STALE_SNAPSHOT"
    diff = cli(db, "resume", "--scope", "j01", "--run", run["run_id"])
    assert diff["changed_keys"] == ["build.python_version"]
    run2 = cli(db, "begin", "--scope", "j01", "--task", "recompute")
    receipt = cli(db, "finish", "--scope", "j01", "--run", run2["run_id"],
                  "--key", "release", "--value", "Python 3.12", "--idempotency-key", "k1")
    assert receipt["guarantee"] == "local_sqlite_commit_only"


def test_demo_is_labelled_scripted(tmp_path):
    r = subprocess.run([sys.executable, "-m", "turnkeep", "--db", str(tmp_path / "d.sqlite3"),
                        "--json", "demo"], capture_output=True, text=True, timeout=60, env=ENV)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["demonstration"] == "SCRIPTED_SIMULATION_NO_MODEL_CALLS"
    assert d["idempotent_replay_identical"] is True


def test_values_with_spaces_chinese_quotes(tmp_path):
    db = tmp_path / "v.sqlite3"
    cli(db, "init", "--scope", "s1")
    r = cli(db, "remember", "--scope", "s1", "--key", "note",
            "--value", '你好 world "quoted" \'single\' $HOME `x`')
    assert r["value"] == '你好 world "quoted" \'single\' $HOME `x`'


def test_remember_path_like_source_rejected(tmp_path):
    # P1-d: a --source value that looks like a filesystem path must fail
    # with exit != 0 before anything is stored (Unix and Windows forms).
    db = tmp_path / "p.sqlite3"
    cli(db, "init", "--scope", "s1")
    err = cli(db, "remember", "--scope", "s1", "--key", "k", "--value", "v",
              "--source", "/home/alice/notes.md:user_statement", expect_fail=True)
    assert err["error"]["code"] == "INVALID_INPUT"
    err = cli(db, "remember", "--scope", "s1", "--key", "k2", "--value", "v",
              "--source", "C:\\Users\\alice\\notes.md:user_statement", expect_fail=True)
    assert err["error"]["code"] == "INVALID_INPUT"
    hits = cli(db, "recall", "--scope", "s1", "--query", "v")
    assert hits["records"] == []
