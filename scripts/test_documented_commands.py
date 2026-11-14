#!/usr/bin/env python3
"""Execute the offline commands documented in README.md /
docs/quickstart.md on a clean temporary fixture (T044).

  .venv/bin/python scripts/test_documented_commands.py --offline

Every step runs a real subprocess against a throwaway SQLite file in
a TemporaryDirectory and asserts on real behaviour (exit codes, JSON
fields, receipts, error codes) — never on string matching of the
docs. Any step failing fails the whole test (exit 1). --offline is
required: without it the script refuses to run (exit 2), so a bare
invocation can never be mistaken for a pass.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TIMEOUT = 120


class DocTest:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = tmp / "turnkeep.sqlite3"
        self.steps = 0
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(ROOT / "src") + ":" + env.get("PYTHONPATH", "")
        self.env = env

    def _run(self, argv, expect=0, note=""):
        """Run argv; expect an exit code (int) or 'nonzero'."""
        self.steps += 1
        label = note or " ".join(argv[2:6])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=TIMEOUT, cwd=ROOT, env=self.env)
        except subprocess.TimeoutExpired:
            print(f"FAIL [{self.steps}] {label}: timed out after {TIMEOUT}s")
            raise SystemExit(1)
        ok = (proc.returncode == expect) if isinstance(expect, int) \
            else (proc.returncode != 0)
        if not ok:
            print(f"FAIL [{self.steps}] {label}: exit {proc.returncode}, "
                  f"expected {expect}\nstdout: {proc.stdout[-2000:]}\n"
                  f"stderr: {proc.stderr[-2000:]}")
            raise SystemExit(1)
        print(f"ok [{self.steps}] {label} (exit {proc.returncode})")
        return proc

    def tk(self, *args, expect=0, note=""):
        """turnkeep CLI with --db <tmp db> and global --json before the
        subcommand (the form the README documents)."""
        return self._run([PY, "-m", "turnkeep", "--db", str(self.db),
                          "--json", *args], expect=expect, note=note)

    @staticmethod
    def _json(proc):
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(f"FAIL: expected JSON stdout, got: {proc.stdout[-2000:]}")
            raise SystemExit(1)

    def check(self, cond, label):
        self.steps += 1
        if not cond:
            print(f"FAIL [{self.steps}] {label}")
            raise SystemExit(1)
        print(f"ok [{self.steps}] {label}")

    def serve_smoke(self):
        import socket
        self.steps += 1
        label = "serve --scope demo + GET /api/v1/health (token required)"
        proc = subprocess.Popen(
            [PY, "-m", "turnkeep", "--db", str(self.db), "serve",
             "--scope", "demo", "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=ROOT, env=self.env)
        try:
            line = proc.stdout.readline()
            info = json.loads(line)
            url, token = info["url"], info["token"]
            assert url.startswith("http://127.0.0.1:"), info
            # No token -> 401.
            try:
                urllib.request.urlopen(url + "/api/v1/health", timeout=10)
                print(f"FAIL [{self.steps}] {label}: health without token "
                      "did not return 401")
                raise SystemExit(1)
            except urllib.error.HTTPError as e:
                assert e.code == 401, e.code
            req = urllib.request.Request(
                url + "/api/v1/health",
                headers={"X-Turnkeep-Token": token})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            assert resp.status == 200 and body.get("status") == "ok", body
            assert body.get("scope") == "demo", body
            print(f"ok [{self.steps}] {label} (health ok, 401 without token)")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="run the documented offline commands on a clean fixture")
    args = ap.parse_args()
    if not args.offline:
        print("refusing to run without --offline (no fixture, no pass)",
              file=sys.stderr)
        return 2
    for name in ("README.md", "README.zh-CN.md"):
        if not (ROOT / name).exists():
            print(f"FAIL: {name} missing")
            return 1

    with tempfile.TemporaryDirectory(prefix="turnkeep-doctest-") as td:
        t = DocTest(Path(td))

        # 1. Version + doctor (README install section).
        proc = t._run([PY, "-m", "turnkeep", "--version"], note="--version")
        t.check(proc.stdout.strip() == "turnkeep 0.1.0", "--version is 0.1.0")
        proc = t._run([PY, "-m", "turnkeep", "doctor", "--json"],
                      note="doctor --json")
        doctor = t._json(proc)
        t.check(doctor["python"]["status"] == "PASS", "doctor: python PASS")
        t.check(doctor["sqlite"]["status"] == "PASS", "doctor: sqlite PASS")

        # 2. First scope + first fact (quickstart §2).
        t.tk("init", "--scope", "demo", note="init --scope demo")
        proc = t.tk("remember", "--scope", "demo", "--key",
                    "build.python_version", "--value", "3.11",
                    "--kind", "requirement", note="remember 3.11")
        rec = t._json(proc)
        t.check(rec["state"] == "active" and rec["revision"] == 1,
                "remember returns active rev 1")
        rec_id, rec_rev = rec["id"], rec["revision"]

        # 3. Handover loop (quickstart §3): begin, correct, stale
        # finish rejected, resume, re-begin, finish with receipt.
        proc = t.tk("begin", "--scope", "demo", "--task", "prepare release",
                    note="begin run A")
        run = t._json(proc)
        run_a = run["run_id"]
        t.check(run["status"] == "open", "begin returns open run")
        proc = t.tk("correct", "--scope", "demo", "--id", rec_id,
                    "--expected-revision", str(rec_rev), "--value", "3.12",
                    note="correct 3.11 -> 3.12")
        new_rec = t._json(proc)
        t.check(new_rec["value"] == "3.12", "correct returns new value 3.12")
        proc = t.tk("finish", "--scope", "demo", "--run", run_a,
                    "--key", "release", "--value", "Python 3.11",
                    expect="nonzero", note="stale finish rejected")
        err = json.loads(proc.stderr)
        t.check(err["error"]["code"] == "STALE_SNAPSHOT",
                "stale finish error is STALE_SNAPSHOT")
        t.check("build.python_version" in
                err["error"]["details"]["changed_keys"],
                "STALE_SNAPSHOT names the changed key")
        proc = t.tk("resume", "--scope", "demo", "--run", run_a,
                    note="resume run A")
        diff = t._json(proc)
        t.check("build.python_version" in diff["changed_keys"],
                "resume diff names the changed key")
        proc = t.tk("begin", "--scope", "demo", "--task", "recompute release",
                    note="begin run B")
        run_b = t._json(proc)["run_id"]
        proc = t.tk("finish", "--scope", "demo", "--run", run_b,
                    "--key", "release", "--value", "Python 3.12",
                    "--idempotency-key", "demo-1", note="finish run B")
        receipt = t._json(proc)
        t.check(receipt["guarantee"] == "local_sqlite_commit_only",
                "receipt guarantee is local_sqlite_commit_only")
        # Identical replay returns the identical receipt.
        proc = t.tk("finish", "--scope", "demo", "--run", run_b,
                    "--key", "release", "--value", "Python 3.12",
                    "--idempotency-key", "demo-1",
                    note="idempotent finish replay")
        replay = t._json(proc)
        t.check(replay["record_id"] == receipt["record_id"]
                and replay["commit_epoch"] == receipt["commit_epoch"],
                "idempotent replay returns the original receipt")

        # 4. Recall + brief (quickstart §4).
        proc = t.tk("recall", "--scope", "demo", "--query", "python",
                    note="recall --query python")
        hits = t._json(proc)["records"]
        t.check(any(r["value"] == "3.12" for r in hits),
                "recall finds the corrected value")
        proc = t.tk("brief", "--scope", "demo", "--goal", "prepare release",
                    note="brief --goal")
        brief = t._json(proc)
        t.check(brief["scope"] == "demo" and not brief["budget_overrun"],
                "brief within budget")

        # 5. Conflict + full-candidate resolve (quickstart §5).
        proc = t.tk("remember", "--scope", "demo", "--key",
                    "build.python_version", "--value", "3.13",
                    "--kind", "requirement", note="remember 3.13 (conflict)")
        t.check(t._json(proc)["state"] == "conflicted",
                "conflicting remember is conflicted")
        proc = t.tk("recall", "--scope", "demo", "--query", "python",
                    "--history", note="recall --history (read candidates)")
        cands = [(r["id"], r["revision"]) for r in t._json(proc)["records"]
                 if r["key"] == "build.python_version"
                 and r["state"] == "conflicted"]
        t.check(len(cands) == 2, "two conflicted candidates to resolve")
        resolve_args = ["resolve", "--scope", "demo", "--key",
                        "build.python_version", "--value", "3.12",
                        "--source", "cli-1:user_statement"]
        for cid, crev in cands:
            resolve_args += ["--candidate", f"{cid}:{crev}"]
        proc = t.tk(*resolve_args, note="resolve with full candidate set")
        t.check(t._json(proc)["state"] == "active",
                "resolve returns active record")

        # 6. Export / import round-trip (README data section).
        out_dir = t.tmp / "demo-export"
        t._run([PY, "-m", "turnkeep", "--db", str(t.db), "export",
                "--scope", "demo", "--out", str(out_dir)],
               note="export --scope demo")
        t.check((out_dir / "manifest.json").exists()
                and (out_dir / "records.jsonl").exists(),
                "export writes manifest + records")
        fresh = t.tmp / "fresh.sqlite3"
        proc = t._run([PY, "-m", "turnkeep", "--db", str(fresh), "--json",
                       "import", "--format", "turnkeep", "--dry-run",
                       str(out_dir)], note="import --dry-run")
        t.check(t._json(proc)["dry_run"] is True, "import dry-run writes nothing")
        proc = t._run([PY, "-m", "turnkeep", "--db", str(fresh), "--json",
                       "import", "--format", "turnkeep", str(out_dir)],
                      note="import package")
        t.check(t._json(proc)["imported"] >= 1, "import imports records")

        # 7. Markdown import dry-run (README data section).
        notes = t.tmp / "notes.md"
        notes.write_text("## Decisions\n\n- Ship the exporter as tar.\n",
                         encoding="utf-8")
        proc = t._run([PY, "-m", "turnkeep", "--db", str(t.tmp / "md.sqlite3"),
                       "import", "--format",
                       "markdown", "--dry-run", str(notes)],
                      note="import --format markdown --dry-run")
        t.check(proc.returncode == 0, "markdown dry-run exit 0")

        # 8. Scripted demos + SDK handover + replay verify
        # (README 60-second demo; labelled scripted, no model calls).
        for scenario in ("export-format", "research-correction"):
            proc = t._run([PY, "-m", "turnkeep", "--db",
                           str(t.tmp / f"demo-{scenario}.sqlite3"), "demo",
                           "--scenario", scenario, "--no-model"],
                          note=f"demo --scenario {scenario} --no-model")
            t.check("SCRIPTED_SIMULATION_NO_MODEL_CALLS" in proc.stdout,
                    f"demo {scenario} is labelled scripted")
        proc = t._run([PY, "examples/python/handoff.py", "--temp-db"],
                      note="examples/python/handoff.py --temp-db")
        t.check("PASS: SDK two-agent handoff" in proc.stdout,
                "SDK handoff PASS")
        t._run([PY, "scripts/record_demo.py", "--scenario", "export-format",
                "--verify"], note="record_demo.py --verify")

        # 9. MCP smoke via the official SDK client (README MCP section).
        t._run([PY, "scripts/mcp_smoke.py", "--server",
                f"{PY} -m turnkeep mcp"], note="mcp_smoke.py")

        # 10. Workbench health with token auth (quickstart §6).
        t.serve_smoke()

    print(f"PASS: test_documented_commands --offline ({t.steps} checks, "
          "clean temp fixture)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
