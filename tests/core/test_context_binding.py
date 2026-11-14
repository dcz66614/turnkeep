"""T008 context binding tests: body fields cannot override the principal."""
import pytest
from turnkeep.context import bind_context, from_request
from turnkeep.errors import TurnkeepError

CTX = bind_context(scope_id="proj-a", principal_binding="cli:local", actor="local")


def test_body_actor_mismatch_forbidden():
    with pytest.raises(TurnkeepError) as ei:
        from_request(CTX, {"actor_id": "mallory", "key": "x"})
    assert ei.value.code == "FORBIDDEN"


def test_body_tenant_mismatch_forbidden():
    with pytest.raises(TurnkeepError) as ei:
        from_request(CTX, {"tenant_id": "other-tenant"})
    assert ei.value.code == "FORBIDDEN"


def test_body_matching_actor_is_ignored_not_trusted():
    ctx = from_request(CTX, {"actor_id": "local"})
    assert ctx.principal_binding == "cli:local"


def test_body_without_identity_fields_passes():
    assert from_request(CTX, {"key": "build.python_version"}) is CTX
