#!/usr/bin/env python3
"""Evaluation runner (T040).

  python benchmarks/harness/run.py --suite smoke --mode deterministic [--output DIR]

Runs every arm in the suite on every task with the SAME task, budget and
seed, scores the raw traces deterministically, and writes attempts.jsonl +
summary.json. Failures and timeouts stay in the denominator.

--mode deterministic uses scripted policies only (NO model calls). Its
numbers are SCRIPTED_SIMULATION_NO_MODEL_CALLS — they measure the harness
and the policies, never real model performance. A live model mode does not
exist in this runner (Gate C = NOT_RUN without credentials and a budget).
Exit 0 = the run completed; 1 = the harness itself failed.
"""
import argparse
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "harness"))

from scoring import score_trace, summarize  # noqa: E402

ARMS = ("none", "markdown", "log", "turnkeep")
# Mature-memory / nearest-workflow arms (EVALUATION_PLAN B3/B4) are NOT_RUN:
# no real Mem0/Hindsight/Tigerless/Engram is installed or configured here,
# and a missing arm is never scored as a competitor failure.
NOT_RUN_ARMS = {
    "mature-memory": "NOT_RUN: no real Mem0/Hindsight install configured in this environment",
    "nearest-workflow": "NOT_RUN: no real Tigerless/Engram install configured in this environment",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True)
    ap.add_argument("--mode", required=True, choices=["deterministic"])
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    suite_path = ROOT / "benchmarks" / "suites" / f"{args.suite}.json"
    if not suite_path.exists():
        print(f"NOT_RUN: no suite {args.suite!r}", file=sys.stderr)
        return 2
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    out = Path(args.output) if args.output else ROOT / "benchmarks" / "results" / f"{args.suite}-deterministic"
    out.mkdir(parents=True, exist_ok=True)
    attempts, scores = [], []
    for task in suite["tasks"]:
        task = dict(task, seed=suite.get("seed", 0))
        for arm_name in ARMS:
            mod = importlib.import_module(f"arms.{arm_name}")
            with tempfile.TemporaryDirectory(prefix=f"tk-bench-{arm_name}-") as tmp:
                try:
                    trace = mod.run(task, tmp, suite["budget"])
                except Exception as e:  # noqa: BLE001 - a crashing arm is a failed attempt, not a pass
                    trace = {"task_id": task["id"], "arm": arm_name, "seed": task["seed"],
                             "mode": "deterministic", "steps": [], "events": [],
                             "claimed_completed": False, "usage": None,
                             "error": f"{type(e).__name__}: {e}", "timeout": False}
            attempts.append(trace)
            try:
                scores.append(score_trace(trace, task))
            except ValueError as e:
                scores.append({"task_id": task["id"], "arm": arm_name, "seed": task["seed"],
                               "mode": "deterministic", "completed": False,
                               "claimed_completed": False, "claim_overridden": False,
                               "contract_violations": 0, "stale_fact_used": False,
                               "irrelevant_refusal": False, "redo_cost_steps": 0,
                               "steps": 0, "latency_ms": "unknown", "tokens": "unknown",
                               "error": f"scoring_failed: {e}", "timeout": False})
    per_arm = {}
    for arm_name in ARMS:
        per_arm[arm_name] = summarize([s for s in scores if s["arm"] == arm_name])
    summary = {"suite": args.suite, "mode": "deterministic",
               "label": "SCRIPTED_SIMULATION_NO_MODEL_CALLS",
               "note": "Scripted policies only. These numbers measure the harness and the "
                       "policies, never real model performance. Gate C (live models) is NOT_RUN.",
               "seed": suite.get("seed"), "budget": suite["budget"],
               "not_run_arms": NOT_RUN_ARMS,
               "overall": summarize(scores), "per_arm": per_arm}
    (out / "attempts.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in attempts), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
