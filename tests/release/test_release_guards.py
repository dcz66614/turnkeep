"""Regression tests for the P0-R3-03 durable guards (R3 rework item 1b).

The R3 ZIP shipped a stale wheel (built 18:21, before the R2 rework)
because no check compared the wheel to src/ and packaging trusted the
artifact manifest's stale ``tracked_tree_clean`` snapshot. These tests
exercise the guards in scripts/verify_release.py and
scripts/package_handoff.py against synthetic fixtures, so they pass
regardless of the state of the real release/artifacts/ tree.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        f"tk_guard_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verify_release = _load_script("verify_release")
package_handoff = _load_script("package_handoff")


def _fake_src(tmp_path, files=None):
    """A tiny src/turnkeep tree; returns the turnkeep dir."""
    src = tmp_path / "src" / "turnkeep"
    (src / "sub").mkdir(parents=True)
    # write_bytes, not write_text: _files_from_dir compares raw bytes,
    # and text mode would translate "\n" to "\r\n" on Windows, so the
    # expected b"...\n" bytes would never match there.
    (src / "a.py").write_bytes(b"VALUE = 1\n")
    (src / "sub" / "b.py").write_bytes(b"VALUE = 2\n")
    # A non-.py file (migration SQL): the guards must compare these too
    # (P2-R4-07), so the fake tree carries one from the start.
    (src / "migrations").mkdir(parents=True)
    (src / "migrations" / "001_initial.sql").write_bytes(
        b"CREATE TABLE t (id TEXT);\n")
    if files:
        for rel, text in files.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
    return src


def _fake_wheel(path, src_dir, mutate=None):
    """Zip a wheel from src_dir; mutate maps rel -> replacement bytes."""
    mutate = mutate or {}
    with zipfile.ZipFile(path, "w") as zf:
        for p in sorted(src_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir).as_posix()
            data = mutate.get(rel, p.read_bytes())
            zf.writestr(f"turnkeep/{rel}", data)
        zf.writestr("turnkeep-0.1.0.dist-info/METADATA", b"Metadata-Version: 2.1\n")


def _fake_sdist(path, src_dir, mutate=None):
    """Tarball an sdist from src_dir; mutate maps rel -> replacement bytes."""
    mutate = mutate or {}
    with tarfile.open(path, "w:gz") as tf:
        for p in sorted(src_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src_dir).as_posix()
            data = mutate.get(rel, p.read_bytes())
            info = tarfile.TarInfo(f"turnkeep-0.1.0/src/turnkeep/{rel}")
            info.size = len(data)
            import io
            tf.addfile(info, io.BytesIO(data))


def _manifest_with(*names):
    return {"files": [{"path": n, "sha256": "x" * 64} for n in names]}


# -- wheel/sdist vs src: the shared diff -------------------------------

def test_diff_matching_wheel_and_sdist_pass(tmp_path):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "turnkeep-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "turnkeep-0.1.0.tar.gz"
    _fake_wheel(wheel, src)
    _fake_sdist(sdist, src)
    src_files = verify_release._files_from_dir(src)
    assert verify_release._diff_trees(
        verify_release._files_from_wheel(wheel), src_files, "wheel") == []
    assert verify_release._diff_trees(
        verify_release._files_from_sdist(sdist), src_files, "sdist") == []
    # Same check through package_handoff's mirror of the helpers.
    assert package_handoff._diff_trees(
        package_handoff._files_from_wheel(wheel), src_files, "wheel") == []


def test_diff_detects_changed_file_in_wheel_and_sdist(tmp_path):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    sdist = tmp_path / "s.tar.gz"
    _fake_wheel(wheel, src, mutate={"a.py": b"VALUE = 99\n"})
    _fake_sdist(sdist, src, mutate={"sub/b.py": b"VALUE = 99\n"})
    src_files = verify_release._files_from_dir(src)
    wheel_probs = verify_release._diff_trees(
        verify_release._files_from_wheel(wheel), src_files, "wheel")
    assert len(wheel_probs) == 1 and "a.py" in wheel_probs[0]
    assert "differs" in wheel_probs[0]
    sdist_probs = verify_release._diff_trees(
        verify_release._files_from_sdist(sdist), src_files, "sdist")
    assert len(sdist_probs) == 1 and "sub/b.py" in sdist_probs[0]


def test_diff_detects_missing_and_extra_files(tmp_path):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    _fake_wheel(wheel, src)
    src_files = verify_release._files_from_dir(src)
    # Drop a file from the wheel's view and add a foreign one.
    wheel_files = verify_release._files_from_wheel(wheel)
    del wheel_files["sub/b.py"]
    wheel_files["zzz.py"] = b""
    probs = verify_release._diff_trees(wheel_files, src_files, "wheel")
    assert any("missing" in p and "sub/b.py" in p for p in probs)
    assert any("extra" in p and "zzz.py" in p for p in probs)


def test_verify_release_check_fails_on_stale_wheel(tmp_path, monkeypatch):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    _fake_wheel(wheel, src, mutate={"a.py": b"VALUE = 99\n"})
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)
    report = {"checks": [], "failures": [], "not_run": []}
    verify_release._check_wheel_vs_source(
        _manifest_with("w.whl"), tmp_path, report)
    assert report["failures"], "stale wheel must fail verify_release"
    assert any("a.py" in f for f in report["failures"])


def test_verify_release_check_passes_on_matching_wheel(tmp_path, monkeypatch):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    _fake_wheel(wheel, src)
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)
    report = {"checks": [], "failures": [], "not_run": []}
    verify_release._check_wheel_vs_source(
        _manifest_with("w.whl"), tmp_path, report)
    assert report["failures"] == []
    assert any("matches src/turnkeep" in c for c in report["checks"])


def test_package_check_refuses_stale_wheel(tmp_path):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    _fake_wheel(wheel, src, mutate={"sub/b.py": b"VALUE = 99\n"})
    probs = package_handoff._check_artifacts_match_src(
        tmp_path, src, _manifest_with("w.whl"))
    assert probs and any("sub/b.py" in p for p in probs)


def test_package_check_accepts_matching_wheel(tmp_path):
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    _fake_wheel(wheel, src)
    assert package_handoff._check_artifacts_match_src(
        tmp_path, src, _manifest_with("w.whl")) == []


# -- packaging-time tree cleanliness ------------------------------------

def _git(*args, cwd, env=None):
    merged = dict(env or {})
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env={**__import__("os").environ, **merged}, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r


def _git_repo(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.invalid", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-qm", "init", cwd=tmp_path)
    return tmp_path


def test_tree_clean_problems_empty_on_clean_tree(tmp_path):
    repo = _git_repo(tmp_path)
    built_at = datetime.now(timezone.utc)
    assert package_handoff._tree_clean_problems(built_at, repo) == []


def test_tree_clean_problems_detects_modified_src_file(tmp_path):
    repo = _git_repo(tmp_path)
    built_at = datetime.now(timezone.utc)
    (repo / "src" / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
    probs = package_handoff._tree_clean_problems(built_at, repo)
    assert probs and any("src/a.py" in p for p in probs)


def test_tree_clean_problems_detects_untracked_src_file(tmp_path):
    repo = _git_repo(tmp_path)
    built_at = datetime.now(timezone.utc)
    (repo / "src" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    probs = package_handoff._tree_clean_problems(built_at, repo)
    assert probs and any("src/new.py" in p for p in probs)


def test_tree_clean_problems_ignores_non_src_changes(tmp_path):
    # Docs/tests changes do not affect the wheel; only shipped src/
    # files are in scope for this check.
    repo = _git_repo(tmp_path)
    built_at = datetime.now(timezone.utc)
    (repo / "README.md").write_text("changed docs\n", encoding="utf-8")
    assert package_handoff._tree_clean_problems(built_at, repo) == []


def test_tree_clean_problems_refuses_without_built_at(tmp_path):
    repo = _git_repo(tmp_path)
    probs = package_handoff._tree_clean_problems(None, repo)
    assert probs and "built_at" in probs[0]


def test_parse_built_at_round_trip():
    dt = package_handoff._parse_built_at("2026-11-10T18:21:30.102860+00:00")
    assert dt is not None and dt.year == 2026 and dt.tzinfo is not None
    assert package_handoff._parse_built_at("not-a-date") is None
    assert package_handoff._parse_built_at(None) is None


# -- R4 rework: .sql guards, HEAD stamping, sdist rebuild, not_run ------

def test_diff_detects_changed_sql_in_wheel_and_sdist(tmp_path):
    # P2-R4-07: the guards used to compare .py only, so a drifted
    # migration .sql passed silently. Mutating one .sql file must now
    # fail the guard for both artifacts, in both scripts.
    src = _fake_src(tmp_path)
    wheel = tmp_path / "w.whl"
    sdist = tmp_path / "s.tar.gz"
    _fake_wheel(wheel, src, mutate={
        "migrations/001_initial.sql": b"CREATE TABLE t (id INTEGER);\n"})
    _fake_sdist(sdist, src, mutate={
        "migrations/001_initial.sql": b"CREATE TABLE t (id INTEGER);\n"})
    src_files = verify_release._files_from_dir(src)
    assert "migrations/001_initial.sql" in src_files
    wheel_probs = verify_release._diff_trees(
        verify_release._files_from_wheel(wheel), src_files, "wheel")
    assert len(wheel_probs) == 1
    assert "migrations/001_initial.sql" in wheel_probs[0]
    assert "differs" in wheel_probs[0]
    sdist_probs = verify_release._diff_trees(
        verify_release._files_from_sdist(sdist), src_files, "sdist")
    assert len(sdist_probs) == 1
    assert "migrations/001_initial.sql" in sdist_probs[0]
    mirror_probs = package_handoff._check_artifacts_match_src(
        tmp_path, src, _manifest_with("w.whl", "s.tar.gz"))
    assert mirror_probs and any(
        "migrations/001_initial.sql" in p for p in mirror_probs)


def test_files_from_dir_includes_sql(tmp_path):
    # The guard's source tree must include non-.py files at all.
    src = _fake_src(tmp_path)
    files = verify_release._files_from_dir(src)
    assert files["migrations/001_initial.sql"] == (
        b"CREATE TABLE t (id TEXT);\n")
    assert files["a.py"] == b"VALUE = 1\n"


def test_check_version_commit_records_not_run_without_git(monkeypatch):
    # P2-R4-10: with no .git (extracted package), git rev-parse HEAD
    # yields nothing; the check must be recorded as not_run, not
    # silently omitted (the extracted-package report then reads 12
    # checks + 1 not_run instead of a silent 12).
    class _Proc:
        stdout = ""

    monkeypatch.setattr(verify_release, "_run",
                        lambda *args, **kwargs: _Proc())
    report = {"checks": [], "failures": [], "not_run": []}
    verify_release._check_version_commit({"version": "0.1.0"}, report)
    assert report["failures"] == []
    assert any("NOT_RUN" in n for n in report["not_run"])
    assert any("commit" in n.lower() for n in report["not_run"])


def test_stamp_packaging_head_replaces_token():
    # P2-R4-08: the HEAD@PACKAGING token in the handoff docs is
    # replaced with the packaging HEAD; files without the token — and
    # files other than the two handoff docs, which may mention the
    # token (the stamping script itself, the guard tests) — are
    # untouched.
    head = "a" * 40
    files = {Path("HANDOFF.md"): b"git HEAD HEAD@PACKAGING\n",
             Path("release/FINAL_HANDOFF.md"): b"Head: `HEAD@PACKAGING`\n",
             Path("README.md"): b"no token here\n",
             Path("scripts/package_handoff.py"): b"HEAD@PACKAGING\n"}
    stamped = package_handoff._stamp_packaging_head(files, head)
    assert set(stamped) == {"HANDOFF.md", "release/FINAL_HANDOFF.md"}
    assert files[Path("HANDOFF.md")] == b"git HEAD " + head.encode() + b"\n"
    assert files[Path("release/FINAL_HANDOFF.md")] == (
        b"Head: `" + head.encode() + b"`\n")
    assert files[Path("README.md")] == b"no token here\n"
    assert files[Path("scripts/package_handoff.py")] == b"HEAD@PACKAGING\n"


def test_stamp_packaging_head_refuses_surviving_token():
    # A stamp that leaves the token in place (here: the head itself is
    # the token) must fail loudly, never ship an unstamped doc.
    with pytest.raises(ValueError):
        package_handoff._stamp_packaging_head(
            {Path("HANDOFF.md"): b"git HEAD HEAD@PACKAGING\n"},
            "HEAD@PACKAGING")


def test_wheel_built_from_sdist_keeps_migrations(tmp_path):
    # P2-R4-09: a plain `python -m build` from the sdist must produce
    # an equivalent wheel — migration SQL present, file list identical
    # to src/turnkeep/. Runs against the working tree (pyproject.toml
    # as it stands). `python -m build` writes side effects into the
    # tree (build/, src/turnkeep.egg-info — the latter is TRACKED, and
    # the build regenerates its PKG-INFO/SOURCES.txt), so both dirs are
    # snapshotted beforehand and restored afterwards: the test must
    # leave the working tree exactly as it found it.
    sdist_out = tmp_path / "sdist-out"
    sdist_out.mkdir()
    build_dir = ROOT / "build"
    egg_info = ROOT / "src" / "turnkeep.egg-info"

    def _snapshot(path):
        if not path.exists():
            return None
        return {p.relative_to(path): p.read_bytes()
                for p in path.rglob("*") if p.is_file()}

    def _restore(path, snapshot):
        shutil.rmtree(path, ignore_errors=True)
        if snapshot is None:
            return
        for rel, data in snapshot.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    build_snapshot = _snapshot(build_dir)
    egg_snapshot = _snapshot(egg_info)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "--no-isolation",
             "--outdir", str(sdist_out)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr[-2000:]
        sdists = list(sdist_out.glob("*.tar.gz"))
        assert len(sdists) == 1
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        with tarfile.open(sdists[0], "r:gz") as tf:
            tf.extractall(extracted, filter="data")
        roots = [d for d in extracted.iterdir() if d.is_dir()]
        assert len(roots) == 1
        wheel_out = tmp_path / "wheel-out"
        wheel_out.mkdir()
        r = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation",
             "--outdir", str(wheel_out)],
            cwd=str(roots[0]), capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr[-2000:]
        wheels = list(wheel_out.glob("*.whl"))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0]) as zf:
            names = zf.namelist()
        for sql in ("001_initial.sql", "002_record_provenance.sql",
                    "003_scope_secrets.sql", "004_erase_flags.sql",
                    "005_record_erase_keys.sql", "006_maintenance_pending.sql",
                    "007_purge_pending.sql"):
            assert f"turnkeep/migrations/{sql}" in names, names
        src_files = verify_release._files_from_dir(ROOT / "src" / "turnkeep")
        assert verify_release._diff_trees(
            verify_release._files_from_wheel(wheels[0]), src_files,
            "wheel") == []
    finally:
        _restore(build_dir, build_snapshot)
        _restore(egg_info, egg_snapshot)
