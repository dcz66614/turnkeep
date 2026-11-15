"""T040 baseline parity: same task, same budget, same seed for every arm;
failures stay in the denominator; the Markdown arm is not crippled."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_smoke(out):
    r = subprocess.run([sys.executable, "benchmarks/harness/run.py", "--suite", "smoke",
                        "--mode", "deterministic", "--output", str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads((out / "summary.json").read_text(encoding="utf-8"))


def test_all_arms_share_task_budget_seed(tmp_path):
    out = tmp_path / "res"
    summary = _run_smoke(out)
    assert summary["mode"] == "deterministic"
    assert summary["label"] == "SCRIPTED_SIMULATION_NO_MODEL_CALLS"
    assert summary["seed"] == 20261108
    assert summary["budget"] == {"max_steps": 20, "max_seconds": 60}
    attempts = [json.loads(l) for l in (out / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(attempts) == 12  # 3 tasks x 4 arms
    for a in attempts:
        assert a["seed"] == 20261108 and a["mode"] == "deterministic"
    assert summary["overall"]["attempts"] == 12
    # The no-memory arm must fail the change tasks (it cannot see them);
    # failures are results, counted in the denominator.
    assert summary["per_arm"]["none"]["completed"] == 1
    assert summary["per_arm"]["none"]["stale_fact_rate"] > 0


def test_markdown_arm_keeps_search_and_update(tmp_path):
    out = tmp_path / "res"
    _run_smoke(out)
    attempts = [json.loads(l) for l in (out / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    md = [a for a in attempts if a["arm"] == "markdown"]
    assert all(any(s["action"] == "grep" for s in a["steps"]) for a in md)
    assert all(any(s["action"] == "change_applied" for s in a["steps"]) for a in md)


def test_missing_arms_are_not_run_as_failures(tmp_path):
    out = tmp_path / "res"
    summary = _run_smoke(out)
    assert "mature-memory" in summary["not_run_arms"]
    assert "nearest-workflow" in summary["not_run_arms"]
    assert "mature-memory" not in summary["per_arm"]
