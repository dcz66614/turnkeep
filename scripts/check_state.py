#!/usr/bin/env python3
"""T006 state gate: CONTROL_STATE + backlog must be mutually consistent, and a
new session must be recoverable from HANDOFF + state/ + specs/ alone.
Stdlib only.
Usage: python scripts/check_state.py --state state/CONTROL_STATE.json --backlog tasks/backlog.json
  (tasks/backlog.json is a symlink to state/backlog.json in this repo — one source of truth.)
Exit 0 PASS, 1 FAIL, 2 NOT_RUN."""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--backlog", required=True)
    args = ap.parse_args()
    root = Path.cwd()
    errors, missing = [], []
    for name in (args.state, args.backlog):
        if not (root / name).exists():
            missing.append(name)
    if missing:
        print(f"NOT_RUN missing: {missing}")
        return 2
    state = json.loads((root / args.state).read_text())
    backlog = json.loads((root / args.backlog).read_text())
    tasks = {t["id"]: t for t in backlog["tasks"]}
    if len(tasks) != 48:
        errors.append(f"backlog has {len(tasks)} tasks, expected 48")
    # 1. completed_tasks in state must match VERIFIED tasks in backlog
    verified = {tid for tid, t in tasks.items() if t.get("status") == "VERIFIED"}
    if set(state.get("completed_tasks", [])) != verified:
        errors.append(f"state completed_tasks {sorted(state.get('completed_tasks', []))} != backlog VERIFIED {sorted(verified)}")
    # 2. no VERIFIED task without an evidence file
    for tid in verified:
        ev = root / "evidence" / "tasks" / f"{tid}.json"
        if not ev.exists():
            errors.append(f"{tid} VERIFIED but evidence/tasks/{tid}.json missing")
    # 3. active_tasks must be READY/RUNNING and have deps satisfied
    for tid in state.get("active_tasks", []):
        t = tasks.get(tid)
        if not t:
            errors.append(f"active task {tid} not in backlog"); continue
        if t.get("status") not in ("READY", "RUNNING", "IMPLEMENTED"):
            errors.append(f"active task {tid} status {t.get('status')} is not READY/RUNNING/IMPLEMENTED")
        for d in t.get("dependencies", []):
            if tasks[d].get("status") not in ("VERIFIED", "IMPLEMENTED"):
                errors.append(f"active task {tid} depends on {d} ({tasks[d].get('status')})")
    # 4. owned_paths must not overlap between two concurrently active tasks
    act = state.get("active_tasks", [])
    for i, a in enumerate(act):
        for b in act[i + 1:]:
            overlap = set(tasks[a].get("owned_paths", [])) & set(tasks[b].get("owned_paths", []))
            if overlap:
                errors.append(f"owned_paths overlap between {a} and {b}: {overlap}")
    # 5. recovery: HANDOFF.md + state/CURRENT_STATUS.md must exist and name the next task
    for f in ("HANDOFF.md", "state/CURRENT_STATUS.md"):
        if not (root / f).exists():
            errors.append(f"recovery file missing: {f}")
    nxt = state.get("next_action", "")
    if not nxt:
        errors.append("CONTROL_STATE.next_action is empty")
    if errors:
        print("FAIL check_state:")
        for e in errors:
            print(" -", e)
        return 1
    print(f"PASS check_state: {len(verified)} VERIFIED, {len(act)} active, next: {nxt[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
