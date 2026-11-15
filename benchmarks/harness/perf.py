#!/usr/bin/env python3
"""Performance gate (T043).

  python benchmarks/harness/perf.py --dataset fixtures/perf/manifest.json

Fixed dataset sizes (1k / 10k / 100k records, seeds 42 / 1729 / 20261108)
on this machine, with the hardware recorded in the report. Datasets are
bulk-loaded via SQL (load time is NOT measured); recall, finish, cold
start and concurrent-write behaviour are measured through the real API.
Thresholds come from the manifest (pre-registered); a threshold failure is
reported as FAIL and exits 1 — never silently re-run with a smaller
dataset. Numbers are experiment points on this machine, not a promised
upper bound.
"""
import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _p95(values):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(0.95 * len(s)))]


def _bulk_load(dbpath, n, seed):
    from turnkeep.storage import open_db, transaction
    from turnkeep.scopes import register_scope
    import random
    rng = random.Random(seed)
    db = open_db(dbpath)
    register_scope(db, "perf")
    rows = []
    for i in range(n):
        rows.append((f"r{i:06d}", "perf", f"k{i:06d}",
                     json.dumps(f"value {i} alpha beta gamma {rng.choice(['delta', 'epsilon', 'zeta'])}"),
                     "fact", "active", 1))
    with transaction(db, write=True):
        db.executemany("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       [(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                         "2026-11-08T00:00:00+00:00", "2026-11-08T00:00:00+00:00") for r in rows])
    db.close()
    return dbpath


def _measure(dbpath, n, samples=10):
    from turnkeep.storage import open_db
    from turnkeep.retrieval import recall
    from turnkeep.runs import begin
    from turnkeep.commits import finish
    db = open_db(dbpath)
    # Recall: 20 lexical queries for a term present in ~1/3 of the records.
    lat = []
    for i in range(20):
        t0 = time.monotonic()
        rows = recall(db, "perf", "alpha", limit=20)
        lat.append((time.monotonic() - t0) * 1000)
        assert rows, "recall returned nothing"
    # Finish: real begin+finish cycles on fresh keys (samples per dataset).
    flat = []
    for i in range(samples):
        t0 = time.monotonic()
        run = begin(db, "perf", f"perf task {i}")
        finish(db, "perf", run["run_id"], {"key": f"perf.out.{i}", "value": f"done {i}"})
        flat.append((time.monotonic() - t0) * 1000)
    size = Path(dbpath).stat().st_size
    db.close()
    return {"records": n, "recall_ms": {"p50": statistics.median(lat), "p95": _p95(lat), "max": max(lat)},
            "finish_ms": {"p50": statistics.median(flat), "p95": _p95(flat), "max": max(flat)},
            "db_bytes": size}


def _cold_start_ms():
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, 'src'); "
                        "from turnkeep.storage import open_db; "
                        "import tempfile, pathlib; "
                        "db = open_db(pathlib.Path(tempfile.mkdtemp()) / 'c.sqlite3'); db.close()"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"cold start failed: {r.stderr[-500:]}")
    return (time.monotonic() - t0) * 1000


def _concurrency(tmp, writers):
    import multiprocessing as mp
    dbpath = Path(tmp) / "conc.sqlite3"
    from turnkeep.storage import open_db
    from turnkeep.scopes import register_scope
    from turnkeep.memory import remember
    db = open_db(dbpath)
    register_scope(db, "conc")
    rec = remember(db, "conc", "shared", "v0", source_refs=[{"id": "u:1", "type": "user_statement"}])
    db.close()

    def _worker(i, q):
        sys.path.insert(0, str(ROOT / "src"))
        from turnkeep.storage import open_db
        from turnkeep.corrections import correct
        try:
            db = open_db(dbpath)
            correct(db, "conc", rec["id"], 1, f"v{i}",
                    source_refs=[{"id": "u:1", "type": "user_statement"}])
            q.put(("ok", i))
        except Exception as e:  # noqa: BLE001 - count the outcome, don't crash the harness
            code = getattr(e, "code", "")
            q.put((f"{type(e).__name__}:{code}:{str(e)[:60]}", i))

    ctx = mp.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(i, q)) for i in range(writers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    results = [q.get_nowait() for _ in range(writers)]
    # Exactly one writer wins; the rest must get VERSION_CONFLICT (expected).
    # Anything else (lock timeouts, busy errors, crashes) is a real failure.
    unexpected = [r for r in results
                  if r[0] != "ok" and not r[0].startswith("TurnkeepError:VERSION_CONFLICT")]
    return {"writers": writers, "won": sum(1 for r in results if r[0] == "ok"),
            "version_conflicts": sum(1 for r in results
                                     if r[0].startswith("TurnkeepError:VERSION_CONFLICT")),
            "busy_errors": unexpected}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    args = ap.parse_args()
    manifest = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    hardware = {"platform": platform.platform(), "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "memory_total_mb": None, "filesystem": str(Path.home().anchor)}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    hardware["memory_total_mb"] = int(line.split()[1]) // 1024
    except OSError:
        pass
    import sqlite3 as _s
    hardware["sqlite"] = _s.sqlite_version
    results, failures = [], []
    # /tmp is a 512M tmpfs on this VM; 100k begin() snapshots fill it.
    # Use a temp dir on the big disk instead (auto-removed on exit).
    tmp_root = ROOT / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tk-perf-", dir=str(tmp_root)) as tmp:
        for ds in manifest["datasets"]:
            path = _bulk_load(str(Path(tmp) / f"perf-{ds['records']}.sqlite3"),
                              ds["records"], ds["seed"])
            m = _measure(path, ds["records"], samples=ds.get("finish_samples", 10))
            results.append(m)
            if m["recall_ms"]["p95"] > manifest["recall_p95_ms_max"]:
                failures.append(f"recall p95 {m['recall_ms']['p95']:.1f}ms > "
                                f"{manifest['recall_p95_ms_max']}ms at {ds['records']} records")
            if m["finish_ms"]["p95"] > manifest["finish_p95_ms_max"]:
                failures.append(f"finish p95 {m['finish_ms']['p95']:.1f}ms > "
                                f"{manifest['finish_p95_ms_max']}ms at {ds['records']} records")
        cold = _cold_start_ms()
        if cold > manifest["cold_start_ms_max"]:
            failures.append(f"cold start {cold:.0f}ms > {manifest['cold_start_ms_max']}ms")
        conc = _concurrency(tmp, manifest["concurrent_writers"])
        if conc["busy_errors"]:
            failures.append(f"concurrency busy errors: {conc['busy_errors']}")
        if conc["won"] != 1:
            failures.append(f"concurrency: expected exactly 1 winner, got {conc['won']}")
    report = {"status": "PASS" if not failures else "FAIL", "failures": failures,
              "hardware": hardware, "thresholds": {
                  "recall_p95_ms_max": manifest["recall_p95_ms_max"],
                  "finish_p95_ms_max": manifest["finish_p95_ms_max"],
                  "cold_start_ms_max": manifest["cold_start_ms_max"]},
              "cold_start_ms": round(cold, 1), "concurrency": conc, "datasets": results,
              "note": "Fixed sizes on this machine; numbers are experiment points, not a promised upper bound."}
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
