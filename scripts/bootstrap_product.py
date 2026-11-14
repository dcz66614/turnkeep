#!/usr/bin/env python3
"""T004 bootstrap: verify the product core imports and demos in a controlled
temp dir. Stdlib only. Missing optional deps -> report, still run pure core.
Usage: python scripts/bootstrap_product.py [--root .]
Exit 0 = core OK, 1 = core broken, 2 = NOT_RUN (missing src)."""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--root", default=".")
    args = ap.parse_args(); root = Path(args.root).resolve()
    src = root / "src"
    if not (src / "turnkeep" / "store.py").exists():
        print("NOT_RUN: src/turnkeep/store.py missing"); return 2
    env = {"PYTHONPATH": str(src), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    import os
    env.update({k: v for k, v in os.environ.items() if k in ("HOME", "LANG", "LC_ALL", "TMPDIR")})
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, "-m", "turnkeep", "demo"], cwd=td,
                           env=env, capture_output=True, text=True, timeout=120)
        try:
            demo = json.loads(r.stdout)
            ok = demo.get("demonstration") == "SCRIPTED_SIMULATION_NO_MODEL_CALLS" and demo.get("idempotent_replay_identical") is True
        except Exception:
            ok = False
        print(f"python={sys.version.split()[0]} demo_exit={r.returncode} demo_ok={ok}")
        if r.returncode != 0 or not ok:
            print(r.stdout[-2000:]); print(r.stderr[-2000:]); return 1
    print("PASS bootstrap: core imports + demo OK (offline, no model key, no network to model services)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
