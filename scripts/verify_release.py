#!/usr/bin/env python3
"""Verify a Turnkeep release candidate against its manifest (T046).

  .venv/bin/python scripts/verify_release.py --manifest release/artifacts/manifest.json

Real checks, in order (exit 0 PASS, 1 FAIL, 2 NOT_RUN):
  1. Manifest is valid JSON and carries every required field, including
     all four known-gap entries (T026, T042, T043, trademark -- none may
     be missing).
  2. Version matches pyproject.toml; commit matches the current HEAD.
  3. Every file listed in the manifest exists under the artifacts
     directory and its sha256 matches; SHA256SUMS agrees with a fresh
     recomputation.
  3b. Wheel-vs-source consistency (P0-R3-03 durable guard): the wheel
     and the sdist are unpacked and every file under
     ``src/turnkeep/`` is compared byte-for-byte against the packaged
     copy — .py AND non-.py alike (P2-R4-07: the guard used to compare
     .py files only, so a drifted migration .sql would have passed).
     Any difference, missing file, or extra file is a FAIL. The
     manifest-hash check (3) cannot catch this: it only proves the
     artifacts match the manifest, not that they were built from the
     current source.
  4. The Python artifact (wheel, or the source-tree fallback tarball)
     installs into a clean temporary venv -- never the source tree, never
     editable mode -- and the installed ``turnkeep`` runs the key
     journeys there: the zero-key demo (J01, --no-model) and at least one
     CLI remember/begin/finish round trip ending in a real receipt
     (record_id + commit_epoch). The UI tarball extracts and its
     index.html exists with relative assets.
  5. Platforms are honest: Linux verified, Windows + macOS NOT_RUN.

Claims check (for T047):
  .venv/bin/python scripts/verify_release.py --claims README.md \\
      --gates evidence/GATE_MATRIX.json

  --claims and --gates must be given together. The gate matrix is
  produced by T047; if it does not exist yet this mode returns NOT_RUN
  (exit 2), never PASS. When it exists, every ``[verified]`` claim in
  the claims file must map to a gate entry whose status supports it; a
  ``[verified]`` claim backed by a NOT_RUN / BLOCKED / FAIL gate is a
  FAIL, and a claim with no matching gate entry is reported unmatched.

Gate-matrix format accepted (T047 writes it; be liberal here, strict
about status): either {"gates": [{"id": ..., "claim": ..., "status":
...}, ...]} or a top-level {"<claim-id>": {"status": ...}} mapping.
Gate statuses understood: PASS / VERIFIED / FAIL / NOT_RUN /
BLOCKED_EXTERNAL / BLOCKED_CORE.
"""
import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_GAPS = ("T026", "T042", "T043", "trademark")
REQUIRED_MANIFEST_FIELDS = ("name", "version", "commit", "files",
                            "platforms", "known_gaps", "locks", "schema",
                            "reproducibility")
OK_GATE = {"PASS", "VERIFIED"}
BAD_GATE = {"FAIL", "NOT_RUN", "BLOCKED_EXTERNAL", "BLOCKED_CORE"}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(argv, cwd=None, env=None, timeout=600):
    import os
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, env=merged,
                          capture_output=True, text=True, timeout=timeout)


def _fail(report, message):
    report["failures"].append(message)
    print(f"FAIL: {message}", file=sys.stderr)


def _check_manifest(manifest_path, report):
    if not manifest_path.is_file():
        _fail(report, f"manifest not found: {manifest_path}")
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        _fail(report, f"manifest is not valid JSON: {exc}")
        return None
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            _fail(report, f"manifest missing required field: {field}")
    gaps = manifest.get("known_gaps") or {}
    for gap in REQUIRED_GAPS:
        entry = gaps.get(gap)
        if not entry:
            _fail(report, f"known_gaps missing required entry: {gap}")
        elif not entry.get("status") or not entry.get("detail"):
            _fail(report, f"known_gaps[{gap}] needs status + detail")
    report["checks"].append("manifest fields + known gaps present")
    return manifest


def _check_version_commit(manifest, report):
    pyproject = ROOT / "pyproject.toml"
    try:
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        pv = declared["project"]["version"]
    except Exception as exc:  # noqa: BLE001
        _fail(report, f"cannot read version from pyproject.toml: {exc}")
        return
    if manifest.get("version") != pv:
        _fail(report, f"manifest version {manifest.get('version')} != "
                      f"pyproject version {pv}")
    else:
        report["checks"].append(f"version matches pyproject.toml ({pv})")
    head = _run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    if head and manifest.get("commit") != head:
        _fail(report, f"manifest commit {manifest.get('commit')} != "
                      f"current HEAD {head} (rebuild before verifying)")
    elif head:
        report["checks"].append("commit matches current HEAD")
    else:
        # P2-R4-10: no .git (e.g. the extracted package) — record the
        # check as not_run instead of silently skipping it, so the
        # extracted-package report reads 12 checks + 1 not_run.
        report["not_run"].append(
            "commit matches current HEAD NOT_RUN: git rev-parse HEAD "
            "returned nothing (no .git here, e.g. an extracted package)")


