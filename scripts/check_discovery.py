#!/usr/bin/env python3
"""T002 gate: every external fact in COMPETITORS.md / NAME_CHECK.md must carry
a URL and a check date; scorecard must be valid JSON with a decision.
Stdlib only. Usage: python scripts/check_discovery.py --ledger docs/COMPETITORS.md
Exit 0 = PASS, 1 = FAIL, 2 = NOT_RUN (missing input)."""
import argparse, json, re, sys
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--scorecard", default="docs/discovery-scorecard.json")
    ap.add_argument("--name-check", default="docs/NAME_CHECK.md")
    args = ap.parse_args()
    root = Path.cwd()
    errors, missing = [], []
    for name in (args.ledger, args.scorecard, args.name_check):
        if not (root / name).exists():
            missing.append(name)
    if missing:
        print(f"NOT_RUN missing: {missing}"); return 2
    ledger = (root / args.ledger).read_text(encoding="utf-8")
    name = (root / args.name_check).read_text(encoding="utf-8")
    for label, text in (("COMPETITORS", ledger), ("NAME_CHECK", name)):
        if "2026-11-08" not in text:
            errors.append(f"{label}: no check date 2026-11-08")
        if "http" not in text:
            errors.append(f"{label}: no source URL")
    if "turnkeep.app" not in name:
        errors.append("NAME_CHECK: turnkeep.app collision not recorded")
    if "NOT" not in name or "trademark" not in name.lower():
        errors.append("NAME_CHECK: trademark clearance status missing")
    try:
        sc = json.loads((root / args.scorecard).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL scorecard not valid JSON: {e}"); return 1
    if sc.get("decision") != "GO_WITH_BOUNDS":
        errors.append("scorecard decision is not GO_WITH_BOUNDS")
    if sc.get("name_check", {}).get("trademark_clearance") != "NOT_RUN":
        errors.append("scorecard must record trademark_clearance NOT_RUN")
    if errors:
        print("FAIL:"); [print(" -", e) for e in errors]; return 1
    print("PASS check_discovery: ledger+name-check+scorecard consistent, GO_WITH_BOUNDS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
