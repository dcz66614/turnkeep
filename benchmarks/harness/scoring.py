"""Deterministic scorer (T040). Reads raw traces, never trusts claims.

Rules (pre-registered in benchmarks/PROTOCOL.md):
- A trace is corrupt when a required field is missing or malformed:
  scoring raises ValueError and the attempt is recorded as scoring_failed,
  never as a pass.
- `completed` is computed by the scorer, not by the trace's claim: the
  trace must contain a produce event with the expected outcome, no
  stale_fact_used event, and no contract_violation event. A trace that
  claims completed=True while using a stale fact scores completed=False.
- Metrics: contract_violations (count), stale_fact_used (bool),
  irrelevant_refusal (bool), redo_cost (steps beyond minimal_steps),
  latency_ms (trace timestamps), tokens (usage if present, else "unknown").
"""
from __future__ import annotations

REQUIRED = ("task_id", "arm", "seed", "mode", "steps", "events")


def _corrupt(reason):
    raise ValueError(f"corrupt trace: {reason}")


def score_trace(trace, task):
    for field in REQUIRED:
        if field not in trace:
            _corrupt(f"missing field {field!r}")
    if not isinstance(trace["steps"], list) or not isinstance(trace["events"], list):
        _corrupt("steps/events must be lists")
    if trace["mode"] != "deterministic":
        _corrupt(f"unsupported mode {trace['mode']!r} (this scorer only reads deterministic traces)")

    events = trace["events"]
    expected = task["expected"]
    stale_values = set(task.get("must_not_use", []))
    produced = [e for e in events
                if e.get("type") == "produce"
                and e.get("key") == expected["key"]
                and e.get("value") == expected["value"]]
    stale_used = any(e.get("type") == "use_fact" and e.get("value") in stale_values
                     for e in events)
    violations = [e for e in events if e.get("type") == "contract_violation"]
    refused = any(e.get("type") == "refused" for e in events)
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
        "contract_violations": len(violations),
        "stale_fact_used": stale_used,
        "irrelevant_refusal": irrelevant_refusal,
        "redo_cost_steps": redo_cost,
        "steps": len(trace["steps"]),
        "latency_ms": trace.get("latency_ms", "unknown"),
        "tokens": tokens,
        "error": trace.get("error"),
        "timeout": bool(trace.get("timeout")),
    }


def summarize(scores):
    """Aggregate over attempts. Failures and timeouts stay in the denominator."""
    total = len(scores)
    done = sum(1 for s in scores if s["completed"])
    return {
        "attempts": total,
        "completed": done,
        "success_rate": (done / total) if total else 0.0,
        "stale_fact_rate": (sum(1 for s in scores if s["stale_fact_used"]) / total) if total else 0.0,
        "contract_violations": sum(s["contract_violations"] for s in scores),
        "irrelevant_refusals": sum(1 for s in scores if s["irrelevant_refusal"]),
        "mean_redo_cost_steps": (sum(s["redo_cost_steps"] for s in scores) / total) if total else 0.0,
    }
