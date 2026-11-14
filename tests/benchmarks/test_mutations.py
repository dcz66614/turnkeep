"""T041 mutations: a deliberately broken engine must FAIL the replay.
If the mutations passed, the replay could not distinguish a working
implementation from a broken one — and the scorer would be worthless."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _replay_mutation(out, mutation):
    r = subprocess.run([sys.executable, "benchmarks/replay.py", "--suite", "core-change",
                        "--output", str(out), "--mutation", mutation],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return json.loads((out / "manifest-mutation.json").read_text())


def test_no_taint_mutation_fails_taint_scenarios(tmp_path):
    m = _replay_mutation(tmp_path / "mut", "no-taint")
    failed = {r["id"] for r in m["results"] if r["status"] == "FAIL"}
    assert m["label"].startswith("MUTATION RUN")
    assert {"S02-post-plan-retract", "S09-indirect-taint-chain",
            "S10-indirect-taint-diamond", "S11-shared-subgraph-taint"} <= failed


def test_silent_overwrite_mutation_fails_concurrency_scenarios(tmp_path):
    m = _replay_mutation(tmp_path / "mut", "silent-overwrite")
    failed = {r["id"] for r in m["results"] if r["status"] == "FAIL"}
    assert {"S06-concurrent-correct-one-wins", "S07-correct-after-retract-refused"} <= failed


def test_mutation_runs_do_not_count_as_product_evidence(tmp_path):
    m = _replay_mutation(tmp_path / "mut", "no-taint")
    assert m["mutation"] == "no-taint"
    assert "not product evidence" in m["label"]
