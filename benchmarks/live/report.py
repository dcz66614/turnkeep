#!/usr/bin/env python3
"""Live-eval report (T042).

  python benchmarks/live/report.py --input evidence/live-evals

Reads the live-eval evidence directory and prints the result table plus the
NOT_RUN gap table. With no live attempts it reports NOT_RUN and lists the
gaps — it never invents a result table from an empty directory.
Exit 0 = report written; 1 = input missing or unreadable.
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()
    inp = Path(args.input)
    if not inp.exists():
        print(f"input directory missing: {inp}", file=sys.stderr)
        return 1
    attempts = []
    for f in sorted(inp.glob("attempts*.jsonl")):
        attempts.extend(json.loads(l) for l in f.read_text().splitlines() if l.strip())
    # Campaigns (T042 live runs) keep their attempts under
    # campaign-*/live-pilot|live-confirmation/attempts.jsonl.
    campaigns = []
    for cdir in sorted(inp.glob("campaign-*")):
        entry = {"campaign": cdir.name,
                 "contaminated": (cdir / "CONTAMINATED").exists()}
        for suite in ("live-pilot", "live-confirmation"):
            sfile = cdir / suite / "summary.json"
            if sfile.exists():
                try:
                    s = json.loads(sfile.read_text())
                except ValueError:
                    continue
                entry[suite] = {"attempts": s.get("attempts"),
                                "completed": s.get("overall", {}).get("completed"),
                                "success_rate": s.get("overall", {}).get("success_rate")}
                attempts.extend(
                    json.loads(l) for l in
                    (cdir / suite / "attempts.jsonl").read_text().splitlines()
                    if l.strip()) if (cdir / suite / "attempts.jsonl").exists() else []
        campaigns.append(entry)
    gap = {}
    gap_path = inp / "gap.json"
    if gap_path.exists():
        gap = json.loads(gap_path.read_text())
    report = {"status": "NOT_RUN" if not attempts else "HAS_LIVE_RESULTS",
              "attempts": len(attempts),
              "campaigns": campaigns,
              "gap": gap or None,
              "note": "No live model results exist yet. Gate C is NOT_RUN until a real "
                      "model campaign runs under the frozen protocol with an authorised budget."
                      if not campaigns else
                      "Campaign attempts exist; a campaign marked contaminated must not be "
                      "quoted as a result — see its GAP record. Gate C stays NOT_RUN until "
                      "a clean pilot + confirmation complete."}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
