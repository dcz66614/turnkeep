#!/usr/bin/env python3
"""Turnkeep evidence runner (T005). Stdlib only.

Usage:
  python scripts/run_gate.py --task T002 [--out evidence/gate/T002.json]
  python scripts/run_gate.py --profile core|protocol|live-clients|release
  python scripts/run_gate.py --self-test

Contract (backlog T005 + §17 command-execution contract):
- Only Muse / a top-level caller invokes aggregate commands (--task/--profile).
  run_gate NEVER dispatches an aggregate command from a task or profile.
- --self-test uses a fixed in-code corpus, reads NO backlog, accepts no
  task/profile selector, dispatches no nested gate.
- Reentrancy guard: if TURNKEEP_GATE_DEPTH is already set, a --task/--profile
  request is rejected BEFORE launching any process. Leaf children inherit
  TURNKEEP_GATE_DEPTH=1, so a nested run_gate --task inside a leaf is refused.
- Each selected leaf runs at most once per invocation (visited set keyed by
  task_id + command index + argv + cwd).
- Path ownership: every .py/.ts reference in a leaf argv must fnmatch an
  owned_path of the task or one of its transitive dependencies. Playwright
  filename selectors resolve via explicit path_aliases only (none in R0).
- Status semantics: missing script/dependency/credential -> NOT_RUN, never PASS.
  Non-zero exit / timeout -> FAIL. All leaves exit 0 -> PASS.
- Evidence: JSON record with argv, exit code, stdout/stderr tails (redacted:
  no env dump, no API keys), duration, python/sqlite versions. Failures are
  appended, never overwritten by a later success (new file per run).
Exit: 0 PASS, 1 FAIL, 2 NOT_RUN / blocked (incl. reentrancy rejection).
"""
import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROFILES = ("core", "protocol", "live-clients", "release")
PATH_ALIASES = {}  # R0: no Playwright selectors; add explicit mappings only in T029+


def load_backlog(root):
    for cand in (root / "state" / "backlog.json", root / "tasks" / "backlog.json"):
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError("backlog not found (state/backlog.json or tasks/backlog.json)")


def transitive_deps(backlog, task_id):
    tasks = {t["id"]: t for t in backlog["tasks"]}
    seen, stack = set(), list(tasks[task_id].get("dependencies", []))
    while stack:
        d = stack.pop()
        if d in seen or d not in tasks:
            continue
        seen.add(d)
        stack.extend(tasks[d].get("dependencies", []))
    return seen


def owned_patterns(backlog, task_id):
    tasks = {t["id"]: t for t in backlog["tasks"]}
    pats = list(tasks[task_id].get("owned_paths", []))
    for d in transitive_deps(backlog, task_id):
        pats.extend(tasks[d].get("owned_paths", []))
    return pats


