"""Regression repro for the Turnkeep R21 rework (P2-R20-01 doc fix).

The R20 verdict (fourth conversation, round 5/5): substantive PASS
but not a formal PASS — 0 new P0, 0 new P1, 1 new non-blocking P2
(P2-R20-01), P0-R18-01 and P0-R19-01 both literally confirmed
CLOSED, but the reply carries no literal "FINAL_VERDICT:" line
(same as R17). P2-R20-01 is a DOCS issue, verified TRUE: the
purge_residue() docstring stated unconditionally that a failed
cleanup PERSISTS a purge_pending marker and that the residual
report stays NOT_CLEAN — contradicting the same docstring's
P0-R19-01 section, which already records the two boundaries where
no marker is persisted yet: (a) the retry budget is spent with the
write lock still held (B2: error says "marker could not be
persisted", purge_pending is 0, the report reads clean until a
retry persists the marker); (b) the caller already has a
transaction open (had_txn: the marker joins the caller's
transaction and the caller owns the commit). Behaviour is correct
and unchanged — this script checks DOCUMENTATION only, no code.

Sections (all doc checks, 8 items):
  D1: purge_residue() docstring qualifies the P0-R16-03 sentence
      with "when the failure settlement commits".
  D2: purge_residue() docstring states the cannot-commit case
      ("no marker is persisted yet").
  D3: purge_residue() docstring names the unpersisted marker in
      the error ("marker could not be persisted").
  D4: the unconditional P0-R16-03 sentence is gone (no
      "PERSISTS a purge_pending marker (one row per scope,
      live-DB-only table): residual_report()" verbatim).
  D5: the unconditional Returns sentence is gone (no
      'On failure "cleaned" is False AND a purge_pending marker is
      persisted for every affected scope' verbatim).
  D6: C-1 fixed — _set_purge_pending() docstring says "while a
      persisted marker stands" and no longer claims the report
      "never calls the tables clean while a failed cleanup stands".
  D7: §33 exists in docs/KNOWN_LIMITATIONS.md and records the R20
      verdict (no literal FINAL_VERDICT line; both P0s literally
      CLOSED) and the P2-R20-01 fix.
  D8: no "R20 not yet sent" remains anywhere in
      KNOWN_LIMITATIONS.md, and §33 carries no TBD placeholder.

Usage:
  TURNKEEP_SRC=<src dir> python3 repro_r21.py   (source / RC)
  <wheel-venv>/bin/python repro_r21.py           (wheel)
For the wheel run, copy this script to a directory OUTSIDE the
source tree first (else the doc checks find the source docs and
run full). On a wheel run the doc checks SKIP by design (8 skipped)
because the docs are not adjacent to the installed package.
Temp dirs live under this file's directory (persistent disk, never
/tmp); removed on success via atexit, kept on failure for inspection.
"""
import atexit
import os
import re
import shutil
import sys
from pathlib import Path

import turnkeep  # noqa: F401

SCRIPT_DIR = Path(__file__).resolve().parent
VERIFY_ROOT = SCRIPT_DIR.parent
TMPROOT = Path(os.environ.get("TURNKEEP_VERIFY_TMPDIR")
               or SCRIPT_DIR / "tmp-r21")
# A previous failed run keeps its TMPROOT for inspection; a new run
# starts clean. Concurrent runs must use distinct
# TURNKEEP_VERIFY_TMPDIR values.
if TMPROOT.exists():
    shutil.rmtree(TMPROOT)
TMPROOT.mkdir(parents=True, exist_ok=True)
CHECKS = []
SKIPS = []


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""),
          flush=True)


def skip(name, why):
    SKIPS.append(name)
    print("SKIP " + name + f"  [{why}]")


def _cleanup():
    fails = [n for n, ok in CHECKS if not ok]
    if not fails:
        shutil.rmtree(TMPROOT, ignore_errors=True)
    else:
        print(f"\n(keeping TMPROOT for inspection: {TMPROOT})")


atexit.register(_cleanup)

print(f"turnkeep.__file__ = {turnkeep.__file__}", flush=True)


def _norm(text):
    """Collapse all whitespace runs to single spaces, so a sentence
    split across docstring lines still matches verbatim."""
    return re.sub(r"\s+", " ", text)


# ------------------------------------------------------- §D doc checks
print("\n== §D docs (P2-R20-01 docstring fix + §33) ==", flush=True)
pkg_dir = Path(turnkeep.__file__).resolve().parent
root = None
for parent in [pkg_dir.parent, pkg_dir.parent.parent,
               pkg_dir.parent.parent.parent]:
    if (parent / "docs" / "KNOWN_LIMITATIONS.md").exists():
        root = parent
        break
