"""Consistent backup, restore and crash diagnosis (T039).

- create_backup(): SQLite online backup API (a consistent snapshot of the
  live DB, WAL included) to a temp file, then an atomic rename; a sidecar
  .meta.json records sha256 / user_version / scope list / record count.
  The backup is verified (integrity_check + counts) before it is reported.
- restore_backup(): verifies the backup first (header, integrity_check,
  user_version, expected tables), refuses to overwrite an existing DB
  unless force=True, then copies atomically and verifies the restored DB.
- diagnose(): crash diagnosis for a DB path — integrity, journal mode,
  WAL presence, user_version, per-scope epoch / last event, and whether a
  -wal file holds uncheckpointed frames. Status OK / DEGRADED / CORRUPT
  with a recommended action. Never deletes or repairs anything itself.

Erasure interaction (T038): a backup is a point-in-time copy. Restoring a
backup taken BEFORE an erasure resurrects the erased payload — that is
why backups are listed as uncontrolled residuals, and why restore never
claims to be an erasure.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .errors import TurnkeepError
from .storage import SCHEMA_VERSION, open_db, _now

EXPECTED_TABLES = {"scopes", "memories", "sources", "memory_sources", "dependencies",
                   "replacements", "runs", "observed_reads", "events", "idempotency",
                   "import_batches", "schema_meta"}


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify_db(path):
    """Open read-only and check the backup is a usable Turnkeep DB.
    Returns a dict of facts; raises TurnkeepError on any failure."""
    path = Path(path)
    if not path.exists():
        raise TurnkeepError("NOT_FOUND", f"backup not found: {path}")
    with open(path, "rb") as f:
        header = f.read(16)
    if header[:6] != b"SQLite":
        raise TurnkeepError("INVALID_INPUT", f"not a SQLite file: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as e:
            raise TurnkeepError("INVALID_INPUT", f"backup failed integrity_check: {e}")
        if integrity != "ok":
            raise TurnkeepError("INVALID_INPUT", f"backup failed integrity_check: {integrity}")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise TurnkeepError("SCHEMA_VERSION",
                                f"backup schema version {version} != {SCHEMA_VERSION}")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = EXPECTED_TABLES - tables
        if missing:
            raise TurnkeepError("INVALID_INPUT", f"backup is missing tables: {sorted(missing)}")
        scopes = [r[0] for r in con.execute("SELECT id FROM scopes ORDER BY id")]
        records = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {"user_version": version, "scopes": scopes, "records": records,
                "integrity": "ok"}
    finally:
        con.close()


def create_backup(db, out_path):
    """Consistent online backup of the live connection. Returns the metadata."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=str(out.parent))
    os.close(fd)
    tmp = Path(tmp)
    try:
        target = sqlite3.connect(str(tmp))
        try:
            db.backup(target)
        finally:
            target.close()
        facts = _verify_db(tmp)
        meta = {"format": "turnkeep-backup", "schema_version": SCHEMA_VERSION,
                "created_at": _now(), "sha256": _sha256_file(tmp), **facts}
        os.replace(tmp, out)
        (Path(str(out) + ".meta.json")).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return {**meta, "path": str(out)}
    finally:
        if tmp.exists():
            tmp.unlink()


def restore_backup(backup_path, dest_path, *, force=False):
    """Verify, then restore. Refuses to overwrite unless force=True."""
    backup, dest = Path(backup_path), Path(dest_path)
    facts = _verify_db(backup)
    if dest.exists() and not force:
        raise TurnkeepError("INVALID_INPUT",
                            f"destination already exists (use force=True to overwrite): {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp)
    try:
        import shutil
        shutil.copyfile(backup, tmp)
        # The copy must be a clean, standalone DB: drop any stale WAL sidecars.
        for suffix in ("-wal", "-shm"):
            side = Path(str(tmp) + suffix)
            if side.exists():
                side.unlink()
        _verify_db(tmp)
        os.replace(tmp, dest)
        restored = open_db(dest)
        facts2 = _verify_db(dest)
        restored.close()
        return {"path": str(dest), "restored": True, **facts2}
    finally:
        if tmp.exists():
            tmp.unlink()


def diagnose(db_path):
    """Crash diagnosis. Read-only; never repairs or deletes anything."""
    path = Path(db_path)
    report = {"path": str(path), "exists": path.exists(), "status": "OK", "findings": []}
    if not path.exists():
        report["status"] = "CORRUPT"
        report["findings"].append("database file does not exist")
        report["action"] = "restore from a verified backup (restore_backup)"
        return report
    with open(path, "rb") as f:
        header = f.read(16)
    if header[:6] != b"SQLite":
        report["status"] = "CORRUPT"
        report["findings"].append("file header is not SQLite — the file is truncated or overwritten")
        report["action"] = "restore from a verified backup; do not open this file for writing"
        return report
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        report["findings"].append(f"WAL file present ({wal.stat().st_size} bytes) — "
                                  "uncheckpointed frames may exist; open read-only first")
        report["status"] = "DEGRADED"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as e:
            report["status"] = "CORRUPT"
            report["findings"].append(f"integrity_check raised: {e}")
            report["action"] = "restore from a verified backup"
            return report
        if integrity != "ok":
            report["status"] = "CORRUPT"
            report["findings"].append(f"integrity_check failed: {integrity}")
            report["action"] = "restore from a verified backup"
            return report
        report["findings"].append(f"integrity_check ok; journal_mode="
                                  f"{con.execute('PRAGMA journal_mode').fetchone()[0]}; "
                                  f"user_version={con.execute('PRAGMA user_version').fetchone()[0]}")
        for scope_id, epoch in con.execute("SELECT id, epoch FROM scopes ORDER BY id"):
            last = con.execute("SELECT MAX(created_at) FROM events WHERE scope_id=?", (scope_id,)).fetchone()[0]
            report["findings"].append(f"scope {scope_id}: epoch {epoch}, last event {last}")
    finally:
        con.close()
    if report["status"] == "OK":
        report["action"] = "safe to open normally"
    elif report["status"] == "DEGRADED":
        report["action"] = "open read-only, verify the last run, then checkpoint the WAL"
    return report
