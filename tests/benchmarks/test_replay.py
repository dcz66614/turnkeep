"""T041 replay: 25 scenarios pass on the real core; the manifest records
failures with explanations and recovery counts; false refusals are
reported separately, not hidden."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _replay(out, mutation=None):
    cmd = [sys.executable, "benchmarks/replay.py", "--suite", "core-change",
           "--output", str(out)]
    if mutation:
        cmd += ["--mutation", mutation]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    name = "manifest-mutation.json" if mutation else "manifest.json"
    return json.loads((out / name).read_text())


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
