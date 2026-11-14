#!/usr/bin/env python3
"""Record / verify a zero-key demo replay (T034).

  python scripts/record_demo.py --scenario export-format
  python scripts/record_demo.py --scenario export-format --verify

The replay file is examples/demo/<scenario>.replay.jsonl: one header line
(scenario, seed, label) plus one line per deterministic event. Each line
carries a chained digest: digest[i] = sha256(digest[i-1] + canonical(event)).
--verify does two independent checks:
  1. recompute the chain from the recorded file (any tampered line breaks it);
  2. re-run the demo in a fresh temp DB and compare the event sequence
     (ids/timestamps are excluded from events, so the comparison is stable).
Exit 0 PASS, 1 FAIL, 2 NOT_RUN (unknown scenario).
"""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from turnkeep.demo import run_demo  # noqa: E402

SEED = "turnkeep-demo-seed-1"


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chain(events):
    digest = hashlib.sha256(f"seed:{SEED}".encode()).hexdigest()
    lines = []
    for ev in events:
        digest = hashlib.sha256((digest + _canonical(ev)).encode()).hexdigest()
        lines.append({"digest": digest, "event": ev})
    return lines


def record(scenario, out_path):
    result = run_demo(scenario=scenario)
    events = result["events"]
    header = {"scenario": scenario, "seed": SEED,
              "label": "SCRIPTED_SIMULATION_NO_MODEL_CALLS",
              "event_count": len(events)}
    lines = [header] + _chain(events)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(_canonical(l) for l in lines) + "\n", encoding="utf-8")
    return {"recorded": str(out_path), "events": len(events), "scenario": scenario}


def verify(scenario, replay_path):
    if not replay_path.exists():
        print(f"FAIL: replay file missing: {replay_path}", file=sys.stderr)
        return 1
    lines = [json.loads(l) for l in replay_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    header, chained = lines[0], lines[1:]
    if header.get("scenario") != scenario or header.get("seed") != SEED:
        print("FAIL: replay header does not match scenario/seed", file=sys.stderr)
        return 1
    # 1. chain integrity
    digest = hashlib.sha256(f"seed:{SEED}".encode()).hexdigest()
    for line in chained:
        digest = hashlib.sha256((digest + _canonical(line["event"])).encode()).hexdigest()
        if digest != line["digest"]:
            print(f"FAIL: digest chain broken at event {line['event']}", file=sys.stderr)
            return 1
    # 2. fresh re-run reproduces the same event sequence
    fresh = run_demo(scenario=scenario)["events"]
    recorded = [l["event"] for l in chained]
    if fresh != recorded:
        print("FAIL: fresh re-run events differ from the recording", file=sys.stderr)
        for i, (a, b) in enumerate(zip(fresh, recorded)):
            if a != b:
                print(f"  first difference at step {i}: fresh={a} recorded={b}", file=sys.stderr)
                break
        return 1
    print(f"PASS: replay {scenario} verified ({len(recorded)} events, chain intact, re-run identical)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True,
                    choices=["j01", "export-format", "research-correction"])
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    replay = Path(args.out) if args.out else ROOT / "examples" / "demo" / f"{args.scenario}.replay.jsonl"
    if args.verify:
        return verify(args.scenario, replay)
    info = record(args.scenario, replay)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
