"""T006 state-recovery tests: a new session recovers from HANDOFF + state/ +
specs/ alone. pytest, stdlib only.

R2 (D15) portability: the release zip deliberately excludes state/ (and
evidence/ raw files), so these tests must SKIP — not fail — when run
from an extracted zip, where the tree under test has no state/ to
recover from. In the dev checkout (state/ present) they still run
and assert exactly as before.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _require_state():
    """Skip cleanly when this tree has no state/ (extracted release zip)."""
    if not (ROOT / "state" / "CONTROL_STATE.json").is_file():
        pytest.skip("state/ not present in this tree (e.g. extracted release zip)")
    if not (ROOT / "state" / "backlog.json").is_file():
        pytest.skip("state/backlog.json not present in this tree (e.g. extracted release zip)")


def _require_evidence_tasks():
    if not (ROOT / "evidence" / "tasks").is_dir():
        pytest.skip("evidence/tasks/ not present in this tree (e.g. extracted release zip)")


def test_control_state_matches_backlog():
    _require_state()
    state = json.loads((ROOT / "state/CONTROL_STATE.json").read_text())
    backlog = json.loads((ROOT / "state/backlog.json").read_text())
    verified = {t["id"] for t in backlog["tasks"] if t["status"] == "VERIFIED"}
    assert set(state["completed_tasks"]) == verified


def test_verified_tasks_have_evidence():
    _require_state()
    _require_evidence_tasks()
    backlog = json.loads((ROOT / "state/backlog.json").read_text())
    for t in backlog["tasks"]:
        if t["status"] == "VERIFIED":
            assert (ROOT / "evidence/tasks" / f"{t['id']}.json").exists()


def test_handoff_names_next_task():
    _require_state()
    hand = (ROOT / "HANDOFF.md").read_text()
    state = json.loads((ROOT / "state/CONTROL_STATE.json").read_text())
    assert state["next_action"]
    # HANDOFF must contain the next task id mentioned in CONTROL_STATE
    assert "T007" in hand or "R1" in hand


def test_no_task_ready_before_deps():
    _require_state()
    backlog = json.loads((ROOT / "state/backlog.json").read_text())
    tasks = {t["id"]: t for t in backlog["tasks"]}
    for t in backlog["tasks"]:
        if t["status"] in ("READY", "RUNNING"):
            for d in t["dependencies"]:
                assert tasks[d]["status"] in ("VERIFIED", "IMPLEMENTED"), \
                    f"{t['id']} READY but dep {d} is {tasks[d]['status']}"


def test_backlog_symlink_single_source():
    _require_state()
    # tasks/backlog.json must resolve to the same content as state/backlog.json
    a = (ROOT / "tasks/backlog.json").read_text()
    b = (ROOT / "state/backlog.json").read_text()
    assert a == b
