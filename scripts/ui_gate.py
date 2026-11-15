#!/usr/bin/env python3
"""Workbench production gate (T035).

  python scripts/ui_gate.py --production-build

Checks, in order:
  1. `npm run build` succeeds and dist/index.html references assets with
     RELATIVE paths (./assets/...) for same-origin static deployment.
  2. dist contains no external CDN / remote URL (http:// or https://) —
     offline restart must not fetch anything remote.
  3. Full e2e suite runs against the PRODUCTION build (vite preview),
     not the dev server.
Writes evidence/ui/ui-gate.json. Exit 0 PASS, 1 FAIL, 2 NOT_RUN.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
EVIDENCE = ROOT / "evidence" / "ui" / "ui-gate.json"


def _run(argv, cwd=ROOT):
    r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=600, encoding="utf-8")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--production-build", action="store_true", required=True)
    args = ap.parse_args()
    checks = {}

    build = _run(["npm", "--prefix", "ui", "run", "build"])
    checks["build"] = {"exit_code": build.returncode,
                       "status": "PASS" if build.returncode == 0 else "FAIL"}
    if build.returncode != 0:
        print("FAIL: production build failed", file=sys.stderr)
        print(build.stdout[-2000:] + build.stderr[-2000:], file=sys.stderr)
        return 1

    index = (UI / "dist" / "index.html").read_text(encoding="utf-8")
    relative = "./assets/" in index and 'src="/assets/' not in index and 'href="/' not in index
    checks["relative_assets"] = {"status": "PASS" if relative else "FAIL",
                                 "detail": "dist/index.html uses ./assets/ (relative)" if relative
                                           else "absolute asset paths found"}

    external = []
    for f in sorted((UI / "dist").rglob("*")):
        if f.is_file() and f.suffix in (".js", ".css", ".html"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"https?://[^\s\"'<>]+", text):
                url = m.group(0)
                # localhost/127.0.0.1 references are local, not remote.
                if "localhost" not in url and "127.0.0.1" not in url:
                    external.append(f"{f.relative_to(UI / 'dist')}: {url}")
    checks["no_external_urls"] = {"status": "PASS" if not external else "FAIL",
                                  "external": external[:10]}

    e2e = _run(["npm", "--prefix", "ui", "run", "test:e2e:prod"])
    checks["e2e_production_build"] = {"exit_code": e2e.returncode,
                                      "status": "PASS" if e2e.returncode == 0 else "FAIL",
                                      "tail": (e2e.stdout + e2e.stderr)[-1500:]}

    overall = "PASS" if all(c["status"] == "PASS" for c in checks.values()) else "FAIL"
    record = {"task": "T035", "gate": "ui production build", "status": overall,
              "checks": checks,
              "notes": ["Automated a11y checks only — not a substitute for human usability validation.",
                        "No green 'connected' indicator exists in the UI; a dead API shows the error card, never a fake connected state."]}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": overall, "evidence": str(EVIDENCE)}, indent=2))
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
