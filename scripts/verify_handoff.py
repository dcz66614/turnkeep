#!/usr/bin/env python3
"""Verify the Turnkeep handoff zip (T048).

  .venv/bin/python scripts/verify_handoff.py --archive release/TURNKEEP_RC.zip

Real checks, in order (exit 0 PASS, 1 FAIL, 2 NOT_RUN/refused):
  1. The archive is a readable zip; no entry escapes the prefix, is
     absolute, or lives under a forbidden directory (.venv, .git,
     __pycache__, node_modules) or has a forbidden suffix (.db,
     .sqlite*, .pyc, .pem, .key).
  2. release/DELIVERY_MANIFEST.json on disk matches the archive: its
     archive_sha256 equals the sha256 of the archive file, and the
     in-zip copy of the manifest is identical except archive_sha256.
     From the extracted package the in-zip manifest's archive_sha256
     is null by design (a file cannot hash the archive that contains
     it), so a recipient passes the zip's sha256 with
     --expected-sha256 <sha256> instead; when given, that value
     replaces the manifest's archive_sha256 for this check only.
  3. Every file listed in the manifest (except the manifest itself,
     whose sha256 is null by design) exists in the archive with
     matching sha256 and byte count; no listed file is missing.
  4. No extracted text file contains this machine's home path or a
     private-key marker; no secret-like filename is present.
  5. From the extracted copy (clean temp dir, never the source tree):
     the J01 zero-key demo runs (SCRIPTED_SIMULATION_NO_MODEL_CALLS)
     and at least one CLI remember/begin/finish round trip ends in a
     real receipt (record_id + commit_epoch).

Exit 0 only when every check passes. Nothing is uploaded, posted or
installed outside the temporary verification directory.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIR_PARTS = {".venv", ".git", "__pycache__", "node_modules",
                       ".pytest_cache", ".tmp"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
                      ".pyc", ".pem", ".key"}
def _forbidden_markers():
    """Markers that must never appear in a shipped text file.

    The machine home path is derived at runtime (Path.home()), never
    written as a literal: a literal in this file would make the scan
    flag this file itself. The key marker is a real PEM block (armour
    header + base64 body), not the bare words: leak-detector scripts
    legitimately contain the words as patterns.
    """
    home = str(Path.home())
    pem = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[0-9A-Za-z+/=\r\n]{40,}")
    return home, pem


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _run(argv, cwd=None, env=None, timeout=600):
    import os
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, env=merged,
                          capture_output=True, text=True, timeout=timeout, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="release/TURNKEEP_RC.zip")
    ap.add_argument("--manifest", default="release/DELIVERY_MANIFEST.json")
    ap.add_argument(
        "--expected-sha256", default=None, metavar="SHA256",
        help="sha256 of the archive file, used for check 2 in place of "
             "the manifest's archive_sha256. Needed when verifying from "
             "the extracted package, whose manifest carries null there "
             "by design (a file cannot hash the archive that contains "
             "it). Has no effect on any other check.")
    args = ap.parse_args()

    archive = (ROOT / args.archive).resolve()
    manifest_path = (ROOT / args.manifest).resolve()
    report = {"archive": str(archive), "checks": [], "failures": [],
              "notes": []}

    def fail(msg):
        report["failures"].append(msg)
        print(f"FAIL: {msg}", file=sys.stderr)

    if not archive.is_file():
        fail(f"archive not found: {archive}")
        print(json.dumps(report, indent=2))
        return 2
    if not manifest_path.is_file():
        fail(f"delivery manifest not found: {manifest_path}")
        print(json.dumps(report, indent=2))
        return 2

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        fail(f"delivery manifest is not valid JSON: {exc}")
        print(json.dumps(report, indent=2))
        return 1

    # 1. Archive structure.
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        fail(f"archive is not a readable zip: {exc}")
        print(json.dumps(report, indent=2))
        return 1
    names = zf.namelist()
    prefix = (manifest.get("archive_name", "").replace(".zip", "") or
              "turnkeep-0.1.0")
    bad = []
    for name in names:
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts:
            bad.append(name)
        elif any(p in FORBIDDEN_DIR_PARTS for p in parts):
            bad.append(name)
        elif Path(name).suffix in FORBIDDEN_SUFFIXES:
            bad.append(name)
    if bad:
        fail(f"forbidden entries in archive: {bad[:10]}")
    else:
        report["checks"].append(
            f"archive structure OK: {len(names)} entries, no forbidden "
            "dirs/suffixes, no absolute or escaping paths")

    # 2. Manifest vs archive.
    want_sha = manifest.get("archive_sha256")
    got_sha = _sha256(archive)
    if args.expected_sha256 is not None:
        # Recipient flow (P2-R6-03): the manifest shipped inside the
        # zip has archive_sha256 null by design, so the recipient
        # computes the zip's sha256 and passes it here. The flag
        # replaces the manifest value for this check only; it does
        # not change any other check's pass/fail outcome.
        flag_sha = args.expected_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", flag_sha):
            fail("--expected-sha256 must be a 64-character hexadecimal "
                 "sha256 digest")
        elif flag_sha != got_sha:
            fail(f"archive_sha256 mismatch: --expected-sha256 "
                 f"{flag_sha} != archive {got_sha}")
        else:
            report["checks"].append(
                "archive_sha256 matches the zip file (--expected-sha256)")
    elif not want_sha:
        fail("delivery manifest has no archive_sha256 (the copy "
             "shipped inside the zip is null by design — pass the "
             "zip's sha256 with --expected-sha256 <sha256>)")
    elif want_sha != got_sha:
        fail(f"archive_sha256 mismatch: manifest {want_sha} != "
             f"archive {got_sha}")
    else:
        report["checks"].append("archive_sha256 matches the zip file")

    inzip_manifest_name = None
    for name in names:
        if name.endswith("release/DELIVERY_MANIFEST.json"):
            inzip_manifest_name = name
    if not inzip_manifest_name:
        fail("DELIVERY_MANIFEST.json not present inside the archive")
        inzip_manifest = None
    else:
        inzip_manifest = json.loads(
            zf.read(inzip_manifest_name).decode("utf-8"))
        disk_copy = dict(manifest)
        inzip_copy = dict(inzip_manifest)
        disk_copy.pop("archive_sha256", None)
        inzip_copy.pop("archive_sha256", None)
        # archive_sha256_note differs by design (null in-zip, filled in
        # on disk); compare everything else.
        disk_copy.pop("archive_sha256_note", None)
        inzip_copy.pop("archive_sha256_note", None)
        if disk_copy != inzip_copy:
            fail("in-zip DELIVERY_MANIFEST.json differs from the on-disk "
                 "copy beyond archive_sha256")
        else:
            report["checks"].append(
                "in-zip manifest matches on-disk manifest (except "
                "archive_sha256, by design)")

    # 3. Listed files: presence + hash + size.
    listed = manifest.get("files") or []
    missing, mismatched = [], []
    for entry in listed:
        name = entry.get("path")
        want = entry.get("sha256")
        if name is None:
            mismatched.append(f"entry without path: {entry}")
            continue
        if name not in names:
            missing.append(name)
            continue
        data = zf.read(name)
        if want is None:
            # The manifest itself: hash cannot be self-referential.
            if len(data) != entry.get("bytes"):
                mismatched.append(f"{name}: bytes {len(data)} != "
                                  f"{entry.get('bytes')}")
            continue
        if _sha256_bytes(data) != want:
            mismatched.append(f"{name}: sha256 mismatch")
        elif len(data) != entry.get("bytes"):
            mismatched.append(f"{name}: bytes {len(data)} != "
                              f"{entry.get('bytes')}")
    if missing:
        fail(f"manifest files missing from archive: {missing[:10]}")
    if mismatched:
        fail(f"manifest file mismatches: {mismatched[:10]}")
    if not missing and not mismatched:
        report["checks"].append(
            f"{len(listed)} manifest files present with matching "
            "sha256/bytes (manifest itself checked by size only)")

    # 4+5. Extract to a clean temp dir and verify content + journeys.
    with tempfile.TemporaryDirectory(prefix="tk-handoff-verify-") as tmp:
        tmp = Path(tmp)
        zf.extractall(tmp)
        zf.close()
        roots = [d for d in tmp.iterdir() if d.is_dir()]
        if len(roots) != 1:
            fail(f"extracted archive must contain exactly one top "
                 f"directory, got {[d.name for d in roots]}")
            print(json.dumps(report, indent=2))
            return 1
        root = roots[0]
        # 4. Forbidden content scan over extracted text files.
        home, pem = _forbidden_markers()
        hits = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix in FORBIDDEN_SUFFIXES:
                hits.append(f"forbidden file: {p.relative_to(root)}")
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                continue  # binary artifact (wheel/sdist/tarball)
            if home in text:
                hits.append(f"{p.relative_to(root)}: contains the "
                            "machine home path")
            elif pem.search(text):
                hits.append(f"{p.relative_to(root)}: contains a "
                            "private-key block")
        if hits:
            fail(f"forbidden content in extracted files: {hits[:10]}")
        else:
            report["checks"].append(
                "no machine paths, private-key markers or secret files "
                "in the extracted tree")

        # 5. Journeys from the extracted copy.
        src = root / "src"
        if not (src / "turnkeep").is_dir():
            fail("extracted copy has no src/turnkeep; cannot run journeys")
            print(json.dumps(report, indent=2))
            return 1
        env = {"PYTHONPATH": str(src)}
        demo = _run([sys.executable, "-m", "turnkeep", "--db",
                     str(tmp / "demo.db"), "--json", "demo", "--no-model"],
                    cwd=root, env=env)
        if demo.returncode != 0 or \
                "SCRIPTED_SIMULATION_NO_MODEL_CALLS" not in demo.stdout:
            fail("J01 zero-key demo failed from the extracted copy: "
                 f"{(demo.stdout + demo.stderr)[-800:]}")
        else:
            report["checks"].append(
                "J01 zero-key demo PASS from the extracted copy")

        db = tmp / "verify.db"
        base = [sys.executable, "-m", "turnkeep", "--db", str(db),
                "--json"]

        def cli(*argv):
            return _run([*base, *argv], cwd=root, env=env)

        init = cli("init", "--scope", "verify")
        if init.returncode != 0:
            fail(f"CLI init failed from extracted copy: "
                 f"{init.stderr[-500:]}")
        else:
            rem = cli("remember", "--scope", "verify", "--key", "demo.req",
                      "--value", "export-format v1")
            rec = None
            try:
                rec = json.loads(rem.stdout)
            except Exception:  # noqa: BLE001
                pass
            if rem.returncode != 0 or not rec or \
                    rec.get("state") != "active" or not rec.get("id"):
                fail("CLI remember failed from extracted copy: "
                     f"{(rem.stdout + rem.stderr)[-500:]}")
            else:
                beg = cli("begin", "--scope", "verify",
                          "--task", "verify journey")
                run_id = None
                try:
                    run_id = json.loads(beg.stdout)["run_id"]
                except Exception:  # noqa: BLE001
                    pass
                if beg.returncode != 0 or not run_id:
                    fail("CLI begin failed from extracted copy: "
                         f"{(beg.stdout + beg.stderr)[-500:]}")
                else:
                    fin = cli("finish", "--scope", "verify", "--run",
                              run_id, "--key", "demo.done",
                              "--value", "verified",
                              "--idempotency-key", "t048-verify-1")
                    receipt = None
                    try:
                        receipt = json.loads(fin.stdout)
                    except Exception:  # noqa: BLE001
                        pass
                    if fin.returncode != 0 or not receipt or \
                            not receipt.get("record_id") or \
                            receipt.get("commit_epoch") is None:
                        fail("CLI finish failed from extracted copy: "
                             f"{(fin.stdout + fin.stderr)[-500:]}")
                    else:
                        report["checks"].append(
                            "CLI remember/begin/finish round trip PASS "
                            "from the extracted copy "
                            f"(receipt record_id="
                            f"{receipt['record_id'][:8]}..., "
                            f"commit_epoch={receipt['commit_epoch']})")

    report["status"] = "PASS" if not report["failures"] else "FAIL"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
