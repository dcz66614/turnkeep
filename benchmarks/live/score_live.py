"""Deterministic scorer for LIVE traces (T042).

Same rules as benchmarks/harness/scoring.py, but reads live traces
(mode "live") instead of scripted ones. The scorer never trusts the
agent's claim: `completed` is computed from the deliverable artifact
the runner read back, the must_not_use list, and the raw rollout.

- produced: the deliverable file exists and its stripped content equals
  the expected value exactly.
- stale_fact_used: any must_not_use string appears as a substring of the
  deliverable content. (Suites are authored to avoid collisions where a
  stale value is a substring of the expected value; the one documented
  exception is conf-f03-recovery, whose must_not_use omits the
  intermediate value for exactly this reason.)
- contract_violations: a Turnkeep finish was rejected STALE_SNAPSHOT and
  no finish receipt was ever recorded afterwards. A stale rejection that
  the agent recovered from (resume + successful finish) is correct
  behaviour, not a violation.
- irrelevant_refusal: the agent produced no deliverable and its last
  message refuses or says it cannot, on a task whose change is irrelevant.
- redo_cost_steps: tool-call steps beyond minimal_steps.
- tokens: codex "tokens used" (cumulative per session) when present,
  else "unknown". NOTE: codex resends ~21k fixed context tokens per turn,
  so the per-attempt input total is dominated by harness overhead, not
  task work; it is reported, not hidden.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "harness"))

from scoring import summarize  # noqa: E402

REQUIRED = ("task_id", "arm", "seed", "mode", "steps", "deliverable")


def _corrupt(reason):
    raise ValueError(f"corrupt trace: {reason}")


def score_live_trace(trace, task):
    for field in REQUIRED:
        if field not in trace:
            _corrupt(f"missing field {field!r}")
    if trace["mode"] != "live":
        _corrupt(f"unsupported mode {trace['mode']!r} (this scorer only reads live traces)")
    if not isinstance(trace["steps"], list):
        _corrupt("steps must be a list")

    expected = task["expected"]
    stale_values = list(task.get("must_not_use", []))
    d = trace["deliverable"]
    content = d.get("content") if d.get("exists") else None
    produced = content is not None and content == expected["value"]
    stale_used = bool(content is not None and any(s in content for s in stale_values))
    # A stale-finish rejection only counts as a violation when the agent
    # never recovered with a successful finish afterwards.
    violations = 0
    if trace.get("arm") == "turnkeep" and trace.get("stale_finish_rejections", 0) > 0 \
            and not trace.get("finish_receipt"):
        violations = int(trace["stale_finish_rejections"])
    refused = False
    if not produced:
        # Refusal is inferred only from the agent's own last message.
        msg = (trace.get("last_message") or "").lower()
        refused = any(w in msg for w in ("cannot", "can't", "unable", "refuse", "won't", "not able"))
    irrelevant_refusal = bool(refused and task.get("change", {}).get("relevant") is False)
    redo_cost = max(0, len(trace["steps"]) - task.get("minimal_steps", 0))
    usage = trace.get("usage")
    tokens = usage if isinstance(usage, dict) else "unknown"
    completed = bool(produced) and not stale_used and not violations and not trace.get("timeout")
    return {
        "task_id": trace["task_id"], "arm": trace["arm"], "seed": trace["seed"],
        "mode": trace["mode"],
        "completed": completed,
        "claimed_completed": bool(trace.get("claimed_completed")),
        "claim_overridden": bool(trace.get("claimed_completed")) and not completed,
        "contract_violations": violations,
        "stale_fact_used": stale_used,
        "irrelevant_refusal": irrelevant_refusal,
        "redo_cost_steps": redo_cost,
        "steps": len(trace["steps"]),
        "latency_ms": trace.get("latency_ms", "unknown"),
        "tokens": tokens,
        "memory_consulted": bool(trace.get("memory_consulted")),
        "stale_finish_rejections": int(trace.get("stale_finish_rejections", 0)),
        "finish_receipt": bool(trace.get("finish_receipt")),
        "error": trace.get("error"),
        "timeout": bool(trace.get("timeout")),
    }


__all__ = ["score_live_trace", "summarize"]
