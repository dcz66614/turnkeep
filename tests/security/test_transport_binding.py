"""T022 transport-binding tests."""
import pytest
from turnkeep.transports.context import TransportBinding, bind_for_server
from turnkeep.transports.policy import validate_request_body
from turnkeep.errors import TurnkeepError

B = bind_for_server("s1", "mcp:claude-code", actor="claude")


def test_allowed_scope_passes():
    B.check_scope("s1")


def test_other_scope_forbidden():
    with pytest.raises(TurnkeepError) as ei:
        B.check_scope("s2")
    assert ei.value.code == "FORBIDDEN"


def test_body_actor_override_forbidden():
    with pytest.raises(TurnkeepError) as ei:
        validate_request_body(B, {"actor_id": "mallory"}, "s1")
    assert ei.value.code == "FORBIDDEN"


def test_body_cross_scope_run_forbidden():
    with pytest.raises(TurnkeepError) as ei:
        validate_request_body(B, {}, "s2")
    assert ei.value.code == "FORBIDDEN"


def test_unregistered_source_path_rejected():
    with pytest.raises(TurnkeepError) as ei:
        validate_request_body(B, {"source_refs": [{"id": "/etc/passwd", "type": "imported_text"}]}, "s1")
    assert ei.value.code == "INVALID_INPUT"


def test_clean_body_passes():
    assert validate_request_body(B, {"source_refs": [{"id": "doc:1", "type": "user_statement"}]}, "s1") is True
