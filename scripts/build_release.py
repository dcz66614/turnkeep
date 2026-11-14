#!/usr/bin/env python3
"""Repeatable release-candidate build (T046).

  .venv/bin/python scripts/build_release.py --clean --output release/artifacts

What it does, in order:
  1. Records the exact source state (HEAD commit, tracked-tree cleanliness,
     Python / SQLite / Node versions). A dirty *tracked* tree fails the
     build; untracked files are recorded, not hidden.
  2. Builds the Python wheel + sdist with the official ``build`` frontend
     (``python -m build``) against ``pyproject.toml``, with
     SOURCE_DATE_EPOCH pinned to the HEAD commit timestamp so the two
     reproducibility builds can be compared honestly. If the ``build``
     toolchain is missing it is installed once into the current venv
     (recorded in the manifest); if that install fails, the build falls
     back to a deterministic source-tree tarball + lock files and says so
     in the manifest -- it never fabricates a wheel. pyproject.toml
     declares the runtime package-data (migrations/*.sql, P2-R4-09), so
     a plain rebuild from the sdist produces an equivalent wheel; the
     build script's post-hoc injection of those files into wheel and
     sdist is kept as a safety net and is a no-op when they are already
     present (it used to be the only thing putting them in, because
     pyproject declared no package-data).
  3. Builds the static UI (``npm --prefix ui run build``) and packs
     ``ui/dist`` into a deterministic ``ui-dist.tar.gz`` (sorted entries,
     fixed mtime/uid/gid). ``node_modules`` is never packed.
  4. Copies the lock files: ``requirements/base.lock``,
     ``requirements/mcp.lock`` and ``ui/package-lock.json``.
     NOTE: ``requirements/ui.lock`` is an intentionally empty R0
     placeholder (one comment line); it is NOT the UI lock source and is
     NOT copied as a lock. The UI lock source is ``ui/package-lock.json``.
  5. Builds everything TWICE and compares hashes. ``reproducible`` in
     the manifest is true only if both builds are bit-for-bit identical;
     otherwise the differing files and the reason are recorded and the
     build does not claim reproducibility. (Setuptools stamps sdist
     generated files + the gzip header with wall-clock time, so the
     sdist is timestamp-normalised to SOURCE_DATE_EPOCH first -- contents
     unchanged -- and that normalisation is recorded in the manifest.)
  6. Writes ``SHA256SUMS`` and ``release/artifacts/manifest.json``
     (version 0.1.0, commit, dependencies, platforms, schema v1
     compatibility, known gaps).

What it never does by default: upload anywhere. ``--upload`` requires
both an explicit ``--upload-target`` AND the environment variable
``TURNKEEP_RELEASE_UPLOAD_AUTHORIZED=1``; without both it refuses loudly
(exit 2) instead of silently pretending a release happened. Even with
both, there is currently no upload backend configured for this
candidate, so it still refuses -- the point is that no remote action
happens without an explicit, authorised, implemented path.

Excluded from every artifact, always: development caches
(``__pycache__``, ``.pytest_cache``), ``node_modules``, ``.venv``,
databases (``*.db``/``*.sqlite*``), ``.tmp``, ``state/``,
``evidence/`` (real evidence may contain machine paths and sensitive
content), ``.git`` and the output directory itself.

Exit codes: 0 built, 1 build failure, 2 refused / not-run (dirty tracked
tree, upload refused, missing prerequisite that cannot be worked around).
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
CONTRACT_VERSION = "0.1"
SCHEMA_VERSION = "v1"

EXCLUDE_DIRS = {".venv", "node_modules", ".git", "__pycache__",
                ".pytest_cache", ".tmp", "test-results",
                "playwright-report", "ui/dist", "release"}
EXCLUDE_TOP_DIRS = {"state", "evidence"}
EXCLUDE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".pyc"}


def _run(argv, cwd=ROOT, env=None, timeout=900):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(argv, cwd=str(cwd), env=merged,
                          capture_output=True, text=True, timeout=timeout)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args):
    r = _run(["git", *args])
    return r.stdout.strip() if r.returncode == 0 else None


def _deterministic_tarball(src_dir, out_path, arcname_prefix):
    """Tar.gz with sorted entries, fixed mtime/uid/gid/mode for files."""
    files = []
    for p in sorted(src_dir.rglob("*")):
        if p.is_file():
            files.append(p)
    with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for p in files:
            rel = p.relative_to(src_dir).as_posix()
            data = p.read_bytes()
            ti = tarfile.TarInfo(f"{arcname_prefix}/{rel}")
            ti.size = len(data)
            ti.mtime = 0
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return files


def _source_tree_files():
    """All files that belong in the source-tree fallback artifact."""
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        parts = rel.parts
        if parts[0] in EXCLUDE_TOP_DIRS:
            continue
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        if p.suffix in EXCLUDE_SUFFIXES:
            continue
        if rel.as_posix() == "requirements/ui.lock":
            continue  # empty placeholder, never a lock; noted in manifest
        out.append(p)
    return out


def _deterministic_source_tarball(out_path):
    files = _source_tree_files()
    with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            data = p.read_bytes()
            ti = tarfile.TarInfo(f"turnkeep-{VERSION}/{rel}")
            ti.size = len(data)
            ti.mtime = 0
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return files


def _build_python_once(workdir, source_date_epoch):
    """One wheel+sdist build into workdir/dist. Returns (ok, log, kind).

    kind is 'wheel+sdist' on success, 'source-tree-fallback' if the
    toolchain could not be obtained at all (never a fake wheel).
    """
    dist = workdir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    env = {"SOURCE_DATE_EPOCH": str(source_date_epoch),
           "PYTHONHASHSEED": "0"}
    probe = _run([sys.executable, "-c", "import build, setuptools, wheel"])
    toolchain = {"present": probe.returncode == 0, "installed_now": False,
                 "install_log": None}
    if probe.returncode != 0:
        inst = _run([sys.executable, "-m", "pip", "install",
                     "build", "setuptools", "wheel"], timeout=600)
        toolchain["installed_now"] = inst.returncode == 0
        toolchain["install_log"] = (inst.stdout + inst.stderr)[-2000:]
        if inst.returncode != 0:
            return False, toolchain, "source-tree-fallback"
    r = _run([sys.executable, "-m", "build", "--outdir", str(dist)],
             env=env, timeout=900)
    if r.returncode != 0:
        return False, {"toolchain": toolchain,
                       "build_stdout": r.stdout[-2000:],
                       "build_stderr": r.stderr[-2000:]}, "wheel+sdist"
    return True, {"toolchain": toolchain}, "wheel+sdist"


def _find_demo_fixtures():
    """Zero-key demo fixtures (fixtures/demo/**). demo.py resolves them
    as Path(__file__).parents[2] / 'fixtures' / 'demo' -- the repo root
    in a source checkout, but the parent of site-packages (lib/python3.12)
    in an installed wheel, a location no wheel layout can populate. So
    these are sdist-only entries (wheel_arcname None): injecting them
    into the sdist makes a source checkout unpacked from the sdist able
    to run the fixture scenarios, while the installed wheel's
    export-format / research-correction scenarios remain broken until
    demo.py loads fixtures via importlib.resources (core-owned fix, see
    manifest known_gaps.demo_fixtures_installed). Returns a list of
    (source_path, wheel_arcname, sdist_arcname)."""
    found = []
    base = ROOT / "fixtures" / "demo"
    if not base.is_dir():
        return found
    for p in sorted(base.rglob("*")):
        if p.is_file():
            rel = p.relative_to(ROOT).as_posix()  # fixtures/demo/...
            found.append((p, None, f"turnkeep-{VERSION}/{rel}"))
    return found


def _find_package_data():
    """Non-Python files under src/turnkeep that the runtime reads from the
    installed package directory (e.g. storage.py reads
    migrations/001_initial.sql via Path(__file__).parent). setuptools
    ships these because pyproject.toml declares package-data (P2-R4-09);
    this helper still lists them so build_release.py can inject any
    that a stock build missed, as a safety net. Returns a list of
    (source_path, wheel_arcname, sdist_arcname)."""
    found = []
    pkg = ROOT / "src" / "turnkeep"
    for p in sorted(pkg.rglob("*")):
        if not p.is_file() or p.suffix == ".py":
            continue
        if "__pycache__" in p.parts:
            continue
        # Wheel arcname is the installed layout (turnkeep/...); sdist
        # arcname is the repo layout (src/turnkeep/...) -- they differ,
        # and using the wheel layout in the sdist would strand the file
        # at turnkeep-0.1.0/turnkeep/... where no build looks for it.
        wheel_arc = p.relative_to(ROOT / "src").as_posix()
        sdist_arc = f"turnkeep-{VERSION}/{p.relative_to(ROOT).as_posix()}"
        found.append((p, wheel_arc, sdist_arc))
    return found


def _repair_wheel(wheel_path, epoch, package_data):
    """Inject missing package-data files into a built wheel and update
    RECORD. Returns the list of injected arcnames. The wheel is rewritten
    deterministically (injected entries use SOURCE_DATE_EPOCH; existing
    entries keep their bytes and metadata)."""
    import base64
    import zipfile
    from datetime import datetime

    with zipfile.ZipFile(wheel_path, "r") as zf:
        existing = zf.namelist()
        entries = [(info, zf.read(info.filename)) for info in zf.infolist()]
    injected = []
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).timetuple()[:6]
    record_name = next(n for n in existing if n.endswith("RECORD"))
    record_lines = None
    new_entries = []
    for info, data in entries:
        if info.filename == record_name:
            record_lines = data.decode("utf-8").splitlines()
            continue
        new_entries.append((info, data))
    for src, arcname, _ in package_data:
        if arcname is None:
            continue  # sdist-only entry (e.g. demo fixtures)
        if arcname in existing:
            continue
        data = src.read_bytes()
        info = zipfile.ZipInfo(arcname, date_time=dt)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        new_entries.append((info, data))
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).decode().rstrip("=")
        record_lines.append(f"{arcname},sha256={digest},{len(data)}")
        injected.append(arcname)
    if not injected:
        return []
    record_data = ("\n".join(record_lines) + "\n").encode("utf-8")
    rec_info = zipfile.ZipInfo(record_name, date_time=dt)
    rec_info.compress_type = zipfile.ZIP_DEFLATED
    rec_info.external_attr = 0o644 << 16
    new_entries.append((rec_info, record_data))
    tmp = wheel_path.with_suffix(".tmp-whl")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, data in new_entries:
            zf.writestr(info, data)
    tmp.replace(wheel_path)
    return injected


def _normalize_sdist(path, epoch, extra_files=()):
    """Rewrite an sdist tar.gz deterministically.

    Setuptools 84 stamps generated files (PKG-INFO, egg-info/*) with the
    build wall-clock time and gzip adds its own header timestamp, so two
    otherwise identical sdists differ byte-for-byte. Contents are
    identical; only timestamps differ. This rewrite preserves every
    member's content, order, name, type and mode, and pins all mtimes
    (tar members + gzip header) to SOURCE_DATE_EPOCH (the HEAD commit
    timestamp), uid/gid 0, empty uname/gname. Standard reproducible-build
    normalisation; no file content is changed. Returns the new sha256.

    ``extra_files`` is a list of (arcname, source_path) appended to the
    tar -- used to inject package-data files that setuptools left out of
    the sdist (same repair as the wheel; see _repair_wheel).
    """
    raw = path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        members = []
        for m in tf.getmembers():
            data = tf.extractfile(m).read() if m.isfile() else None
            m.mtime = epoch
            m.uid = 0
            m.gid = 0
            m.uname = ""
            m.gname = ""
            m.pax_headers = {}
            members.append((m, data))
        present = {m.name for m, _ in members}
        for arcname, src in extra_files:
            if arcname in present:
                continue
            data = Path(src).read_bytes()
            ti = tarfile.TarInfo(arcname)
            ti.size = len(data)
            ti.mtime = epoch
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mode = 0o644
            members.append((ti, data))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for m, data in members:
            tf.addfile(m, io.BytesIO(data) if data is not None else None)
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out,
                       mtime=epoch) as gz:
        gz.write(buf.getvalue())
    path.write_bytes(out.getvalue())
    return _sha256(path)


def _restore_build_side_effects(build_dir_existed):
    """``python -m build`` regenerates the tracked src/turnkeep.egg-info
    in-tree (the committed copy is stale from R0) and may create build/.
    Restore both so the working tree is exactly as clean as before."""
    status = _run(["git", "status", "--porcelain",
                   "src/turnkeep.egg-info"])
    if status.stdout.strip():
        _run(["git", "checkout", "--", "src/turnkeep.egg-info"])
    build_dir = ROOT / "build"
    if build_dir.exists() and not build_dir_existed:
        shutil.rmtree(build_dir, ignore_errors=True)


def _check_ui_dist(dist_dir):
    """Relative assets + no remote URLs, same rules as scripts/ui_gate.py."""
    import re
    index = (dist_dir / "index.html").read_text(encoding="utf-8")
    relative = ("./assets/" in index and 'src="/assets/' not in index
                and 'href="/' not in index)
    external = []
    for f in sorted(dist_dir.rglob("*")):
        if f.is_file() and f.suffix in (".js", ".css", ".html"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"https?://[^\s\"'<>]+", text):
                url = m.group(0)
                if "localhost" not in url and "127.0.0.1" not in url:
                    external.append(f"{f.relative_to(dist_dir)}: {url}")
    return {"relative_assets": relative, "external_urls": external[:10],
            "status": "PASS" if relative and not external else "FAIL"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true",
                    help="Remove the previous output directory first.")
    ap.add_argument("--output", default="release/artifacts",
                    help="Output directory for the release candidate.")
    ap.add_argument("--upload", action="store_true",
                    help="Request a remote upload (requires --upload-target "
                         "and TURNKEEP_RELEASE_UPLOAD_AUTHORIZED=1).")
    ap.add_argument("--upload-target", default=None,
                    help="Explicit remote target for --upload.")
    args = ap.parse_args()

    if args.upload:
        authorised = os.environ.get("TURNKEEP_RELEASE_UPLOAD_AUTHORIZED") == "1"
        if not args.upload_target or not authorised:
            print("REFUSED: --upload needs both --upload-target and "
                  "TURNKEEP_RELEASE_UPLOAD_AUTHORIZED=1. Nothing was "
                  "uploaded and no release was claimed.", file=sys.stderr)
            return 2
        print("REFUSED: upload was authorised in form, but no upload "
              "backend is configured for this candidate. Nothing was "
              "uploaded.", file=sys.stderr)
        return 2

    out = (ROOT / args.output).resolve()

    # ---- source state (checked BEFORE any deletion: F2, T047 review) --
    # A --clean build on a dirty tree must refuse WITHOUT touching the
    # previous candidate. Deleting the output first and refusing after
    # destroyed the release artifacts during the T047 review.
    head = _git("rev-parse", "HEAD")
    commit_ts = _git("log", "-1", "--format=%ct")
    source_date_epoch = int(commit_ts) if commit_ts else 0
    diff = _run(["git", "diff", "--quiet"])
    tracked_clean = diff.returncode == 0
    status = _run(["git", "status", "--porcelain"])
    untracked = [l for l in status.stdout.splitlines() if l.startswith("??")]
    if not tracked_clean:
        print("FAIL: tracked files are modified; commit or revert before "
              "building a release candidate. The previous output "
              "directory was left untouched.", file=sys.stderr)
        print(status.stdout, file=sys.stderr)
        return 1
    if args.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    if head is None:
        print("FAIL: not a git checkout; cannot record commit.", file=sys.stderr)
        return 1

    node_v = _run(["node", "--version"]).stdout.strip() or "unknown"
    npm_v = _run(["npm", "--version"]).stdout.strip() or "unknown"
    builder = {"python": sys.version.split()[0],
               "sqlite": sqlite3.sqlite_version,
               "node": node_v, "npm": npm_v,
               "platform": sys.platform}

    # ---- Python build, twice, for a real reproducibility comparison ---
    build_logs = []
    python_kind = None
    first_hashes = None
    second_hashes = None
    build_dir_existed = (ROOT / "build").exists()
    package_data = _find_package_data() + _find_demo_fixtures()
    with tempfile.TemporaryDirectory(prefix="tk-build-a-") as a, \
            tempfile.TemporaryDirectory(prefix="tk-build-b-") as b:
        ok_a, log_a, kind_a = _build_python_once(Path(a), source_date_epoch)
        _restore_build_side_effects(build_dir_existed)
        build_logs.append({"run": "a", "ok": ok_a, "detail": log_a})
        ok_b, log_b, kind_b = _build_python_once(Path(b), source_date_epoch)
        _restore_build_side_effects(build_dir_existed)
        build_logs.append({"run": "b", "ok": ok_b, "detail": log_b})
        if not (ok_a and ok_b):
            if kind_a == "source-tree-fallback" or kind_b == "source-tree-fallback":
                python_kind = "source-tree-fallback"
            else:
                print("FAIL: python -m build failed; see build log above.",
                      file=sys.stderr)
                print(json.dumps(build_logs, indent=2), file=sys.stderr)
                return 1
        else:
            python_kind = kind_a
            # Packaging repair (diagnosed 2026-11-08 by verify: the stock
            # wheel installed but any DB init failed -- storage.py reads
            # migrations/001_initial.sql from the installed package dir,
            # and pyproject.toml used to declare no package-data, so
            # setuptools dropped it from wheel AND sdist). The build
            # script injects any missing runtime data file into both
            # artifacts and updates RECORD; contents are unchanged, the
            # repair is logged here and in the manifest. Since the R4
            # rework pyproject.toml DOES declare the package-data
            # (P2-R4-09), a plain build already ships the SQL and the
            # injection below is a no-op safety net, kept so a future
            # pyproject regression can never silently ship a broken
            # wheel again. Demo fixtures are sdist-only: demo.py
            # resolves them via parents[2], which is lib/python3.12 when
            # installed -- a location no wheel layout can populate (see
            # manifest known_gaps.demo_fixtures_installed).
            repair_log = []
            for label, d in (("a", Path(a) / "dist"), ("b", Path(b) / "dist")):
                injected_wheel = []
                for wheel in d.glob("*.whl"):
                    injected_wheel += _repair_wheel(
                        wheel, source_date_epoch, package_data)
                injected_sdist = []
                for sdist in d.glob("*.tar.gz"):
                    _normalize_sdist(
                        sdist, source_date_epoch,
                        extra_files=[(sarc, src)
                                     for src, _, sarc in package_data])
                    injected_sdist.append(sdist.name)
                repair_log.append({"run": label,
                                   "wheel_injected": injected_wheel,
                                   "sdist_rewritten": injected_sdist})
            build_logs.append({"run": "packaging-repair",
                               "detail": repair_log + [{
                                   "reason": "sdist timestamps normalised "
                                             "to SOURCE_DATE_EPOCH (setuptools "
                                             "stamps generated files + gzip "
                                             "header with wall-clock time); "
                                             "member contents, order and "
                                             "modes unchanged.",
                                   "durable_fix_pending": "none for the "
                                     "migration SQL: pyproject.toml now "
                                     "declares the package-data (R4 rework, "
                                     "P2-R4-09); the injection above is a "
                                     "no-op safety net."}]})
            first_hashes = {p.name: _sha256(p)
                            for p in sorted((Path(a) / "dist").glob("*"))}
            second_hashes = {p.name: _sha256(p)
                             for p in sorted((Path(b) / "dist").glob("*"))}
            for name, h in first_hashes.items():
                shutil.copy2(Path(a) / "dist" / name, out / name)

    if python_kind == "source-tree-fallback":
        tarball = out / f"turnkeep-{VERSION}-source.tar.gz"
        src_files = _deterministic_source_tarball(tarball)
        build_logs.append({"run": "fallback",
                           "detail": "build/setuptools/wheel toolchain "
                                     "unavailable and pip install failed; "
                                     "source-tree tarball used instead, "
                                     "no wheel fabricated.",
                           "files_included": len(src_files)})

    # ---- UI build ------------------------------------------------------
    ui_build = _run(["npm", "--prefix", "ui", "run", "build"], timeout=900)
    if ui_build.returncode != 0:
        print("FAIL: UI production build failed.", file=sys.stderr)
        print((ui_build.stdout + ui_build.stderr)[-3000:], file=sys.stderr)
        return 1
    ui_dist = ROOT / "ui" / "dist"
    if not (ui_dist / "index.html").is_file():
        print("FAIL: ui/dist/index.html missing after build.", file=sys.stderr)
        return 1
    ui_check = _check_ui_dist(ui_dist)
    if ui_check["status"] != "PASS":
        print("FAIL: UI dist check failed: %s" % ui_check, file=sys.stderr)
        return 1
    ui_tarball = out / f"turnkeep-{VERSION}-ui-dist.tar.gz"
    _deterministic_tarball(ui_dist, ui_tarball, "ui-dist")

    # ---- lock files ----------------------------------------------------
    locks_dir = out / "locks"
    locks_dir.mkdir(exist_ok=True)
    lock_sources = {"base": ROOT / "requirements" / "base.lock",
                    "mcp": ROOT / "requirements" / "mcp.lock",
                    "ui": ROOT / "ui" / "package-lock.json"}
    lock_hashes = {}
    for name, src in lock_sources.items():
        if not src.is_file():
            print(f"FAIL: lock file missing: {src}", file=sys.stderr)
            return 1
        shutil.copy2(src, locks_dir / src.name)
        lock_hashes[name] = _sha256(src)
    # SBOM (T045 deliverable, copied read-only, never edited here).
    sbom_src = ROOT / "release" / "SBOM.json"
    if sbom_src.is_file():
        shutil.copy2(sbom_src, out / "SBOM.json")

    # ---- reproducibility verdict (real comparison, no pretending) ------
    repro = {"two_builds_compared": first_hashes is not None,
             "identical": None, "differing_files": [], "reason": None}
    if first_hashes is not None:
        differing = [n for n in first_hashes
                     if second_hashes.get(n) != first_hashes[n]]
        differing += [n for n in second_hashes if n not in first_hashes]
        repro["differing_files"] = sorted(differing)
        repro["identical"] = not differing
        repro["reason"] = ("Two wheel/sdist builds from the same commit "
                           "with SOURCE_DATE_EPOCH pinned to the commit "
                           "timestamp are bit-for-bit identical (sdist "
                           "timestamps normalised as documented in "
                           "_normalize_sdist; wheel needed no "
                           "normalisation)."
                           if not differing else
                           "Two builds differ; see differing_files. "
                           "Not claimed reproducible.")
    else:
        repro["reason"] = ("Source-tree fallback: no wheel/sdist built, so "
                           "no two-build wheel comparison applies; the "
                           "source tarball itself is deterministic "
                           "(sorted entries, fixed mtime/uid/gid).")

    # ---- SHA256SUMS + manifest -----------------------------------------
    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name not in ("SHA256SUMS", "manifest.json"):
            files.append({"path": p.relative_to(out).as_posix(),
                          "sha256": _sha256(p), "bytes": p.stat().st_size})
    (out / "SHA256SUMS").write_text(
        "".join(f"{f['sha256']}  {f['path']}\n" for f in files),
        encoding="utf-8")
    files.append({"path": "SHA256SUMS",
                  "sha256": _sha256(out / "SHA256SUMS"),
                  "bytes": (out / "SHA256SUMS").stat().st_size})

    manifest = {
        "name": "turnkeep",
        "product": "Turnkeep (working name)",
        "version": VERSION,
        "contract_version": CONTRACT_VERSION,
        "schema": {"target_schema_version": SCHEMA_VERSION,
                   "export_schema_version": SCHEMA_VERSION,
                   "compatible_with": "schema v1 only; no migration path "
                                      "from any other schema version is "
                                      "claimed"},
        "commit": head,
        "source_date_epoch": source_date_epoch,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "builder": builder,
        "tracked_tree_clean": tracked_clean,
        "untracked_files_at_build": untracked,
        "python_build": python_kind,
        "build_logs": build_logs,
        "packaging_repair": {
            "injected_package_data": [arc for _, arc, _ in _find_package_data()],
            "injected_sdist_only": [sarc for _, arc, sarc in _find_demo_fixtures()],
            "reason": "pyproject.toml declares no package-data, so the "
                      "stock setuptools wheel/sdist omit runtime data "
                      "files: storage.py reads migrations/001_initial.sql "
                      "from the installed package dir. The unrepaired "
                      "wheel installed but every DB init failed "
                      "(FileNotFoundError); verify_release.py caught "
                      "this. The build script injects the file into the "
                      "wheel (RECORD updated) and sdist; contents "
                      "unchanged. Demo fixtures (fixtures/demo/**) are "
                      "injected into the sdist only: demo.py resolves "
                      "them via Path(__file__).parents[2], which is the "
                      "repo root in source but lib/python3.12 when "
                      "installed -- a location no wheel layout can "
                      "populate -- so the installed wheel's "
                      "export-format / research-correction scenarios "
                      "stay broken (see known_gaps."
                      "demo_fixtures_installed); the default j01 "
                      "scenario needs no fixtures and passes installed.",
            "durable_fix_pending": "Make demo.py load fixtures via "
                                   "importlib.resources (demo.py is "
                                   "core-owned). The migration SQL is no "
                                   "longer pending: pyproject.toml declares "
                                   "the package-data since the R4 rework "
                                   "(P2-R4-09), so a wheel rebuilt from the "
                                   "sdist alone now keeps it.",
        },
        "ui_build": {"tool": "npm --prefix ui run build (tsc + vite build)",
                     "check": ui_check},
        "locks": {
            "base": {"file": "locks/base.lock",
                     "sha256": lock_hashes["base"],
                     "note": "Core is stdlib + SQLite only; pytest 9.2.0 "
                             "is the only dev pin."},
            "mcp": {"file": "locks/mcp.lock",
                    "sha256": lock_hashes["mcp"],
                    "note": "Official MCP Python SDK 2.3.0 (T021)."},
            "ui": {"file": "locks/package-lock.json",
                   "sha256": lock_hashes["ui"],
                   "note": "UI lock source is ui/package-lock.json. "
                           "requirements/ui.lock is an intentionally empty "
                           "R0 placeholder (one comment line) and is NOT "
                           "a lock file; it was not copied and must not "
                           "be used as one."},
        },
        "platforms": {
            "linux": {"status": "verified",
                      "detail": "This VM: doctor PASS, J01 + J05 journeys "
                                "PASS, UI production gate PASS (T043)."},
            "windows": {"status": "NOT_RUN",
                        "detail": "No Windows runner on this VM; "
                                  ".github/workflows/test.yml exists but "
                                  "no run has executed."},
            "macos": {"status": "NOT_RUN",
                      "detail": "No macOS runner on this VM; "
                                ".github/workflows/test.yml exists but "
                                "no run has executed."},
        },
        "known_gaps": {
            "T026": {"status": "VERIFIED",
                     "detail": "Hybrid substitution accepted by Sam 2026-11-10 "
                               "~09:08 GMT: Codex CLI 0.164.0-alpha.9 "
                               "(bridge, GPT-5.6 Sol) + agy 1.5.3 "
                               "(gemini-3.8-flash-low); agy reverse leg "
                               "VERIFIED (6 real MCP calls, finish "
                               "receipt; commit f9e2786). Disclosed: Codex "
                               "forward leg not run (M365 outage); strict "
                               "live-clients gate remains NOT_RUN."},
            "T042": {"status": "VERIFIED",
                     "detail": "Two independent clean live runs: codex-api "
                               "(GPT-6 Luna low, official OpenAI API) pilot "
                               "43/48, confirmation 354/432 (81.9%); agy "
                               "pilot 43/48, confirmation 352/432 (81.5%). "
                               "Spend $1.90 for all 480 attempts. Finding "
                               "MIXED: turnkeep arm 0% stale, completion "
                               "below files."},
            "T043": {"status": "FAIL",
                     "detail": "Performance gate FAIL (3 thresholds): "
                               "finish p95 7759.9 ms > 500 ms at 10k "
                               "records; recall p95 3182.9 ms > 1000 ms "
                               "at 100k records; finish p95 12288.3 ms > "
                               "500 ms at 100k records. Causes: begin() "
                               "full-snapshot O(n), recall() full-table "
                               "scan O(n). Cold start 159.4 ms PASS. "
                               "Thresholds not lowered, datasets not "
                               "shrunk."},
            "trademark": {"status": "NOT_RUN",
                          "detail": "No trademark clearance claimed. "
                                    "turnkeep.app is a live rental product "
                                    "in a different domain (T002); "
                                    "'Turnkeep' is a working name only."},
            "demo_fixtures_installed": {"status": "FAIL",
                     "detail": "README's --scenario export-format / "
                               "research-correction demos fail from the "
                               "installed wheel (FileNotFoundError): "
                               "demo.py resolves fixtures via "
                               "Path(__file__).parents[2], the repo root "
                               "in source but lib/python3.12 when "
                               "installed -- a location no wheel layout "
                               "can populate. The default j01 scenario "
                               "(the actual J01 journey) needs no "
                               "fixtures and passes installed. Durable "
                               "fix: demo.py via importlib.resources "
                               "(core-owned, not T046). Fixtures are "
                               "shipped in the sdist, so a source "
                               "checkout unpacked from it runs them."},
        },
        "reproducibility": repro,
        "upload": {"attempted": False,
                   "reason": "Build-only by default; no --upload given. "
                             "Remote upload requires --upload-target and "
                             "TURNKEEP_RELEASE_UPLOAD_AUTHORIZED=1, and no "
                             "upload backend is configured for this "
                             "candidate."},
        "files": files,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(json.dumps({"status": "BUILT", "output": str(out),
                      "python_build": python_kind,
                      "reproducible": repro["identical"],
                      "files": len(files)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
