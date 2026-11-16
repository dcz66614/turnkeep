"""Regression tests for P2-R6-03: the shipped verifier from the
extracted package (R6 rework item 3).

The manifest shipped inside the zip has archive_sha256 null by
design (a file cannot hash the archive that contains it), so the
verifier as shipped failed check 2 from the extracted package while
passing 7/7 from the source tree. The fix is a --expected-sha256
flag: the recipient computes the zip's sha256 and passes it, and the
flag replaces the manifest's archive_sha256 for check 2 only.

These tests simulate the recipient flow against the CURRENT release
zip (release/TURNKEEP_RC.zip — never rebuilt here): extract it to a
temp dir, run the verifier from the extracted copy, and check the
outcomes with and without the flag. The script under test is the
source-tree scripts/verify_handoff.py, copied over the extracted
copy: packaging copies scripts/ from the source tree, so the copy
is what the rebuilt RC will ship. The manifest under test is the
EXTRACTED-package copy (archive_sha256 null), never the on-disk one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "verify_handoff.py"
RC_ZIP = ROOT / "release" / "TURNKEEP_RC.zip"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture
def extracted_rc(tmp_path):
    """The current RC extracted to a temp dir, with the source-tree
    verifier copied over the extracted (pre-flag) copy.

    Returns (zip_path, extracted_root, real_sha256).
    """
    assert RC_ZIP.is_file(), f"release zip missing: {RC_ZIP}"
    dest = tmp_path / "extracted"
    dest.mkdir()
    with zipfile.ZipFile(RC_ZIP) as zf:
        zf.extractall(dest)
    roots = [d for d in dest.iterdir() if d.is_dir()]
    assert len(roots) == 1
    root = roots[0]
    # The shipped RC predates the flag; the rebuilt RC ships the
    # source-tree script, so the copy under test is that script.
    shutil.copy2(SCRIPT, root / "scripts" / "verify_handoff.py")
    # The manifest under test is the extracted one: null by design.
    manifest = json.loads(
        (root / "release" / "DELIVERY_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest.get("archive_sha256") is None
    return RC_ZIP, root, _sha256(RC_ZIP)


def _run_verifier(script, argv, cwd):
    r = subprocess.run([sys.executable, str(script), *argv],
                       cwd=str(cwd), capture_output=True, text=True,
                       timeout=600, encoding="utf-8", errors="replace")
    report = json.loads(r.stdout)
    return r.returncode, report, r.stderr


# -- recipient flow from the extracted package ------------------------

def test_extracted_verifier_passes_with_expected_sha256(extracted_rc):
    zip_path, root, real_sha = extracted_rc
    rc, report, stderr = _run_verifier(
        root / "scripts" / "verify_handoff.py",
        ["--archive", str(zip_path), "--expected-sha256", real_sha],
        cwd=root)
    assert rc == 0, f"{stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "PASS"
    assert report["failures"] == []
    assert len(report["checks"]) == 7
    assert any("--expected-sha256" in c for c in report["checks"])


def test_extracted_verifier_fails_without_flag_and_names_it(extracted_rc):
    # The original P2-R6-03 repro: FAIL, 6 checks, the single failure
    # names the flag the recipient must use.
    zip_path, root, _real_sha = extracted_rc
    rc, report, stderr = _run_verifier(
        root / "scripts" / "verify_handoff.py",
        ["--archive", str(zip_path)],
        cwd=root)
    assert rc == 1, f"{stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "FAIL"
    assert len(report["checks"]) == 6
    assert len(report["failures"]) == 1
    assert "--expected-sha256" in report["failures"][0]
    assert "archive_sha256" in report["failures"][0]


def test_extracted_verifier_rejects_wrong_expected_sha256(extracted_rc):
    # The flag replaces the comparison value; it does not bypass
    # check 2. A wrong digest must still fail, naming the flag.
    zip_path, root, _real_sha = extracted_rc
    rc, report, stderr = _run_verifier(
        root / "scripts" / "verify_handoff.py",
        ["--archive", str(zip_path),
         "--expected-sha256", "0" * 64],
        cwd=root)
    assert rc == 1, f"{stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "FAIL"
    assert len(report["failures"]) == 1
    assert "--expected-sha256" in report["failures"][0]
    assert "mismatch" in report["failures"][0]


def test_extracted_verifier_rejects_malformed_expected_sha256(extracted_rc):
    zip_path, root, _real_sha = extracted_rc
    rc, report, stderr = _run_verifier(
        root / "scripts" / "verify_handoff.py",
        ["--archive", str(zip_path),
         "--expected-sha256", "not-a-sha256"],
        cwd=root)
    assert rc == 1, f"{stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "FAIL"
    assert len(report["failures"]) == 1
    assert "--expected-sha256" in report["failures"][0]


# -- source-tree run must be unchanged ---------------------------------

def test_source_tree_verifier_still_passes_without_flag():
    # The same verifier from the source tree (on-disk manifest with
    # archive_sha256 filled in) must still pass 7/7 with no flag.
    rc, report, stderr = _run_verifier(
        SCRIPT, ["--archive", "release/TURNKEEP_RC.zip"], cwd=ROOT)
    assert rc == 0, f"{stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "PASS"
    assert report["failures"] == []
    assert len(report["checks"]) == 7


# -- R6: non-ASCII report must survive a cp1252 locale ------------------

def test_verifier_report_survives_cp1252_stdout(extracted_rc):
    # CI #5: the no-flag failure message carries an em dash. Under a
    # cp1252 stdout locale (the Windows default) the verifier printed
    # it as byte 0x97, which a strict-UTF-8 parent could not decode,
    # so _run_verifier died before json.loads. The verifier must emit
    # valid UTF-8 regardless of the locale encoding.
    zip_path, root, _real_sha = extracted_rc
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"  # simulate Windows locale stdout
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_handoff.py"),
         "--archive", str(zip_path)],
        cwd=str(root), capture_output=True, text=True, timeout=600,
        encoding="utf-8", env=env)
    report = json.loads(r.stdout)
    assert r.returncode == 1, f"{r.stderr}\n{json.dumps(report, indent=2)}"
    assert report["status"] == "FAIL"
    assert len(report["failures"]) == 1
    assert "--expected-sha256" in report["failures"][0]
    assert "archive_sha256" in report["failures"][0]


def test_run_verifier_tolerates_undecodable_child_output(tmp_path):
    # R6: even if a child emits a stray non-UTF-8 byte, _run_verifier
    # must return the parsed report instead of raising in the decoder.
    stub = tmp_path / "stub.py"
    stub.write_bytes(
        b"import sys\n"
        b"sys.stdout.buffer.write(b'{\"status\": \"FAIL\", \"checks\": [], "
        b"\"failures\": [\"x \\x97 y\"]}\\n')\n")
    rc, report, _stderr = _run_verifier(stub, [], cwd=tmp_path)
    assert rc == 0
    assert report["status"] == "FAIL"
