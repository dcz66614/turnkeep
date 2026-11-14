"""RequestContext (T008): transport-bound principal binding.

The principal (actor label + transport) comes from CLI/MCP session config.
Request bodies MUST NOT override it: from_request() ignores actor_id /
tenant_id / user_id fields, and raises FORBIDDEN if they are present AND
differ from the bound principal (a body that agrees is ignored, not trusted).
Actor is a label, not authentication (CORE_CONTRACT §3).
"""
from __future__ import annotations
from dataclasses import dataclass
from .errors import TurnkeepError
from .storage import _text


@dataclass(frozen=True)
class RequestContext:
    scope_id: str
    principal_binding: str  # e.g. "cli:local" or "mcp:claude-code:session-abc"
    actor: str = "local"

    @property
    def actor_binding(self):
        return self.principal_binding


def bind_context(*, scope_id, principal_binding, actor="local"):
    return RequestContext(scope_id=_text(scope_id, "scope_id"),
                          principal_binding=_text(principal_binding, "principal_binding"),
                          actor=_text(actor, "actor"))


def from_request(ctx, body):
    """Return ctx unchanged after validating the body cannot rebind identity."""
    if not isinstance(body, dict):
        raise TurnkeepError("INVALID_INPUT", "request body must be an object")
    for field in ("actor_id", "tenant_id", "user_id"):
        if field in body and body[field] is not None and str(body[field]) != ctx.actor                 and str(body[field]) != ctx.principal_binding:
            raise TurnkeepError("FORBIDDEN",
                                f"request body field {field} cannot override the bound principal")
    return ctx
