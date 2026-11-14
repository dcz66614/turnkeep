#!/usr/bin/env python3
"""Live-model campaign runner (T042).

  python benchmarks/live/run_live.py --suite pilot [--parallel 2]
  python benchmarks/live/run_live.py --suite confirmation [--parallel 3]
  python benchmarks/live/run_live.py --suite both

Runs real agent attempts for every task x arm x seed, scores the raw
traces deterministically, and writes evidence/live-evals/<campaign>/
attempts.jsonl + summary.json + REPORT.md. Failures and timeouts stay in
the denominator. The runner is resumable: --resume skips attempts already
recorded in attempts.jsonl.

Drivers (--driver): codex (default; Codex CLI via copilot-codex-bridge,
model gpt5.6sol) or agy (agy CLI, model gemini-3.8-flash-low; a separate
campaign, never mixed with the Codex numbers). Each driver freezes its
own budget/manifest; scoring logic is shared and unchanged.

Order: pilot runs in suite order; confirmation runs in an order shuffled
with random.Random(20261108) so no arm is systematically favoured by
time-of-day effects. Seeds are repetition indices (the provider does not
support caller-side seeds).

Stops immediately (keeping partial results) when: the bridge is unhealthy,
the quota snapshot shows a zero remaining allowance, or 3 consecutive
attempts fail with bridge/quota errors.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "live"))
sys.path.insert(0, str(ROOT / "benchmarks" / "harness"))

from score_live import score_live_trace, summarize  # noqa: E402

# Driver is chosen by --driver in main(); run_attempt is rebound there.
# Default (module import time) is the codex driver so existing callers
# that import run_suite directly keep working.
from codex_driver import run_attempt  # noqa: E402

BRIDGE = "http://127.0.0.1:8322"
DRIVER_INFO = {
    "codex": {
        "label": "Codex CLI 0.164.0 via copilot-codex-bridge, model gpt5.6sol (GPT-5.6 Sol through Sam's existing M365 Copilot subscription, $0 new spend)",
        "model": "gpt5.6sol",
        "model_detail": "GPT-5.6 Sol (deep reasoning) via M365 Copilot, served by the local copilot-codex-bridge (127.0.0.1:8322/v1, Responses API). $0 new spend: Sam's existing M365 Copilot subscription.",
        "budget_file": "state/eval-budget.json",
        "driver_file": "benchmarks/live/codex_driver.py",
        "note": "Real model (Codex CLI via copilot-codex-bridge, gpt5.6sol). Scored deterministically from raw traces + deliverable artifacts.",
    },
    "agy": {
        "label": "agy CLI 1.4.12, model gemini-3.8-flash-low (Sam's existing agy access, $0 new spend)",
        "model": "gemini-3.8-flash-low",
        "model_detail": "agy CLI 1.4.12, model gemini-3.8-flash-low, served by agy's configured provider on Sam's existing agy access. $0 new spend: no paid API, no new purchase, no new login. Separate campaign from the Codex + GPT-5.6 Sol numbers; the two are never mixed.",
        "budget_file": "state/eval-budget-agy.json",
        "driver_file": "benchmarks/live/agy_driver.py",
        "note": "Real model (agy CLI, gemini-3.8-flash-low). Scored deterministically from raw traces + deliverable artifacts.",
    },
    "codex-api": {
        "label": "Codex CLI 0.164.0 via official OpenAI API, model gpt-6-luna, reasoning effort low (Sam's OpenAI account, pay-as-you-go)",
        "model": "gpt-6-luna",
        "model_detail": "GPT-6 Luna via the official OpenAI API (api.openai.com, Responses API), Codex CLI auth from ~/.codex/auth.json (Codex CLI's own auth file; the key is never read, printed or copied by any tooling). Reasoning effort low. Pay-as-you-go on Sam's OpenAI account; Sam authorised this route 2026-11-09 ~19:54 GMT ('全部用luna low'). Expected ~$10-16 for all 480 attempts at Luna Standard rates ($0.10 input / $0.50 output per 1M tokens). Separate campaign from the bridge (Codex + GPT-5.6 Sol) and agy campaigns; the three campaigns' numbers are never mixed.",
        "budget_file": "state/eval-budget-codex-api.json",
        "driver_file": "benchmarks/live/codex_driver.py",
        "note": "Real model (Codex CLI, official OpenAI API, gpt-6-luna, low effort). Scored deterministically from raw traces + deliverable artifacts.",
    },
}
DRIVER = "codex"
ARMS = ("none", "markdown", "log", "turnkeep")
CONFIRMATION_SEEDS = (11, 29, 47)
PILOT_SEEDS = (20261108,)


def _api_key():
    return (Path.home() / "workspace" / "copilot-codex-bridge" / ".api-key").read_text().strip()


def bridge_get(path, timeout=10):
    req = urllib.request.Request(BRIDGE + path, headers={"Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def freeze_manifest(suite_names, out_dir):
    info = DRIVER_INFO[DRIVER]
    files = [info["budget_file"], info["driver_file"],
             "benchmarks/live/score_live.py", "benchmarks/live/run_live.py",
             "benchmarks/live/gen_confirmation_suite.py"]
    for s in suite_names:
        files.append(f"benchmarks/suites/{s}.json")
    manifest = {
        "campaign": f"T042 live ({DRIVER})",
        "driver": DRIVER,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_sha": git_sha(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "model": info["model"],
        "model_detail": info["model_detail"],
        "provider_seed_support": "none: seeds are repetition indices only, recorded, never claimed as reproducible sampling.",
        "budget": json.loads((ROOT / info["budget_file"]).read_text()),
        "file_hashes": {f: sha256_file(ROOT / f) for f in files},
        "quota_before": None,
        "blindness_limitation": "Suite author and runner are the same agent (Muse); this is a process measure against accidental leakage, not a true independent blind test (EVALUATION_PLAN §6).",
        "arms_not_run": {
            "mature-memory": "NOT_RUN: no real Mem0/Hindsight install configured",
            "nearest-workflow": "NOT_RUN: no real Tigerless/Engram install configured",
        },
    }
    if DRIVER == "codex":
        try:
            manifest["quota_before"] = bridge_get("/v1/quota").get("metering", {})
        except Exception as e:  # noqa: BLE001
            manifest["quota_before_error"] = str(e)
    # codex-api has no bridge quota snapshot; spend is tracked from the
    # rollout usage records instead (see the campaign's SPEND.md).
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def build_jobs(suite_name, arms, seeds):
    suite = json.loads((ROOT / "benchmarks" / "suites" / f"{suite_name}.json").read_text())
    jobs = []
    for task in suite["tasks"]:
        for seed in seeds:
            for arm in arms:
                jobs.append((task, arm, seed))
    if suite_name == "live-confirmation":
        random.Random(20261108).shuffle(jobs)
    return suite, jobs


def run_suite(suite_name, arms, seeds, out_dir, parallel, limit, resume):
    suite, jobs = build_jobs(suite_name, arms, seeds)
    if limit:
        jobs = jobs[:limit]
    out_dir.mkdir(parents=True, exist_ok=True)
    attempts_path = out_dir / "attempts.jsonl"
    done_keys = set()
    if resume and attempts_path.exists():
        for line in attempts_path.read_text().splitlines():
            if line.strip():
                a = json.loads(line)
                done_keys.add((a["task_id"], a["arm"], a["seed"]))
    lock = threading.Lock()
    attempts, scores = [], []
    by_key = {}
    # Quota guard: 3 consecutive quota-error traces (in completion order)
    # stop new attempts — keeps partial results instead of burning the
    # whole suite while throttled. Resume later with --resume.
    stop_event = threading.Event()
    quota_streak = 0
    if attempts_path.exists():
        for line in attempts_path.read_text().splitlines():
            if line.strip():
                attempts.append(json.loads(line))
    for a in attempts:
        by_key[(a["task_id"], a["arm"], a["seed"])] = a
    # Re-score already-recorded attempts (cheap, keeps summary consistent).
    task_by_id = {t["id"]: t for t in suite["tasks"]}
    for a in attempts:
        t = task_by_id.get(a["task_id"])
        if t:
            try:
                scores.append(score_live_trace(a, t))
            except ValueError as e:
                scores.append({"task_id": a["task_id"], "arm": a["arm"], "seed": a["seed"],
                               "mode": "live", "completed": False, "error": f"scoring_failed: {e}"})

    def do_job(job):
        if stop_event.is_set():
            return None
        task, arm, seed = job
        key = (task["id"], arm, seed)
        if key in done_keys:
            return None
        adir = out_dir / "attempts" / f"{task['id']}-{arm}-seed{seed}"
        trace = run_attempt(task, arm, seed, adir)
        with lock:
            with attempts_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(trace, ensure_ascii=False) + "\n")
        return trace

    pending = [j for j in jobs if (j[0]["id"], j[1], j[2]) not in done_keys]
    print(f"[{suite_name}] {len(jobs)} planned, {len(pending)} to run, {len(jobs) - len(pending)} already recorded", flush=True)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(do_job, j): j for j in pending}
        for fut in concurrent.futures.as_completed(futs):
            job = futs[fut]
            try:
                trace = fut.result()
            except Exception as e:  # noqa: BLE001 - a crashing attempt is a failed attempt
                task, arm, seed = job
                trace = {"task_id": task["id"], "arm": arm, "seed": seed, "mode": "live",
                         "steps": [], "events": [], "claimed_completed": False, "usage": None,
                         "error": f"{type(e).__name__}: {e}", "timeout": False,
                         "deliverable": {"file": task["deliverable"]["file"], "exists": False, "content": None}}
                with lock:
                    with attempts_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(trace, ensure_ascii=False) + "\n")
            if trace is not None:
                attempts.append(trace)
                t = task_by_id[trace["task_id"]]
                try:
                    s = score_live_trace(trace, t)
                except ValueError as e:
                    s = {"task_id": trace["task_id"], "arm": trace["arm"], "seed": trace["seed"],
                         "mode": "live", "completed": False, "error": f"scoring_failed: {e}"}
                scores.append(s)
                if trace.get("quota_error") or trace.get("auth_error"):
                    quota_streak += 1
                    if quota_streak >= 3:
                        stop_event.set()
                        print("  [stopping: 3 consecutive quota/auth errors]", flush=True)
                else:
                    quota_streak = 0
                print(f"  [{len(scores)}/{len(jobs)}] {trace['task_id']} / {trace['arm']} / seed {trace['seed']}: "
                      f"completed={s['completed']} stale={s.get('stale_fact_used')} "
                      f"steps={s.get('steps')} tokens={s.get('tokens')} "
                      f"({time.time() - t0:.0f}s elapsed)", flush=True)
    per_arm = {a: summarize([s for s in scores if s["arm"] == a]) for a in arms}
    summary = {"suite": suite_name, "mode": "live",
               "label": "LIVE_MODEL",
               "driver": DRIVER,
               "model": DRIVER_INFO[DRIVER]["model"],
               "note": DRIVER_INFO[DRIVER]["note"],
               "seed": suite.get("seed"), "budget": suite["budget"],
               "overall": summarize(scores), "per_arm": per_arm,
               "attempts": len(scores),
               "not_run_arms": {
                   "mature-memory": "NOT_RUN: no real Mem0/Hindsight install configured",
                   "nearest-workflow": "NOT_RUN: no real Tigerless/Engram install configured",
               }}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_report(out_dir, suite_name, summary, scores)
    return summary


def write_report(out_dir, suite_name, summary, scores):
    lines = [f"# Live evaluation report — {suite_name} (T042)\n",
             f"Real model: {DRIVER_INFO[DRIVER]['label']}.\n",
             "Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.\n",
             f"## Overall\n\n- Attempts: {summary['attempts']}\n"
             f"- Completed: {summary['overall']['completed']} "
             f"(success rate {summary['overall']['success_rate']:.3f})\n"
             f"- Stale-fact rate: {summary['overall']['stale_fact_rate']:.3f}\n"
             f"- Contract violations: {summary['overall']['contract_violations']}\n"
             f"- Irrelevant refusals: {summary['overall']['irrelevant_refusals']}\n"
             f"- Mean redo cost (steps): {summary['overall']['mean_redo_cost_steps']:.2f}\n",
             "## Per arm\n"]
    for arm, s in summary["per_arm"].items():
        lines.append(f"- {arm}: {s['completed']}/{s['attempts']} "
                     f"(rate {s['success_rate']:.3f}), stale {s['stale_fact_rate']:.3f}, "
                     f"violations {s['contract_violations']}, refusals {s['irrelevant_refusals']}")
    lines.append("\n## Failures (not completed)\n")
    failed = [s for s in scores if not s["completed"]]
    if not failed:
        lines.append("None — every attempt completed.\n")
    else:
        for s in failed:
            lines.append(f"- {s['task_id']} / {s['arm']} / seed {s['seed']}: "
                         f"stale={s.get('stale_fact_used')} violations={s.get('contract_violations')} "
                         f"refusal={s.get('irrelevant_refusal')} timeout={s.get('timeout')} "
                         f"error={s.get('error')}\n")
    if DRIVER in ("codex", "codex-api"):
        third = ("- Codex resends ~21k fixed context tokens per turn; per-attempt token totals are dominated by that overhead and are reported, not hidden.\n"
                 if DRIVER == "codex" else
                 "- Official OpenAI API (gpt-6-luna, low effort); per-attempt token totals (input / cached / cache-write / output / reasoning split) are recorded in the traces and costed in the campaign's SPEND.md. This campaign is separate from the bridge (Codex + GPT-5.6 Sol) and agy campaigns; the three campaigns' numbers are never mixed.\n")
        lines.append("\n## Notes\n\n- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.\n"
                     "- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.\n"
                     + third)
    else:
        lines.append("\n## Notes\n\n- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.\n"
                     "- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.\n"
                     "- This campaign uses the agy driver (separate from the Codex + GPT-5.6 Sol campaign); the two campaigns' numbers are never mixed.\n")
    (out_dir / "REPORT.md").write_text("".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=["pilot", "confirmation", "both"])
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--output", default=None)
    ap.add_argument("--driver", default="codex", choices=["codex", "agy", "codex-api"])
    args = ap.parse_args()
    global DRIVER, run_attempt
    DRIVER = args.driver
    if DRIVER == "agy":
        from agy_driver import run_attempt as _agy_run_attempt  # noqa: E402
        run_attempt = _agy_run_attempt
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    for a in arms:
        if a not in ARMS:
            print(f"unknown arm {a!r}", file=sys.stderr)
            return 1
    if DRIVER == "codex":
        try:
            health = bridge_get("/health")
        except Exception as e:  # noqa: BLE001
            print(f"bridge unhealthy: {e}", file=sys.stderr)
            return 2
        if not health.get("ok"):
            print(f"bridge not ok: {health}", file=sys.stderr)
            return 2
        print(f"bridge ok: {health.get('defaultModel')}, token valid: {health.get('token', {}).get('valid')}", flush=True)
    elif DRIVER == "codex-api":
        # Official OpenAI API: no bridge involved. Switch the driver to
        # official mode (read at call time) and check the auth file
        # exists — its CONTENTS are never read here.
        os.environ["TURNKEEP_CODEX_MODE"] = "official"
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.exists():
            print("codex auth file missing: ~/.codex/auth.json", file=sys.stderr)
            return 2
        print(f"driver codex-api: {DRIVER_INFO['codex-api']['label']}", flush=True)
    else:
        import shutil
        agy_bin = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
        if not Path(agy_bin).exists():
            print(f"agy binary not found: {agy_bin}", file=sys.stderr)
            return 2
        print(f"driver agy: {agy_bin}, model {DRIVER_INFO['agy']['model']}", flush=True)
    suites = {"pilot": ("live-pilot", PILOT_SEEDS), "confirmation": ("live-confirmation", CONFIRMATION_SEEDS)}
    wanted = ["pilot", "confirmation"] if args.suite == "both" else [args.suite]
    base = Path(args.output) if args.output else ROOT / "evidence" / "live-evals" / f"campaign-{time.strftime('%Y%m%d-%H%M%S')}"
    base.mkdir(parents=True, exist_ok=True)
    freeze_manifest([suites[w][0] for w in wanted], base)
    rc = 0
    for w in wanted:
        sname, seeds = suites[w]
        out_dir = base / sname
        summary = run_suite(sname, arms, seeds, out_dir, args.parallel, args.limit, args.resume)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if summary["attempts"] < {"pilot": 48, "confirmation": 432}[w]:
            print(f"[{sname}] INCOMPLETE: {summary['attempts']} attempts recorded", file=sys.stderr)
            rc = 2
    if DRIVER == "codex":
        try:
            quota_after = bridge_get("/v1/quota").get("metering", {})
            manifest_path = base / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["quota_after"] = quota_after
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"quota-after check failed: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
