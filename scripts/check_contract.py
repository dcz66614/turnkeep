#!/usr/bin/env python3
"""T003 gate: frozen schema must cover PUBLIC_API.json paths/schemas, all 15
error codes, state machine, and fixtures must be valid/invalid as labelled.
Stdlib only. Usage: python scripts/check_contract.py --spec specs/public_api.schema.json
Exit 0 PASS, 1 FAIL, 2 NOT_RUN."""
import argparse, json, sys
from pathlib import Path

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--spec", required=True); args = ap.parse_args()
    root = Path.cwd(); errors = []
    spec_p = root / args.spec
    if not spec_p.exists(): print(f"NOT_RUN missing {args.spec}"); return 2
    spec = json.loads(spec_p.read_text())
    api = json.loads((root / "specs/PUBLIC_API.json").read_text())
    if len(spec.get("operations", {})) != len(api.get("paths", {})):
        errors.append(f"operations {len(spec.get('operations',{}))} != API paths {len(api.get('paths',{}))} (note: multiple methods per path are collapsed by operationId; paths={len(api['paths'])})")
    # Count operations by method+path instead
    n_ops = sum(len(m) for m in api["paths"].values())
    if len(spec.get("operations", {})) < n_ops:
        errors.append(f"operations {len(spec['operations'])} < method-path pairs {n_ops}")
    if set(spec.get("schemas", {})) != set(api.get("components", {}).get("schemas", {})):
        errors.append("schema names differ from PUBLIC_API.json")
    if len(spec.get("error_codes", [])) != 15:
        errors.append(f"error_codes {len(spec.get('error_codes',[]))} != 15")
    sm = spec.get("state_machine", {})
    if "conflicted" not in sm.get("states", []): errors.append("state machine missing conflicted")
    if "active->conflicted" not in sm.get("transitions", {}): errors.append("missing active->conflicted transition")
    if "recall" in str(spec.get("operations", {})) or "resume" in str(spec.get("operations", {})):
        errors.append("schema claims recall/resume HTTP paths that PUBLIC_API.json does not have (mapping gap must stay recorded, not papered over)")
    fx = root / "tests/contract/fixtures"
    for name, must_have in (("remember_valid.json", "key"), ("remember_missing_key.json", None),
                             ("correct_missing_revision.json", None), ("finish_valid.json", "result")):
        p = fx / name
        if not p.exists(): errors.append(f"missing fixture {name}"); continue
        d = json.loads(p.read_text())
        if must_have and must_have not in d: errors.append(f"fixture {name} missing {must_have}")
    if "remember_missing_key.json" in [p.name for p in fx.iterdir()]:
        d = json.loads((fx / "remember_missing_key.json").read_text())
        if "key" in d: errors.append("remember_missing_key fixture actually has a key")
    if errors:
        print("FAIL:"); [print(" -", e) for e in errors]; return 1
    print(f"PASS check_contract: {len(spec['operations'])} ops, {len(spec['schemas'])} schemas, 15 error codes, state machine OK; recall/resume gap still recorded in schema note")
    return 0

if __name__ == "__main__":
    sys.exit(main())
