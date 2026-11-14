#!/usr/bin/env python3
"""Live-model evaluation runner (T042).

  python benchmarks/live/run.py --protocol benchmarks/PROTOCOL.md --budget-file state/eval-budget.json

Gate C (EVALUATION_PLAN §5-§9) needs a real model, real credentials and an
authorised budget. This runner NEVER simulates a live run:

- If the budget file is missing, no model is selected, or no model
  endpoint is reachable, it writes a NOT_RUN gap record to
  evidence/live-evals/ and exits 2 (NOT_RUN).
- Spend: a positive authorised budget is accepted. A $0 budget is
  accepted ONLY when the budget file explicitly authorises zero NEW spend
  (zero_new_spend_authorized=true with a named basis, e.g. an existing
  subscription) — $0 then means "no new purchase", not "no model".
- With --live, preconditions met means the campaign actually runs:
  run_live.py executes the frozen pilot, then the frozen confirmation
  set. Without --live this script still refuses to start a campaign on
  its own (the original guarded behaviour).

Exit codes: 0 = a live campaign completed (results in evidence/live-evals/);
2 = NOT_RUN (preconditions missing, or the campaign did not complete —
see the gap record); 1 = harness error.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _campaign_complete():
    """A completed campaign exists: pilot (48 attempts) + confirmation
    (432 attempts) summaries plus a frozen manifest. Returns the newest
    campaign dir, or None."""
    base = ROOT / "evidence" / "live-evals"
    if not base.exists():
        return None
    for campaign in sorted(base.glob("campaign-*"), reverse=True):
        try:
            pilot = json.loads((campaign / "live-pilot" / "summary.json").read_text())
            conf = json.loads((campaign / "live-confirmation" / "summary.json").read_text())
            manifest = json.loads((campaign / "manifest.json").read_text())
        except (OSError, ValueError, KeyError):
            continue
        if (campaign / "CONTAMINATED").exists():
            continue
        if pilot.get("attempts") == 48 and conf.get("attempts") == 432 and manifest.get("model"):
            return campaign
    return None


def _gap_record(reason, details):
    out = ROOT / "evidence" / "live-evals"
    out.mkdir(parents=True, exist_ok=True)
    record = {"task": "T042", "status": "NOT_RUN", "reason": reason, "details": details,
              "note": "No live model call was made by this invocation. "
                      "Earlier partial/contaminated campaigns, if any, live under "
                      "evidence/live-evals/campaign-*/ — see their GAP records; "
                      "do not present this file as the whole T042 story."}
    (out / "gap.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--protocol", required=True)
    ap.add_argument("--budget-file", required=True)
    ap.add_argument("--live", action="store_true",
                    help="actually run the frozen live campaign (pilot, then confirmation)")
    ap.add_argument("--resume", action="store_true",
                    help="with --live: continue the newest incomplete campaign instead of starting a new one")
    ap.add_argument("--parallel", type=int, default=2)
    args = ap.parse_args()
    protocol = Path(args.protocol)
    if not protocol.exists():
        print(f"NOT_RUN: protocol file missing: {protocol}", file=sys.stderr)
        return 2
    budget_path = Path(args.budget_file)
    if not budget_path.exists():
        rec = _gap_record("budget file missing",
                          {"budget_file": str(budget_path),
                           "fix": "Sam must authorise a total budget and a model first; "
                                  "see state/eval-budget.json (template, $0 authorised)."})
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 2
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    if not budget.get("model"):
        rec = _gap_record("no model selected",
                          {"budget_file": str(budget_path),
                           "authorised_spend_usd": budget.get("authorised_spend_usd", 0),
                           "fix": "Select a model ID and freeze it in the budget file before any live run."})
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 2
    spend = float(budget.get("authorised_spend_usd", 0))
    zero_ok = bool(budget.get("zero_new_spend_authorized")) and bool(budget.get("zero_new_spend_basis"))
    if spend <= 0 and not zero_ok:
        rec = _gap_record("authorised spend is $0",
                          {"model": budget.get("model"),
                           "fix": "Authorise a total budget for the campaign, or freeze an explicit "
                                  "zero-new-spend authorisation (existing subscription) in the budget file."})
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 2
    # Credentials: the model client must be configured through the environment
    # or frozen in the budget file (local endpoint), never through a secret
    # pasted into a file or a command line.
    endpoint = os.environ.get("TURNKEEP_EVAL_MODEL_ENDPOINT") or budget.get("endpoint")
    if not endpoint:
        rec = _gap_record("no model endpoint configured",
                          {"model": budget.get("model"),
                           "fix": "Set TURNKEEP_EVAL_MODEL_ENDPOINT to an authorised model "
                                  "endpoint (or a local model server) before running."})
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 2
    if not args.live:
        # A completed campaign already exists: report it, do not re-run.
        done = _campaign_complete()
        if done is not None:
            print(json.dumps({"task": "T042", "status": "LIVE_COMPLETE",
                              "model": budget.get("model"), "campaign": str(done),
                              "note": "A completed live campaign exists; see its manifest and REPORT.md files."},
                             indent=2, ensure_ascii=False))
            return 0
        # Guarded entry point (original behaviour): preconditions exist, but
        # a live campaign still needs an explicit --live go-ahead.
        rec = _gap_record("live campaign not started by this script",
                          {"model": budget.get("model"),
                           "authorised_spend_usd": budget.get("authorised_spend_usd"),
                           "fix": "Preconditions are met, but a live campaign requires an explicit "
                                  "go-ahead. Re-run with --live to run the pilot (6 tasks x 2 variants) "
                                  "first, then the frozen confirmation set, per EVALUATION_PLAN §6-§7."})
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 2
    # --live: run the frozen campaign for real. run_live.py owns execution,
    # traces, scoring and reports; this script only maps its exit code.
    env = dict(os.environ)
    env["TURNKEEP_EVAL_MODEL_ENDPOINT"] = endpoint
    cmd = [sys.executable, str(ROOT / "benchmarks" / "live" / "run_live.py"),
           "--suite", "both", "--parallel", str(args.parallel)]
    if args.resume:
        base = ROOT / "evidence" / "live-evals"
        latest = sorted(base.glob("campaign-*"), reverse=True)[:1] if base.exists() else []
        if latest:
            cmd += ["--output", str(latest[0]), "--resume"]
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if proc.returncode == 0:
        print(json.dumps({"task": "T042", "status": "LIVE_COMPLETE",
                          "model": budget.get("model"),
                          "note": "Live campaign completed; results in evidence/live-evals/. "
                                  "See the campaign manifest and REPORT.md files there."},
                         indent=2, ensure_ascii=False))
        return 0
    rec = _gap_record("live campaign incomplete",
                      {"model": budget.get("model"),
                       "campaign_exit": proc.returncode,
                       "fix": "The campaign stopped before completing. Results recorded so far "
                              "are in evidence/live-evals/; re-run with --live --resume to continue "
                              "from the recorded attempts."})
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
