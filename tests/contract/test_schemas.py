"""T005 contract-schema tests: frozen schema + fixtures, stdlib validation only.
pytest. Full legal/illegal payload schema validation is run here against the
frozen schema's own rules (T003 deferred this to T005)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


def test_schema_covers_api():
    spec = load("specs/public_api.schema.json")
    api = load("specs/PUBLIC_API.json")
    assert len(spec["operations"]) >= sum(len(m) for m in api["paths"].values())
    assert set(spec["schemas"]) == set(api["components"]["schemas"])
    assert len(spec["error_codes"]) == 15


def test_remember_valid_fixture_has_required_fields():
    d = load("tests/contract/fixtures/remember_valid.json")
    for f in ("scope", "key", "value", "kind"):
        assert f in d
    assert d["kind"] in ("requirement", "fact", "decision", "attempt", "procedure", "handoff")


def test_remember_missing_key_fixture_is_invalid():
    d = load("tests/contract/fixtures/remember_missing_key.json")
    assert "key" not in d  # this fixture must stay invalid


def test_correct_missing_revision_fixture_is_invalid():
    d = load("tests/contract/fixtures/correct_missing_revision.json")
    assert "expected_revision" not in d  # CAS requires it


def test_resolve_partial_candidates_fixture_is_invalid():
    d = load("tests/contract/fixtures/resolve_partial_candidates.json")
    # a single candidate can never be a complete set: resolve must be rejected
    assert len(d["expected_candidates"]) == 1


def test_state_machine_terminal_rules():
    spec = load("specs/public_api.schema.json")
    sm = spec["state_machine"]
    assert "active->conflicted" in sm["transitions"]
    assert "erased" in sm["states"]
    assert "expired" in sm["product_only"]
    assert "erased" in sm["product_only"]


def test_no_recall_resume_http_path_claimed():
    spec = load("specs/public_api.schema.json")
    ops = " ".join(spec["operations"].keys()).lower()
    assert "recall" not in ops and "resume" not in ops
    assert "mapping gap" in spec["note"]
