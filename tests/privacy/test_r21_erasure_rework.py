"""R21 rework tests for P2-R20-01 (docs only, no behaviour change).

P2-R20-01 (verified TRUE, non-blocking): the purge_residue()
docstring stated unconditionally that a failed cleanup PERSISTS a
purge_pending marker and that the residual report stays NOT_CLEAN
— contradicting the same docstring's P0-R19-01 section, which
already records the two boundaries where no marker is persisted
yet: (a) the retry budget is spent with the write lock still held
(B2: error says "marker could not be persisted", purge_pending is
0, the report reads clean until a retry persists the marker);
(b) the caller already has a transaction open (had_txn: the marker
joins the caller's transaction and the caller owns the commit).
Behaviour is correct and unchanged — these tests assert
DOCUMENTATION only: the docstring now distinguishes the three
states (marker committed / marker not yet committed (had_txn) /
marker unpersistable because the retry budget is spent), the
same-class C-1 in _set_purge_pending's docstring is qualified, and
docs/KNOWN_LIMITATIONS.md §33 records the R20 verdict and the fix
with no placeholder left.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _norm(text):
    return re.sub(r"\s+", " ", text)


def _purge_docstring():
    era = (REPO_ROOT / "src" / "turnkeep" / "erasure.py").read_text(
        encoding="utf-8")
    start = era.find("P0-R16-03: a failed cleanup PERSISTS")
    assert start != -1
    end = era.find('"""', start)
    assert end != -1
    return _norm(era[start:end])


def _set_purge_pending_docstring():
    era = (REPO_ROOT / "src" / "turnkeep" / "erasure.py").read_text(
        encoding="utf-8")
    # Anchor at "def _set_purge_pending": the same sentence also
    # opens the _set_maintenance_pending docstring above it.
    fn_start = era.find("def _set_purge_pending(")
    assert fn_start != -1
    start = era.find("residual_report() consults it: a pending row",
                     fn_start)
    assert start != -1
    end = era.find('"""', start)
    assert end != -1
    return _norm(era[start:end])


def test_purge_residue_docstring_distinguishes_three_states():
    """(D1–D3) The P0-R16-03 sentence and the Returns paragraph
    qualify persistence with "when the failure settlement commits",
    state the cannot-commit case ("no marker is persisted yet"),
    and name the unpersisted marker in the error text."""
    doc = _purge_docstring()
    assert "when the failure settlement commits" in doc
    assert "no marker is persisted yet" in doc
    assert "marker could not be persisted" in doc
    # The three states are all named: committed / had_txn not yet
    # committed / budget spent.
    assert "had_txn" in doc
    assert "retry budget is spent" in doc


def test_purge_residue_docstring_no_unconditional_sentences():
    """(D4–D5) The two unconditional sentences are gone: the
    P0-R16-03 sentence no longer claims persistence before
    "residual_report()", and the Returns paragraph no longer claims
    a marker "is persisted for every affected scope" without the
    settlement-commits qualifier."""
    doc = _purge_docstring()
    assert ("PERSISTS a purge_pending marker (one row per scope, "
            "live-DB-only table): residual_report()") not in doc
    assert ('On failure "cleaned" is False AND a purge_pending marker '
            "is persisted for every affected scope") not in doc


def test_set_purge_pending_docstring_qualified():
    """(D6, C-1) _set_purge_pending's docstring no longer claims
    the report "never calls the tables clean while a failed cleanup
    stands" — it now says "while a persisted marker stands" and
    qualifies that a failed cleanup whose settlement could not
    commit leaves no marker yet."""
    doc = _set_purge_pending_docstring()
    assert "while a persisted marker stands" in doc
    assert ("never calls the tables clean while a failed cleanup "
            "stands") not in doc
    assert "settlement could not commit" in doc


def test_known_limitations_section_33():
    """(D7–D8) §33 exists and records the R20 verdict (substantive
    PASS, no literal FINAL_VERDICT line, both P0s literally
    CLOSED) and the P2-R20-01 fix; no "R20 not yet sent" remains
    anywhere in the file and §33 carries no placeholder."""
    docs = REPO_ROOT / "docs" / "KNOWN_LIMITATIONS.md"
    text = docs.read_text(encoding="utf-8")
    assert "## 33." in text
    sec33 = text.split("## 33.", 1)[1]
    assert "FINAL_VERDICT" in sec33
    assert "no literal" in sec33.lower()
    assert "P0-R18-01" in sec33 and "P0-R19-01" in sec33
    assert "CLOSED" in sec33
    assert "P2-R20-01" in sec33
    assert "R20 not yet sent" not in text
    assert "TBD" not in sec33
    assert "PLACEHOLDER" not in sec33
    assert "## 32." in text
    sec32 = text.split("## 32.", 1)[1].split("## 33.", 1)[0]
    assert "R20 sent" in sec32
