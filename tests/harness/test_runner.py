"""T005 harness tests: run_gate behaviour with a fake executor / fake backlog.
pytest. No network, no model calls, no backlog mutation."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_gate


def fake_backlog():
    return {"tasks": [
        {"id": "T901", "dependencies": [], "owned_paths": ["scripts/a.py", "tests/x/**"],
         "status": "VERIFIED",
         "acceptance_command_specs": [
             {"argv": ["python", "scripts/a.py"], "cwd": "PRODUCT_ROOT", "execution_role": "leaf"}]},
        {"id": "T902", "dependencies": ["T901"], "owned_paths": ["scripts/b.py"],
         "status": "VERIFIED",
         "acceptance_command_specs": [
             {"argv": ["python", "scripts/b.py"], "cwd": "PRODUCT_ROOT", "execution_role": "leaf"}]},
        {"id": "T903", "dependencies": ["T999"], "owned_paths": [],
         "status": "NOT_STARTED", "acceptance_command_specs": []},
    ], "verification_profiles": {"core": ["T901", "T902"]}}


def test_nonzero_is_fail(tmp_path):
    r = run_gate.run_leaf([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path)
    assert r["status"] == "FAIL" and r["exit_code"] == 3


def test_missing_executable_is_not_run(tmp_path):
    r = run_gate.run_leaf(["/nonexistent/turnkeep-nope"], tmp_path)
    assert r["status"] == "NOT_RUN" and r["exit_code"] is None


def test_path_ownership_rejects_foreign_py():
    bl = fake_backlog()
    v = run_gate.check_path_ownership(bl, "T901", ["python3", "scripts/elsewhere.py"])
    assert len(v) == 1
    assert run_gate.check_path_ownership(bl, "T901", ["python3", "scripts/a.py"]) == []


def test_path_ownership_allows_dep_owned_paths():
    bl = fake_backlog()
    # T902 depends on T901, so T901's owned paths are allowed for T902
    assert run_gate.check_path_ownership(bl, "T902", ["python3", "scripts/a.py"]) == []


def test_unmet_dependency_blocks_run(tmp_path):
    bl = fake_backlog()
    r = run_gate.run_task(bl, tmp_path, "T903", set())
    assert r["status"] == "NOT_RUN"


def test_aggregate_role_refused(tmp_path):
    bl = fake_backlog()
    bl["tasks"][0]["acceptance_command_specs"] = [
        {"argv": ["python", "scripts/run_gate.py", "--task", "T901"],
         "cwd": "PRODUCT_ROOT", "execution_role": "aggregate"}]
    r = run_gate.run_task(bl, tmp_path, "T901", set())
    assert r["status"] == "FAIL"


def test_self_test_passes():
    assert run_gate.self_test() == 0
