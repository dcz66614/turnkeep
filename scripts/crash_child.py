"""Crash-injection driver for the R14 rework (P0-R14-01).

Spawned by scripts/repro_r15.py and tests/privacy/test_r15_erasure_rework.py
as an INDEPENDENT subprocess; at the requested crash point it calls
os._exit(77) — no normal close, no transaction commit, no checkpoint —
so the parent can reopen the DB and inspect what the crash left behind.

Usage:
    crash_child.py <db_path> <record_id> <revision> <mode> <point> [reason]
      mode:  "repair" | "first"
      point: "pre_pending" | "post_commit" | "post_busy" | "post_checkpoint"
             | "mid_delete"
      reason: the erase reason (default "repair" for repair mode,
              "rotation" for first mode). A caller whose payload IS
              the default reason must pass a different one — the
              reason gate runs before any crash point is reached, so
              a payload-equal reason would raise INVALID_INPUT
              instead of crashing.

Crash points (see docs/KNOWN_LIMITATIONS.md §27):
  pre_pending:     crash inside the erase transaction, at the
                   _set_maintenance_pending call (the marker write
                   that now precedes the key deletion) — the
                   transaction never commits, so the erase rolls back
                   atomically: the record keeps its pre-erase state,
                   the key row is intact, no marker is written.
  post_commit:     crash right after the erase transaction commits,
                   at _run_maintenance — the marker is persisted, the
                   key is deleted, the checkpoint never ran.
  post_busy:       crash after the checkpoint returned busy, at
                   _restore_record_erase_key — the key restore never
                   ran. The parent must hold an ordinary read
                   transaction (BEGIN + SELECT COUNT(*) FROM memories)
                   so the checkpoint goes busy.
  post_checkpoint: crash after the checkpoint succeeded, at
                   _clear_maintenance_pending — the marker clear never
                   commits (stale marker: purge unconfirmed, the file
                   may already be clean).
  mid_delete:      crash inside the erase transaction, at
                   _delete_record_erase_key — AFTER the marker write
                   and BEFORE the key deletion (the R15 extra crash
                   check: os._exit(77) right after writing the marker
                   and before deleting the key). The transaction never
                   commits, so the erase rolls back atomically: the
                   marker is 0 rows, the key row is intact, the record
                   keeps its pre-erase state, the report reads
                   CONTROLLED_NOT_CLEAN.

Exit codes: 77 = crashed at the requested point (expected); 0 = the
erase returned normally, so the crash point was NOT hit (a harness
failure — the parent must treat it as one); 2 = usage error; 1 = any
other error (traceback on stderr).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from turnkeep.storage import open_db  # noqa: E402
from turnkeep import erasure as E  # noqa: E402
from turnkeep.erasure import erase_record  # noqa: E402


def _crash(*_args, **_kwargs):
    os.write(2, b"crash_child: crashing at the requested point\n")
    os._exit(77)


def main(argv):
    if len(argv) not in (6, 7):
        print(__doc__, file=sys.stderr)
        return 2
    db_path, record_id, revision, mode, point = argv[1:6]
    reason = argv[6] if len(argv) == 7 else None
    if mode not in ("repair", "first"):
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    if point == "pre_pending":
        E._set_maintenance_pending = _crash
    elif point == "mid_delete":
        E._delete_record_erase_key = _crash
    elif point == "post_commit":
        E._run_maintenance = _crash
    elif point == "post_busy":
        E._restore_record_erase_key = _crash
    elif point == "post_checkpoint":
        E._clear_maintenance_pending = _crash
    else:
        print(f"unknown point: {point}", file=sys.stderr)
        return 2
    db = open_db(Path(db_path))
    if reason is None:
        reason = "repair" if mode == "repair" else "rotation"
    out = erase_record(db, "s", record_id, int(revision), reason)
    # Reached only when the crash point was not hit: the parent must
    # treat a 0 exit as a harness failure, never as a pass.
    print(f"SURVIVED (crash point not hit): {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
