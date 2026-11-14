#!/usr/bin/env python3
"""OpenAPI snapshot check (T027). Compares the committed snapshot with the
route table's generated document; any drift fails.

  python scripts/check_openapi.py --snapshot specs/local-app-api.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from turnkeep.web.routes import ROUTES  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    args = ap.parse_args()
    snap_path = ROOT / args.snapshot
    if not snap_path.exists():
        print(f"FAIL: snapshot missing: {snap_path}", file=sys.stderr)
        return 1
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    generated = ROUTES.openapi()
    if snapshot != generated:
        print("FAIL: OpenAPI snapshot drift", file=sys.stderr)
        sp = set(snapshot.get("paths", {}))
        gp = set(generated.get("paths", {}))
        print(f"  snapshot-only paths: {sorted(sp - gp)}", file=sys.stderr)
        print(f"  generated-only paths: {sorted(gp - sp)}", file=sys.stderr)
        for p in sorted(sp & gp):
            if snapshot["paths"][p] != generated["paths"][p]:
                print(f"  path differs: {p}", file=sys.stderr)
        return 1
    print(f"PASS: OpenAPI snapshot matches route table ({len(generated['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
