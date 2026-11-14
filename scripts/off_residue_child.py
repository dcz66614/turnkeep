"""OFF-residue fixture driver for the R15 rework (P0-R15-01).

Spawned by scripts/repro_r16.py and
tests/privacy/test_r16_erasure_rework.py as an INDEPENDENT subprocess.
It reproduces, against the CURRENT product code, the residue a
pre-fix RC would leave behind: the OFF chain (B PARTIAL, A COMPLETE,
repair B COMPLETE) with the secure_delete gate AND the file-level
purge acceptance both bypassed, then os._exit(77) — no normal close,
no final checkpoint — so the deleted keys' bytes stay in the main DB
file (the checkpoint inside _run_maintenance copied the freeblock
pages; VACUUM's compact pages sit in the WAL, un-checkpointed).

The double bypass exists ONLY in this fixture script, never in
product code: it is what lets the test build the historical-residue
database that purge_residue is meant to clean. A clean close would
clear the residue (the close checkpoints the WAL's VACUUM pages into
the main file), which is why the child crashes instead of closing.

Usage:
    off_residue_child.py <db_path> <a_id> <a_rev> <b_id> <b_rev> [key_out] [scope]

The parent creates the two records (both holding PIN 4827) first;
the child only runs the erases. Exit codes: 77 = crashed after the
chain (expected); 0 = the chain did not complete (harness failure);
2 = usage error; 1 = any other error (traceback on stderr).

The optional [key_out] sidecar (R16 rework, P0-R16-01): after B's
PARTIAL erase the child records B's per-record verification key
hex there, so the parent can check — without any prior knowledge
of the key — whether that key's bytes are still recoverable from
the live DB file after purge_residue() runs. The value itself is
never passed to the child (it never needs it); the parent chooses
the value (4-digit PIN or 6-digit value) when creating the records.

The optional [scope] (R20 rework, P0-R19-01 A2): the scope the
records live in and the OFF chain runs in; default "s". Old calls
with 6-7 positional arguments behave exactly as before.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from turnkeep.storage import open_db  # noqa: E402
from turnkeep import erasure as E  # noqa: E402
from turnkeep.erasure import erase_record  # noqa: E402


def main(argv):
    if len(argv) not in (6, 7, 8):
        print(__doc__, file=sys.stderr)
        return 2
    db_path, a_id, a_rev, b_id, b_rev = argv[1], argv[2], int(argv[3]), argv[4], int(argv[5])
    key_out = argv[6] if len(argv) >= 7 else None
    scope = argv[7] if len(argv) == 8 else "s"
    db = open_db(Path(db_path))
    db.execute("PRAGMA secure_delete=OFF")
    # Fixture-only double bypass (see the module docstring): the gate
    # would refuse an OFF erase, and the file check would downgrade
    # the repair to PARTIAL — both must be off to reproduce the
    # pre-fix residue (repair COMPLETE, key rows 0, marker 0).
    E._require_secure_delete = lambda _db: None
    E._verify_purge = lambda _db, _key, _hashes: True
    out_b = erase_record(db, scope, b_id, b_rev, "rotation")
    if out_b["status"] != "PARTIAL":
        print(f"B erase not PARTIAL: {out_b}", file=sys.stderr)
        return 0
    if key_out is not None:
        # B's per-record verification key row exists now (created by
        # the PARTIAL erase); record it before the repair deletes it.
        row = db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE record_id=?",
            (b_id,)).fetchone()
        if row is None:
            print("no record_erase_keys row for B after PARTIAL", file=sys.stderr)
            return 0
        Path(key_out).write_text(row[0], encoding="ascii")
    out_a = erase_record(db, scope, a_id, a_rev, "rotation")
    # The PARTIAL erase bumped B's revision: the repair needs the
    # current revision, not the pre-erase one.
    cur_b_rev = db.execute(
        "SELECT revision FROM memories WHERE scope_id=? AND id=?",
        (scope, b_id)).fetchone()[0]
    out_r = erase_record(db, scope, b_id, cur_b_rev, "repair")
    print(f"erase B: {out_b['status']}; erase A: {out_a['status']}; "
          f"repair B: {out_r['status']}", flush=True)
    if not (out_b["status"] == "PARTIAL" and out_a["status"] == "COMPLETE"
            and out_r["status"] == "COMPLETE"):
        print("OFF chain did not reach the expected outcomes", file=sys.stderr)
        return 0
    # Crash without closing: a clean close would checkpoint the WAL's
    # VACUUM pages into the main file and clear the residue.
    os.write(2, b"off_residue_child: crashing without closing\n")
    os._exit(77)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