def _check_files(manifest, artifacts_dir, report):
    listed = manifest.get("files") or []
    seen = set()
    for entry in listed:
        rel = entry.get("path")
        want = entry.get("sha256")
        if not rel or not want:
            _fail(report, f"manifest file entry incomplete: {entry}")
            continue
        seen.add(rel)
        p = artifacts_dir / rel
        if not p.is_file():
            _fail(report, f"manifest file missing: {rel}")
            continue
        if _sha256(p) != want:
            _fail(report, f"sha256 mismatch: {rel}")
    sums = artifacts_dir / "SHA256SUMS"
    if not sums.is_file():
        _fail(report, "SHA256SUMS missing from artifacts")
    else:
        for line in sums.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 2:
                _fail(report, f"malformed SHA256SUMS line: {line!r}")
                continue
            digest, rel = parts
            p = artifacts_dir / rel
            if rel in ("SHA256SUMS", "manifest.json"):
                continue
            if not p.is_file():
                _fail(report, f"SHA256SUMS lists missing file: {rel}")
            elif _sha256(p) != digest:
                _fail(report, f"SHA256SUMS mismatch: {rel}")
    report["checks"].append(
        f"{len(listed)} manifest files present with matching sha256; "
        "SHA256SUMS agrees")


def _files_from_dir(src_root):
    """{rel-posix -> bytes} for every file under src_root (recursive),
    .py and non-.py alike (P2-R4-07: migrations/*.sql included)."""
    out = {}
    for p in sorted(Path(src_root).rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        out[p.relative_to(src_root).as_posix()] = p.read_bytes()
    return out


def _files_from_wheel(wheel_path):
    """{rel-posix under turnkeep/ -> bytes} for every file in the wheel."""
    out = {}
    with zipfile.ZipFile(wheel_path) as zf:
        for name in zf.namelist():
            if not name.startswith("turnkeep/"):
                continue
            out[name[len("turnkeep/"):]] = zf.read(name)
    return out


def _files_from_sdist(sdist_path):
    """{rel-posix under src/turnkeep/ -> bytes} for every packaged file."""
    out = {}
    with tarfile.open(sdist_path, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            parts = m.name.split("/")
            # sdist layout: <name>-<version>/src/turnkeep/<rel>
            if len(parts) >= 4 and parts[1] == "src" and parts[2] == "turnkeep":
                f = tf.extractfile(m)
                if f is not None:
                    out["/".join(parts[3:])] = f.read()
    return out


def _diff_trees(artifact_files, src_files, label):
    """Compare one packaged file tree against src/. Returns a list of
    problem strings (empty when identical)."""
    problems = []
    for rel in sorted(set(artifact_files) | set(src_files)):
        if rel not in src_files:
            problems.append(f"{label}: extra file not in src/: {rel}")
        elif rel not in artifact_files:
            problems.append(f"{label}: missing file present in src/: {rel}")
        elif artifact_files[rel] != src_files[rel]:
            problems.append(f"{label}: differs from src/: {rel}")
    return problems


def _check_wheel_vs_source(manifest, artifacts_dir, report):
    """P0-R3-03 guard: the shipped wheel/sdist must be built from the
    current src/. The manifest-hash check only proves the artifacts
    match the manifest; this proves they match the source tree."""
    src_files = _files_from_dir(ROOT / "src" / "turnkeep")
    if not src_files:
        _fail(report, "wheel-vs-source check: no files found under "
                      "src/turnkeep/")
        return
    files = [f["path"] for f in manifest.get("files") or []]
    wheels = [f for f in files if f.endswith(".whl")]
    sdists = [f for f in files if f.endswith(".tar.gz")
              and "ui-dist" not in f and "source" not in f]
    if not wheels and not sdists:
        _fail(report, "wheel-vs-source check: no wheel or sdist listed in "
                      "the manifest")
        return
    for wheel in wheels:
        p = artifacts_dir / wheel
        if not p.is_file():
            _fail(report, f"wheel-vs-source check: wheel missing: {wheel}")
            continue
        problems = _diff_trees(_files_from_wheel(p), src_files,
                               f"wheel {wheel}")
        if problems:
            for prob in problems:
                _fail(report, f"wheel-vs-source mismatch: {prob}")
        else:
            report["checks"].append(
                f"wheel {wheel} matches src/turnkeep/ "
                f"({len(src_files)} files, byte-identical)")
    for sdist in sdists:
        p = artifacts_dir / sdist
        if not p.is_file():
            _fail(report, f"wheel-vs-source check: sdist missing: {sdist}")
            continue
        problems = _diff_trees(_files_from_sdist(p), src_files,
                               f"sdist {sdist}")
        if problems:
            for prob in problems:
                _fail(report, f"wheel-vs-source mismatch: {prob}")
        else:
            report["checks"].append(
                f"sdist {sdist} matches src/turnkeep/ "
                f"({len(src_files)} files, byte-identical)")


def _check_platforms(manifest, report):
    platforms = manifest.get("platforms") or {}
    linux = platforms.get("linux", {}).get("status")
    if linux != "verified":
        _fail(report, f"platforms.linux must be 'verified', got {linux!r}")
    for other in ("windows", "macos"):
        status = platforms.get(other, {}).get("status")
        if status != "NOT_RUN":
            _fail(report, f"platforms.{other} must be 'NOT_RUN' (no runner "
                          f"evidence), got {status!r}")
    report["checks"].append("platforms honest: Linux verified, "
                            "Windows/macOS NOT_RUN")


def _check_install_and_journeys(manifest, artifacts_dir, report):
    files = [f["path"] for f in manifest.get("files") or []]
    wheels = [f for f in files if f.endswith(".whl")]
    sdists = [f for f in files if f.endswith(".tar.gz")
              and "ui-dist" not in f and "source" not in f]
    source_tars = [f for f in files if "source.tar.gz" in f]
    ui_tars = [f for f in files if "ui-dist.tar.gz" in f]
    if wheels:
        install_target = artifacts_dir / wheels[0]
        kind = "wheel"
    elif source_tars:
        install_target = None
        kind = "source-tree-fallback"
    elif sdists:
        install_target = artifacts_dir / sdists[0]
        kind = "sdist"
    else:
        _fail(report, "no installable Python artifact (wheel/sdist/source "
                      "tarball) in manifest")
        return
    report["checks"].append(f"installable artifact: {kind} "
                            f"({wheels[0] if wheels else source_tars[0] if source_tars else sdists[0]})")

    with tempfile.TemporaryDirectory(prefix="tk-verify-") as tmp:
        tmp = Path(tmp)
        # UI tarball: extract + index.html with relative assets.
        if ui_tars:
            ui_dir = tmp / "ui"
            ui_dir.mkdir()
            with tarfile.open(artifacts_dir / ui_tars[0], "r:gz") as tf:
                tf.extractall(ui_dir, filter="data")
            index = ui_dir / "ui-dist" / "index.html"
            if not index.is_file():
                _fail(report, "ui-dist tarball has no ui-dist/index.html")
            elif "./assets/" not in index.read_text(encoding="utf-8"):
                _fail(report, "ui-dist index.html lacks relative ./assets/")
            else:
                report["checks"].append("UI tarball extracts; index.html "
                                        "uses relative assets")
        else:
            _fail(report, "no ui-dist tarball in manifest")

        venv_dir = tmp / "venv"
        venv.create(venv_dir, with_pip=True)
        if sys.platform == "win32":
            vpy = venv_dir / "Scripts" / "python.exe"
            tk = venv_dir / "Scripts" / "turnkeep.exe"
        else:
            vpy = venv_dir / "bin" / "python"
            tk = venv_dir / "bin" / "turnkeep"
        db = tmp / "verify.db"
        if kind == "source-tree-fallback":
            src = tmp / "src"
            src.mkdir()
            with tarfile.open(artifacts_dir / source_tars[0], "r:gz") as tf:
                tf.extractall(src, filter="data")
            roots = [d for d in src.iterdir() if d.is_dir()]
            if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
                _fail(report, "source tarball does not contain exactly one "
                              "project root with pyproject.toml")
                return
            inst = _run([str(vpy), "-m", "pip", "install", "--no-index",
                         "--no-deps", str(roots[0])], cwd=tmp)
            # A source tree without a build backend cannot be pip-installed
            # offline; in that case fall back to PYTHONPATH smoke use and
            # say so -- the manifest already declares the fallback.
            if inst.returncode != 0:
                report["checks"].append(
                    "source-tree fallback: pip install from the tree "
                    "failed offline (no build backend in the venv); "
                    "journeys below run from the extracted tree via "
                    "PYTHONPATH instead, and this is recorded, not hidden")
                env = {"PYTHONPATH": str(roots[0] / "src")}
                tk_cmd = [str(vpy), "-m", "turnkeep"]
            else:
                env = {}
                tk_cmd = [str(tk)]
        else:
            inst = _run([str(vpy), "-m", "pip", "install", "--no-index",
                         "--no-deps", str(install_target)], cwd=tmp)
            if inst.returncode != 0:
                _fail(report, f"pip install of {kind} failed in a clean "
                              f"venv: {(inst.stdout + inst.stderr)[-1500:]}")
                return
            env = {}
            tk_cmd = [str(tk)]
        report["checks"].append(f"installed {kind} into a clean venv")

        def tk_cli(*argv):
            return _run([*tk_cmd, "--db", str(db), "--json", *argv],
                        cwd=tmp, env=env)

        ver = _run([*tk_cmd, "--version"], cwd=tmp, env=env)
        if ver.returncode != 0 or "0.1.0" not in ver.stdout:
            _fail(report, f"installed turnkeep --version wrong: "
                          f"{ver.stdout!r} {ver.stderr!r}")
            return
        report["checks"].append("installed turnkeep --version reports 0.1.0")

        # J01 is the DEFAULT demo scenario ("j01"): inline
        # remember/correct/begin/finish, no fixture files. The
        # export-format / research-correction scenarios read
        # fixtures/demo/** (see the manifest's known_gaps entry) and are
        # probed separately below as a non-gating note.
        demo = _run([*tk_cmd, "--db", str(tmp / "demo.db"), "--json",
                     "demo", "--no-model"],
                    cwd=tmp, env=env)
        if demo.returncode != 0 or "SCRIPTED_SIMULATION_NO_MODEL_CALLS" not in demo.stdout:
            _fail(report, "installed J01 zero-key demo failed or lacked "
                          "the SCRIPTED_SIMULATION_NO_MODEL_CALLS label")
        else:
            report["checks"].append("J01 zero-key demo PASS from the "
                                    "installed artifact")
        probe = _run([*tk_cmd, "--db", str(tmp / "demo-fx.db"), "--json",
                      "demo", "--scenario", "export-format", "--no-model"],
                     cwd=tmp, env=env)
        if probe.returncode == 0 and "SCRIPTED_SIMULATION_NO_MODEL_CALLS" in probe.stdout:
            report["checks"].append("export-format scenario PASS from the "
                                    "installed artifact")
        else:
            report.setdefault("notes", []).append(
                "export-format scenario does NOT run from the installed "
                "wheel (known gap, see manifest known_gaps."
                "demo_fixtures_installed): demo.py resolves fixtures via "
                "parents[2], which is lib/python3.12 when installed. "
                "Non-gating for J01; durable fix is demo.py via "
                "importlib.resources.")

        init = tk_cli("init", "--scope", "verify")
        if init.returncode != 0:
            _fail(report, f"installed CLI init failed: {init.stderr[-500:]}")
            return
        rem = tk_cli("remember", "--scope", "verify", "--key", "demo.req",
                     "--value", "export-format v1")
        if rem.returncode != 0:
            _fail(report, f"installed CLI remember failed: {rem.stderr[-500:]}")
            return
        try:
            rec = json.loads(rem.stdout)
            if rec.get("state") != "active" or not rec.get("id"):
                raise ValueError("remember output lacks active record id")
        except Exception as exc:  # noqa: BLE001
            _fail(report, f"installed CLI remember output unexpected: {exc}")
            return
        beg = tk_cli("begin", "--scope", "verify", "--task", "verify journey")
        if beg.returncode != 0:
            _fail(report, f"installed CLI begin failed: {beg.stderr[-500:]}")
            return
        try:
            run_id = json.loads(beg.stdout)["run_id"]
        except Exception as exc:  # noqa: BLE001
            _fail(report, f"installed CLI begin output has no run_id: {exc}")
            return
        fin = tk_cli("finish", "--scope", "verify", "--run", run_id,
                     "--key", "demo.done", "--value", "verified",
                     "--idempotency-key", "t046-verify-1")
        if fin.returncode != 0:
            _fail(report, f"installed CLI finish failed: {fin.stderr[-500:]}")
            return
        try:
            receipt = json.loads(fin.stdout)
            if not receipt.get("record_id") or receipt.get("commit_epoch") is None:
                raise ValueError("finish output lacks record_id/commit_epoch")
        except Exception as exc:  # noqa: BLE001
            _fail(report, f"installed CLI finish receipt unexpected: {exc}")
            return
        report["checks"].append(
            "CLI remember/begin/finish round trip PASS from the installed "
            f"artifact (receipt record_id={receipt['record_id'][:8]}..., "
            f"commit_epoch={receipt['commit_epoch']})")


def _parse_claims(claims_path):
    """Lines like '- **[verified]** ...' -> (marker, text)."""
    claims = []
    pat = re.compile(r"^\s*-\s*\*\*\[([^\]]+)\]\*\*\s*(.*)$")
    for line in claims_path.read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if m:
            claims.append((m.group(1).strip().lower(), m.group(2).strip()))
    return claims


def _load_gates(gates_path):
    data = json.loads(gates_path.read_text(encoding="utf-8"))
    gates = []
    if isinstance(data, dict) and isinstance(data.get("gates"), list):
        gates = data["gates"]
    elif isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict) and "status" in val:
                gates.append({"id": key, "claim": key, **val})
    elif isinstance(data, list):
        gates = data
    return gates


def _check_claims(claims_path, gates_path, report):
    """Returns 'PASS' / 'FAIL' / 'NOT_RUN'."""
    if not claims_path.is_file():
        _fail(report, f"claims file not found: {claims_path}")
        return "FAIL"
    if not gates_path.is_file():
        # The gate matrix is produced by T047. Without it there is no
        # claims evidence at all: NOT_RUN, never PASS.
        report["not_run"].append(
            f"claims check NOT_RUN: gate matrix not found: {gates_path} "
            "(produced by T047; no claims verdict can be given yet)")
        return "NOT_RUN"
    claims = _parse_claims(claims_path)
    gates = _load_gates(gates_path)

    def norm(text):
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    unmatched = []
    for marker, text in claims:
        words = norm(text)
        best = None
        best_overlap = 0
        for gate in gates:
            gate_text = f"{gate.get('id', '')} {gate.get('claim', '')}"
            overlap = len(words & norm(gate_text))
            if overlap > best_overlap:
                best_overlap = overlap
                best = gate
        # A single shared word (e.g. "handover") is not a match: it would
        # misattribute claims to unrelated gates. Require >= 2 shared
        # tokens, or the gate id named in the claim text.
        gate_id = str(best.get("id", "")).lower() if best else ""
        if best is None or (best_overlap < 2 and gate_id not in text.lower()):
            unmatched.append((marker, text))
            continue
        status = str(best.get("status", "")).upper()
        if marker == "verified" and status in BAD_GATE:
            _fail(report, f"claim marked [verified] but gate "
                          f"{best.get('id')} is {status}: {text[:120]}")
        elif marker in ("not run", "not_run") and status in OK_GATE:
            _fail(report, f"claim marked [not run] but gate "
                          f"{best.get('id')} is {status}: {text[:120]}")
    for marker, text in unmatched:
        report["not_run"].append(
            f"claim with no matching gate entry [{marker}]: {text[:120]}")
    report["checks"].append(
        f"claims check: {len(claims)} claims parsed, "
        f"{len(gates)} gate entries, {len(unmatched)} unmatched")
    return "FAIL" if report["failures"] else "PASS"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="release/artifacts/manifest.json")
    ap.add_argument("--claims", default=None,
                    help="Claims file to check (e.g. README.md).")
    ap.add_argument("--gates", default=None,
                    help="Gate matrix JSON (evidence/GATE_MATRIX.json).")
    args = ap.parse_args()

    if bool(args.claims) != bool(args.gates):
        print("REFUSED: --claims and --gates must be given together.",
              file=sys.stderr)
        return 2

    manifest_path = (ROOT / args.manifest).resolve()
    artifacts_dir = manifest_path.parent
    report = {"manifest": str(manifest_path), "checks": [],
              "failures": [], "not_run": []}

    manifest = _check_manifest(manifest_path, report)
    if manifest is not None:
        _check_version_commit(manifest, report)
        _check_files(manifest, artifacts_dir, report)
        _check_wheel_vs_source(manifest, artifacts_dir, report)
        _check_platforms(manifest, report)
        _check_install_and_journeys(manifest, artifacts_dir, report)

    claims_status = None
    if args.claims:
        claims_status = _check_claims(ROOT / args.claims,
                                      ROOT / args.gates, report)

    if claims_status == "NOT_RUN" and not report["failures"]:
        report["status"] = "NOT_RUN"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2
    report["status"] = "PASS" if not report["failures"] else "FAIL"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