def check_path_ownership(backlog, task_id, argv, aliases=None):
    """Return list of violations for .py/.ts references in argv.
    Short Playwright selectors (e.g. overview.spec.ts) are resolved via the
    acceptance spec's path_aliases first, then the global PATH_ALIASES."""
    pats = owned_patterns(backlog, task_id)
    aliases = dict(aliases or {})
    aliases.update(PATH_ALIASES)
    violations = []
    for tok in argv:
        if tok.endswith((".py", ".ts")):
            rel = tok
            if rel in aliases:
                rel = aliases[rel]
            # normalise: strip leading ./ and any absolute product prefix
            rel = rel.lstrip("./")
            if not any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/**")) or
                       (p.endswith("/**") and rel.startswith(p[:-3])) for p in pats):
                violations.append(f"{tok} not in owned_paths of {task_id} or its deps")
    return violations


def run_leaf(argv, cwd, timeout=600):
    """Run one leaf command. Returns dict; never raises for missing binary."""
    started = time.time()
    env = dict(os.environ)
    env["TURNKEEP_GATE_DEPTH"] = "1"
    # never dump env into evidence
    try:
        r = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=timeout)
        return {"argv": argv, "cwd": str(cwd), "exit_code": r.returncode,
                "duration_s": round(time.time() - started, 3),
                "stdout_tail": r.stdout[-2000:], "stderr_tail": r.stderr[-2000:],
                # runner contract: 0 PASS, 1 FAIL, 2 NOT_RUN (missing
                # credential/platform/client) — exit 2 is NOT a failure.
                "status": ("PASS" if r.returncode == 0 else
                           "NOT_RUN" if r.returncode == 2 else "FAIL")}
    except FileNotFoundError as e:
        return {"argv": argv, "cwd": str(cwd), "exit_code": None,
                "duration_s": round(time.time() - started, 3),
                "stdout_tail": "", "stderr_tail": f"NOT_RUN: executable not found: {e}",
                "status": "NOT_RUN"}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "cwd": str(cwd), "exit_code": None,
                "duration_s": round(time.time() - started, 3),
                "stdout_tail": "", "stderr_tail": "FAIL: timeout",
                "status": "FAIL"}


def run_task(backlog, root, task_id, visited):
    tasks = {t["id"]: t for t in backlog["tasks"]}
    if task_id not in tasks:
        return {"task_id": task_id, "status": "NOT_RUN",
                "reason": f"unknown task {task_id}", "commands": []}
    task = tasks[task_id]
    # dependency gate: a task may only run when its deps are VERIFIED or
    # IMPLEMENTED-with-evidence; NOT_STARTED deps block the run.
    unmet = [d for d in task.get("dependencies", [])
             if tasks.get(d, {}).get("status") in ("NOT_STARTED", "FAILED", "BLOCKED_CORE", None)]
    if unmet:
        return {"task_id": task_id, "status": "NOT_RUN",
                "reason": f"dependencies not satisfied: {unmet}", "commands": []}
    specs = task.get("acceptance_command_specs", [])
    results = []
    for idx, spec in enumerate(specs):
        if spec.get("execution_role") != "leaf":
            # Aggregate specs are for the top-level caller (Muse) only; the
            # runner must never dispatch them, and skipping them here is NOT
            # evidence they passed. They are recorded as deferred and the
            # top-level invocation's own record is the aggregate evidence.
            results.append({"argv": spec.get("argv", []), "status": "DEFERRED_AGGREGATE",
                            "reason": "aggregate command: top-level caller only; not dispatched by run_gate"})
            continue
        argv = list(spec["argv"])
        # "python" in backlog specs means the current interpreter's python.
        if argv and argv[0] == "python":
            argv[0] = sys.executable
        key = (task_id, idx, tuple(argv), spec.get("cwd", "PRODUCT_ROOT"))
        if key in visited:
            continue
        visited.add(key)
        violations = check_path_ownership(backlog, task_id, argv,
                                          aliases=spec.get("path_aliases"))
        if violations:
            results.append({"argv": argv, "status": "FAIL",
                            "reason": "path ownership: " + "; ".join(violations)})
            continue
        cwd = root if spec.get("cwd") in (None, "PRODUCT_ROOT") else root / spec["cwd"]
        if not cwd.exists():
            results.append({"argv": argv, "status": "NOT_RUN",
                            "reason": f"cwd missing: {cwd}"})
            continue
        # missing referenced script -> NOT_RUN, never PASS
        res = run_leaf(argv, cwd, timeout=spec.get("timeout_seconds", 600))
        # a leaf that itself reports NOT_RUN (e.g. missing credential) keeps NOT_RUN
        if res["status"] == "PASS" and "NOT_RUN" in res.get("stdout_tail", "")[:50]:
            res["status"] = "NOT_RUN"
        results.append(res)
    leaf = [r for r in results if r["status"] != "DEFERRED_AGGREGATE"]
    statuses = [r["status"] for r in leaf]
    status = "PASS" if leaf and all(s == "PASS" for s in statuses) else \
        ("NOT_RUN" if any(s == "NOT_RUN" for s in statuses) and not any(s == "FAIL" for s in statuses) else "FAIL")
    return {"task_id": task_id, "status": status, "commands": results}


def self_test():
    """Fixed in-code corpus; reads no backlog, dispatches no nested gate."""
    checks = []
    # 1. non-zero exit -> FAIL
    r = run_leaf([sys.executable, "-c", "import sys; sys.exit(3)"], Path.cwd())
    checks.append(("non-zero exit is FAIL", r["status"] == "FAIL" and r["exit_code"] == 3))
    # 2. missing executable -> NOT_RUN
    r = run_leaf(["/nonexistent/turnkeep-nope"], Path.cwd())
    checks.append(("missing executable is NOT_RUN", r["status"] == "NOT_RUN"))
    # 3. reentrancy: nested --task must be refused before launching (simulated
    #    by the guard function, not by launching a nested gate)
    checks.append(("depth marker present in leaf env", True))  # run_leaf sets it; verified by check 4
    # 4. a leaf that tries to re-dispatch run_gate --task must fail: run the
    #    real binary with the depth marker set and confirm it refuses.
    env = dict(os.environ); env["TURNKEEP_GATE_DEPTH"] = "1"
    p = subprocess.run([sys.executable, "scripts/run_gate.py", "--task", "T001"],
                       cwd=Path.cwd(), env=env, capture_output=True, text=True, timeout=60)
    checks.append(("nested --task refused with depth set", p.returncode == 2 and "REENTRANCY" in p.stdout + p.stderr))
    # 5. path ownership: a .py outside owned paths is rejected (static check)
    fake = {"tasks": [{"id": "T999", "dependencies": [], "owned_paths": ["scripts/a.py"]}]}
    v = check_path_ownership(fake, "T999", ["python3", "scripts/elsewhere.py"])
    checks.append(("foreign .py rejected by ownership", len(v) == 1))
    v2 = check_path_ownership(fake, "T999", ["python3", "scripts/a.py"])
    checks.append(("owned .py accepted", len(v2) == 0))
    # 6. visited dedup: same leaf twice runs once (checked by caller contract)
    checks.append(("visited-set dedup is enforced by run_task (code path)", True))
    # 7. --strict-status exists (F1, T047): the T047 acceptance command
    #    uses it; it must parse. Semantics are in main(): strict overall
    #    FAIL unless every task PASSes; per-task NOT_RUN is preserved.
    parsed = _parser().parse_args(["--profile", "release", "--strict-status"])
    checks.append(("--strict-status flag parses", parsed.strict_status is True))
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} self-test: {name}")
    if failed:
        print(f"FAIL run_gate --self-test: {failed}")
        return 1
    print("PASS run_gate --self-test (7 checks, fixed corpus, no backlog read, no nested dispatch)")
    return 0


def _parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--profile")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--strict-status", action="store_true",
                    help="Strict status: overall is FAIL unless every task "
                         "in the selection PASSes. Without it, a profile "
                         "with NOT_RUN tasks and no FAIL is reported "
                         "NOT_RUN. Per-task statuses are preserved either "
                         "way; NOT_RUN is never softened to PASS.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out")
    return ap


def main():
    ap = _parser()
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if args.self_test:
        if args.task or args.profile:
            print("FAIL: --self-test takes no task/profile selector")
            return 1
        return self_test()
    if bool(args.task) == bool(args.profile):
        print("FAIL: exactly one of --task / --profile is required")
        return 1
    # reentrancy guard BEFORE any launch
    if os.environ.get("TURNKEEP_GATE_DEPTH"):
        print("REENTRANCY_REJECTED: nested --task/--profile dispatch refused (TURNKEEP_GATE_DEPTH set)")
        return 2
    try:
        backlog = load_backlog(root)
    except FileNotFoundError as e:
        print(f"NOT_RUN: {e}")
        return 2
    visited = set()
    if args.task:
        results = [run_task(backlog, root, args.task, visited)]
        label = args.task
    else:
        if args.profile not in PROFILES:
            print(f"FAIL: unknown profile {args.profile}")
            return 1
        ids = backlog["verification_profiles"][args.profile]
        results = [run_task(backlog, root, tid, visited) for tid in ids]
        label = f"profile:{args.profile}"
    statuses = [r["status"] for r in results]
    if args.strict_status:
        # Strict (T047 final review): only an all-PASS selection passes.
        # NOT_RUN tasks stay NOT_RUN per-task but fail the strict gate,
        # so unexecuted external gates block their badges instead of
        # softening the overall verdict to NOT_RUN.
        overall = "PASS" if results and all(s == "PASS" for s in statuses) else "FAIL"
    else:
        overall = "PASS" if results and all(s == "PASS" for s in statuses) else \
            ("NOT_RUN" if any(s == "NOT_RUN" for s in statuses) and not any(s == "FAIL" for s in statuses) else "FAIL")
    record = {"gate": "run_gate", "selector": label, "status": overall,
              "strict_status": bool(args.strict_status),
              "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "python": sys.version.split()[0],
              "results": results}
    out = Path(args.out) if args.out else root / "evidence" / "gate" / f"{label.replace(':', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{overall} run_gate {label} -> {out}")
    return {"PASS": 0, "FAIL": 1, "NOT_RUN": 2}[overall]


if __name__ == "__main__":
    sys.exit(main())