DOC_NAMES = (
    "D1: purge_residue docstring qualifies the P0-R16-03 sentence "
    "('when the failure settlement commits')",
    "D2: purge_residue docstring states the cannot-commit case "
    "('no marker is persisted yet')",
    "D3: purge_residue docstring names the unpersisted marker "
    "('marker could not be persisted')",
    "D4: unconditional P0-R16-03 sentence is gone",
    "D5: unconditional Returns sentence is gone",
    "D6: C-1 fixed — _set_purge_pending docstring says 'while a "
    "persisted marker stands', no 'never calls the tables clean "
    "while a failed cleanup stands'",
    "D7: §33 exists and records the R20 verdict + P2-R20-01 fix",
    "D8: no 'R20 not yet sent' remains; no TBD placeholder in §33",
)
if root is None:
    for n in DOC_NAMES:
        skip(n, "docs not adjacent (wheel?)")
else:
    era_path = root / "src" / "turnkeep" / "erasure.py"
    era = era_path.read_text(encoding="utf-8")
    era_norm = _norm(era)
    # The purge_residue docstring is the block starting at the
    # "def purge_residue" docstring; slice from the P0-R16-03
    # sentence to the end of that docstring (the first '"""' after
    # it) so the assertions pin to this docstring, not to §32.
    start = era.find("P0-R16-03: a failed cleanup PERSISTS")
    assert start != -1, "P0-R16-03 sentence not found in erasure.py"
    end = era.find('"""', start)
    assert end != -1, "purge_residue docstring end not found"
    purge_doc = _norm(era[start:end])
    check(DOC_NAMES[0],
          "when the failure settlement commits" in purge_doc,
          "P0-R16-03 sentence")
    check(DOC_NAMES[1],
          "no marker is persisted yet" in purge_doc,
          "cannot-commit case")
    check(DOC_NAMES[2],
          "marker could not be persisted" in purge_doc,
          "unpersisted-marker error text")
    check(DOC_NAMES[3],
          "PERSISTS a purge_pending marker (one row per scope, "
          "live-DB-only table): residual_report()" not in purge_doc,
          "unconditional sentence absent")
    check(DOC_NAMES[4],
          'On failure "cleaned" is False AND a purge_pending marker '
          "is persisted for every affected scope" not in purge_doc,
          "unconditional Returns sentence absent")
    # C-1: _set_purge_pending docstring — slice from its
    # residual_report() sentence to the docstring end. Anchor at
    # "def _set_purge_pending": the same sentence also opens the
    # _set_maintenance_pending docstring above it.
    fn_start = era.find("def _set_purge_pending(")
    assert fn_start != -1, "def _set_purge_pending not found"
    c1_start = era.find("residual_report() consults it: a pending row",
                        fn_start)
    assert c1_start != -1, "_set_purge_pending docstring not found"
    c1_end = era.find('"""', c1_start)
    assert c1_end != -1, "_set_purge_pending docstring end not found"
    c1_doc = _norm(era[c1_start:c1_end])
    check(DOC_NAMES[5],
          "while a persisted marker stands" in c1_doc
          and "never calls the tables clean while a failed cleanup "
          "stands" not in c1_doc,
          "_set_purge_pending docstring")
    kl = (root / "docs" / "KNOWN_LIMITATIONS.md").read_text(
        encoding="utf-8")
    lines = kl.splitlines()
    sec33 = kl.split("## 33.", 1)[1] if "## 33." in kl else ""
    check(DOC_NAMES[6],
          any(l.startswith("## 33.") for l in lines)
          and "no literal" in sec33.lower()
          and "FINAL_VERDICT" in sec33
          and "P0-R18-01" in sec33 and "P0-R19-01" in sec33
          and "CLOSED" in sec33
          and "P2-R20-01" in sec33,
          "§33 heading + verdict + fix record")
    check(DOC_NAMES[7],
          "R20 not yet sent" not in kl
          and "TBD" not in sec33,
          "no stale 'not yet sent'; no TBD in §33")

# ---------------------------------------------------------------- summary
fails = [n for n, ok in CHECKS if not ok]
print(f"\n== summary ==\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed"
      + (f" ({len(SKIPS)} skipped: {', '.join(SKIPS)})" if SKIPS else ""))
if fails:
    print("FAILED:", *fails, sep="\n  ")
    sys.exit(1)
