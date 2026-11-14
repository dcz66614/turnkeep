"""T004 seed parity: copied starter core behaves identically to the kit
reference core on the contract's key semantics. pytest, stdlib + src only.
Store API (from starter/turnkeep/store.py):
  Store(path, actor=...), remember(scope, key, value, *, source_refs=[{id,type}], kind=..., parent_ids=...)
  correct(scope, old_id, expected_revision, new_value, *, source_refs=[...])
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from turnkeep.store import Store, TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def test_remember_conflict(tmp_path):
    s = Store(str(tmp_path / "t.db"), actor="tester")
    s.remember("p", "k", "a", kind="fact", source_refs=SRC)
    r = s.remember("p", "k", "b", kind="fact", source_refs=SRC)
    assert r["state"] == "conflicted"
    assert r["value"] == "b"


def test_correct_cas_wrong_revision_fails(tmp_path):
    s = Store(str(tmp_path / "t.db"), actor="tester")
    rec = s.remember("p", "k", "a", kind="fact", source_refs=SRC)
    try:
        s.correct("p", rec["id"], 99, "b", source_refs=SRC)
    except TurnkeepError as e:
        assert e.code in ("VERSION_CONFLICT", "VERSION_REQUIRED", "INVALID_INPUT")
    else:
        raise AssertionError("wrong revision should fail")
    ok = s.correct("p", rec["id"], 1, "b", source_refs=SRC)
    assert ok["value"] == "b"


def test_stale_finish_rejected_and_replay(tmp_path):
    s = Store(str(tmp_path / "t.db"), actor="tester")
    req = s.remember("p", "build.python_version", "3.11", kind="requirement", source_refs=SRC)
    run = s.begin("p", "do work")
    s.correct("p", req["id"], 1, "3.12", source_refs=SRC)
    try:
        s.finish("p", run["run_id"], {"release": "3.11"})
    except TurnkeepError as e:
        assert e.code == "STALE_SNAPSHOT"
    else:
        raise AssertionError("stale finish should be rejected")


def test_demo_replay_via_cli(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    r = subprocess.run([sys.executable, "-m", "turnkeep", "demo"], cwd=str(tmp_path),
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["demonstration"] == "SCRIPTED_SIMULATION_NO_MODEL_CALLS"
    assert d["idempotent_replay_identical"] is True
