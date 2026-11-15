#!/usr/bin/env python3
"""Live-model attempt driver (T042) — agy route.

Runs ONE attempt of a live evaluation task with the real agent:
agy CLI 1.4.12, model gemini-3.8-flash-low (Sam's existing agy access,
$0 new spend). Used while the Copilot bridge (Codex + GPT-5.6 Sol) is
rate-limited — this is a SEPARATE campaign, never mixed with the
Codex/GPT-5.6 Sol numbers.

Design mirrors benchmarks/live/codex_driver.py and reuses its
seed_memory / apply_change / build_prompt / read_deliverable /
turnkeep_receipt, so the task, arms, prompts and scoring inputs are
identical; only the agent harness differs.

agy interface (verified 2026-11-09 by a real pilot-task smoke test):
- `agy -p <prompt> --model <model> --output-format stream-json
  --dangerously-skip-permissions`, run with cwd=workdir (agy treats the
  cwd as its project; it writes no extra files into the workdir).
- stream-json events: `init` (conversation_id), `step_update`
  (step_type tool / agent_response, with tool_name, tool_info.parameters
  and tool_info.output on DONE, and per-step usage), `result`
  (status, response, duration_seconds, usage.total_tokens).
- Resume a conversation: `--conversation <id> -p <prompt>`.
- The agent's claims are never trusted: the trace records the raw tool
  calls + outputs and the deliverable artifact; scoring is done
  separately by score_live.py.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "live"))

from codex_driver import (  # noqa: E402
    apply_change,
    build_prompt,
    read_deliverable,
    seed_memory,
    turnkeep_receipt,
)

AGY_BIN = str(Path.home() / ".local" / "bin" / "agy")
MODEL = "gemini-3.8-flash-low"


def _run_agy(workdir, prompt, conversation_id, timeout_s, meta_dir):
    """Run one agy turn. Returns a dict like codex_driver._run_codex."""
    argv = [
        AGY_BIN, "-p", prompt, "--model", MODEL,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    if conversation_id:
        argv += ["--conversation", conversation_id]
    env = dict(os.environ)
    t0 = time.monotonic()
    try:
        # agy treats its process cwd as the project directory (verified:
        # the init event reports cwd == workdir). Unlike codex, passing
        # cwd=workdir does not break startup.
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s, env=env, cwd=str(workdir),
                              encoding="utf-8")
        timed_out = False
        stdout, stderr = proc.stdout, proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        rc = None
    dur_ms = int((time.monotonic() - t0) * 1000)
    parsed = parse_stream(stdout)
    # Quota exhaustion (HTTP 429) is an infrastructure failure, not an
    # agent measurement: flag it so the runner can stop early instead of
    # burning hundreds of attempts while throttled.
    quota_error = "RESOURCE_EXHAUSTED" in (stderr or "") or "code 429" in (stderr or "")
    return {"returncode": rc, "stdout": stdout, "stderr": stderr,
            "session_id": parsed.get("conversation_id") or conversation_id,
            "tokens_used": parsed.get("max_total_tokens"),
            "duration_ms": dur_ms, "timed_out": timed_out,
            "quota_error": quota_error,
            "parsed": parsed}


def parse_stream(stdout):
    """Extract tool calls, messages and usage from agy stream-json output.

    Returns the same shape as codex_driver.parse_rollout so run_attempt
    and score_live.py work unchanged.
    """
    out = {"tool_calls": [], "agent_messages": [], "max_total_tokens": None,
           "task_duration_ms": None, "last_message": None, "found": False,
           "task_completed": False, "stale_rejections": 0, "finish_receipt": False,
           "conversation_id": None, "error": None}
    if not stdout:
        return out
    max_tokens = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = d.get("event")
        if ev == "init":
            out["conversation_id"] = d.get("conversation_id")
            continue
        if ev == "step_update":
            su = d.get("step_update", {})
            if su.get("step_type") == "tool" and su.get("state") == "DONE":
                info = su.get("tool_info") or {}
                params = info.get("parameters") or {}
                args = json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else str(params)
                output = str(info.get("output", ""))
                if "STALE_SNAPSHOT" in output:
                    out["stale_rejections"] += 1
                if '"receipt"' in output:
                    out["finish_receipt"] = True
                out["tool_calls"].append({
                    "name": su.get("tool_name"),
                    "arguments": args,
                    "output": output[:2000],
                    "timestamp": None,
                })
            elif su.get("step_type") == "agent_response":
                u = su.get("usage") or {}
                tot = u.get("total_tokens")
                if isinstance(tot, int):
                    max_tokens = tot if max_tokens is None else max(max_tokens, tot)
            continue
        if ev == "result":
            r = d.get("result", {})
            out["found"] = True
            out["conversation_id"] = r.get("conversation_id") or out["conversation_id"]
            out["task_completed"] = (r.get("status") == "SUCCESS")
            if r.get("status") != "SUCCESS":
                # The harness turn itself failed (e.g. quota 429): this is
                # an infrastructure error, not the agent failing the task.
                out["error"] = f"agy turn failed: status={r.get('status')}"
            out["last_message"] = r.get("response")
            if r.get("response"):
                out["agent_messages"].append(r["response"])
            u = r.get("usage") or {}
            tot = u.get("total_tokens")
            if isinstance(tot, int):
                max_tokens = tot if max_tokens is None else max(max_tokens, tot)
            dur = r.get("duration_seconds")
            if isinstance(dur, (int, float)):
                out["task_duration_ms"] = int(dur * 1000)
            continue
    out["max_total_tokens"] = max_tokens
    return out


def run_attempt(task, arm, seed, attempt_dir, timeout_s=600):
    """Run one full attempt (phase A, plus phase B for recovery variant).

    Same structure and trace shape as codex_driver.run_attempt; only the
    agent harness (agy) differs.
    """
    attempt_dir = Path(attempt_dir)
    workdir = attempt_dir / "work"
    meta_dir = attempt_dir / "meta"
    workdir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = seed_memory(task, arm, workdir)
    apply_change(task, arm, workdir, task.get("change"), meta)
    (meta_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    phases = []
    conversation_id = None
    timed_out = False
    nudges = 0

    def _nudge_phase(tag):
        """One resume turn when the deliverable is still missing."""
        nonlocal conversation_id, timed_out, nudges
        prompt_n = (
            f"The deliverable file {task['deliverable']['file']} is still missing. "
            "You have not finished. Write it now with a tool call, verify it with `cat`, "
            "then end your turn with a one-line summary.\n"
            "You MUST do the work with tool calls. Do not just reply in text.\n"
        )
        (meta_dir / f"prompt-{tag}.txt").write_text(prompt_n, encoding="utf-8")
        rn = _run_agy(workdir, prompt_n, conversation_id, timeout_s, meta_dir)
        (meta_dir / f"agy-{tag}.stdout.txt").write_text(rn["stdout"], encoding="utf-8")
        (meta_dir / f"agy-{tag}.stderr.txt").write_text(rn["stderr"], encoding="utf-8")
        timed_out = timed_out or rn["timed_out"]
        rollout_n = rn["parsed"]
        (meta_dir / f"rollout-{tag}.json").write_text(json.dumps(rollout_n, indent=2, ensure_ascii=False), encoding="utf-8")
        phases.append({"phase": tag, "session_id": rn["session_id"], "returncode": rn["returncode"],
                       "duration_ms": rn["duration_ms"], "tokens_used": rn["tokens_used"],
                       "timed_out": rn["timed_out"], "rollout": rollout_n,
                       "quota_error": rn.get("quota_error", False),
                       "stderr_tail": rn["stderr"][-500:]})
        nudges += 1
        return read_deliverable(workdir, task)

    # Phase A
    prompt_a = build_prompt(task, arm, phase="A")
    (meta_dir / "prompt-A.txt").write_text(prompt_a, encoding="utf-8")
    r = _run_agy(workdir, prompt_a, None, timeout_s, meta_dir)
    (meta_dir / "agy-A.stdout.txt").write_text(r["stdout"], encoding="utf-8")
    (meta_dir / "agy-A.stderr.txt").write_text(r["stderr"], encoding="utf-8")
    conversation_id = r["session_id"]
    timed_out = timed_out or r["timed_out"]
    rollout_a = r["parsed"]
    (meta_dir / "rollout-A.json").write_text(json.dumps(rollout_a, indent=2, ensure_ascii=False), encoding="utf-8")
    phases.append({"phase": "A", "session_id": conversation_id, "returncode": r["returncode"],
                   "duration_ms": r["duration_ms"], "tokens_used": r["tokens_used"],
                   "timed_out": r["timed_out"], "rollout": rollout_a,
                   "quota_error": r.get("quota_error", False),
                   "stderr_tail": r["stderr"][-500:]})
    artifact_a = read_deliverable(workdir, task)
    if not artifact_a["exists"] and not timed_out:
        artifact_a = _nudge_phase("N1")

    # Phase B (recovery variant only)
    artifact_b = None
    if task.get("variant") == "recovery" and task.get("change2") and not timed_out:
        apply_change(task, arm, workdir, task["change2"], meta)
        (meta_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        prompt_b = build_prompt(task, arm, phase="B")
        (meta_dir / "prompt-B.txt").write_text(prompt_b, encoding="utf-8")
        r2 = _run_agy(workdir, prompt_b, conversation_id, timeout_s, meta_dir)
        (meta_dir / "agy-B.stdout.txt").write_text(r2["stdout"], encoding="utf-8")
        (meta_dir / "agy-B.stderr.txt").write_text(r2["stderr"], encoding="utf-8")
        timed_out = timed_out or r2["timed_out"]
        rollout_b = r2["parsed"]
        (meta_dir / "rollout-B.json").write_text(json.dumps(rollout_b, indent=2, ensure_ascii=False), encoding="utf-8")
        phases.append({"phase": "B", "session_id": r2["session_id"], "returncode": r2["returncode"],
                       "duration_ms": r2["duration_ms"], "tokens_used": r2["tokens_used"],
                       "timed_out": r2["timed_out"], "rollout": rollout_b,
                       "quota_error": r2.get("quota_error", False),
                       "stderr_tail": r2["stderr"][-500:]})
        artifact_b = read_deliverable(workdir, task)
        if not artifact_b["exists"] and not timed_out:
            artifact_b = _nudge_phase("N2")

    final_artifact = artifact_b if artifact_b is not None else artifact_a
    seen, tool_calls = set(), []
    for ph in phases:
        for tc in ph["rollout"].get("tool_calls", []):
            k = (tc.get("name"), tc.get("arguments", ""), tc.get("timestamp"))
            if k not in seen:
                seen.add(k)
                tool_calls.append(tc)
    args_text = "\n".join((tc.get("arguments", "") or "") for tc in tool_calls
                          if tc.get("name") != "__output__")
    # Memory access is judged from what the agent INVOKED (arguments only).
    if arm == "markdown":
        memory_consulted = "decisions.md" in args_text
    elif arm == "log":
        memory_consulted = "events.log" in args_text
    elif arm == "turnkeep":
        memory_consulted = "turnkeep" in args_text
    else:
        memory_consulted = False
    stale_rejections = sum(ph["rollout"].get("stale_rejections", 0) for ph in phases)
    finish_receipt = any(ph["rollout"].get("finish_receipt") for ph in phases)
    if arm == "turnkeep":
        finish_receipt = finish_receipt or turnkeep_receipt(workdir)
    total_tokens = max([ph["tokens_used"] for ph in phases if ph["tokens_used"]] or [None])
    total_ms = sum(ph["duration_ms"] for ph in phases)
    last_msgs = [ph["rollout"].get("last_message") for ph in phases if ph["rollout"].get("last_message")]
    last_message = last_msgs[-1] if last_msgs else None
    ended = any(ph["rollout"].get("task_completed") for ph in phases)
    rollout_errors = [ph["rollout"].get("error") for ph in phases if ph["rollout"].get("error")]
    start_error = rollout_errors[0] if rollout_errors else None
    if start_error is None and not any(ph["rollout"].get("found") for ph in phases):
        start_error = "agy did not start: no result event; stderr: " + \
            (phases[0].get("stderr_tail", "") if phases else "")
    quota_error = any(ph.get("quota_error") for ph in phases)
    trace = {
        "task_id": task["id"], "arm": arm, "seed": seed, "mode": "live",
        "steps": [{"tool": tc.get("name"), "arguments": (tc.get("arguments") or "")[:500]}
                  for tc in tool_calls
                  if tc.get("name") != "__output__"],
        "events": [],
        "claimed_completed": ended,
        "last_message": last_message,
        "nudges": nudges,
        "usage": {"tokens_total": total_tokens, "turns": len(phases)} if total_tokens else None,
        "latency_ms": total_ms,
        "timeout": timed_out,
        "error": start_error,
        "quota_error": quota_error,
        "session_id": conversation_id,
        "driver": "agy",
        "model": MODEL,
        "phases": [{"phase": ph["phase"], "duration_ms": ph["duration_ms"],
                    "tokens_used": ph["tokens_used"], "timed_out": ph["timed_out"],
                    "returncode": ph["returncode"]} for ph in phases],
        "memory_consulted": memory_consulted,
        "stale_finish_rejections": stale_rejections,
        "finish_receipt": finish_receipt,
        "deliverable": {"file": task["deliverable"]["file"], **final_artifact},
        "artifact_after_phase_A": artifact_a,
    }
    return trace
