"""T041 replay: 25 scenarios pass on the real core; the manifest records
failures with explanations and recovery counts; false refusals are
reported separately, not hidden."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _replay(out, mutation=None):
    cmd = [sys.executable, "benchmarks/replay.py", "--suite", "core-change",
           "--output", str(out)]
    if mutation:
        cmd += ["--mutation", mutation]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, encoding="utf-8",
                       errors="replace")
    assert r.returncode == 0, r.stderr
    name = "manifest-mutation.json" if mutation else "manifest.json"
    return json.loads((out / name).read_text(encoding="utf-8"))


def test_all_scenarios_pass_on_real_core(tmp_path):
    m = _replay(tmp_path / "replay")
    assert m["scenarios"] == 25 and m["passed"] == 25 and m["failed"] == 0
    assert m["label"] == "SCRIPTED_REPLAY_NO_MODEL_CALLS"
    assert m["false_refusals"] == 2  # S05 + S18 demonstrate the conservative false refusal
    assert all(r["status"] == "PASS" for r in m["results"])
    # Every failure-free run still records recovery counts.
    assert sum(r["recoveries"] for r in m["results"]) >= 4


def test_false_refusals_are_reported_not_hidden(tmp_path):
    m = _replay(tmp_path / "replay")
    flagged = [r for r in m["results"] if r["false_refusal"]]
    assert len(flagged) == 2
    assert all(r["status"] == "PASS" for r in flagged)


def test_manifest_keeps_raw_results(tmp_path):
    m = _replay(tmp_path / "replay")
    for r in m["results"]:
        assert "failures" in r and "recoveries" in r and "events" in r


def test_replay_closes_all_db_connections():
    """Regression: _export_import swaps engine.db onto a fresh DB, and
    the old code never closed it (the local `db` still pointed at the
    already-closed original), so TemporaryDirectory cleanup failed on
    Windows (WinError 32, open sqlite file). Track every connection
    the replay opens and assert all are closed at return. The scenario
    also restarts after the export_import, so the restart branch must
    close the live (fresh) connection too, not just the stale local."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tk_replay", ROOT / "benchmarks" / "replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    opened = []
    real_open_db = mod.open_db

    def _tracking_open_db(path, *args, **kwargs):
        conn = real_open_db(path, *args, **kwargs)
        opened.append(conn)
        return conn

    mod.open_db = _tracking_open_db
    scenario = {
        "id": "leak-probe", "title": "leak probe", "family": "test",
        "initial": [{"key": "a", "value": "1"}],
        "events": [{"op": "export_import"}, {"op": "restart"},
                   {"op": "remember", "key": "b", "value": "2"}],
        "observations": [],
    }
    result = mod.replay_scenario(scenario)
    assert result["status"] == "PASS", result
    assert opened, "probe tracked no connections"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
