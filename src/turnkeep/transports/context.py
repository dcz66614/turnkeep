"""Transport context (T022). Every MCP connection is bound at connect time
from trusted LOCAL config to an allowed scope set + actor label.

- TransportBinding: allowed_scopes (frozenset), principal_binding, actor.
  A tool call may only select a scope inside allowed_scopes; the default
  is the single scope the server was started with.
- bind_for_server(scope, principal): single-scope binding (the T021 default).
- check_scope(binding, scope): FORBIDDEN otherwise. No global fallback.
- The binding is NOT authentication: it is a local trust boundary for one
  OS user (contract §1). Remote multi-tenant auth is out of scope.
"""
from __future__ import annotations
from dataclasses import dataclass
from ..errors import TurnkeepError
from ..storage import _text


@dataclass(frozen=True)
class TransportBinding:
    allowed_scopes: frozenset
    principal_binding: str
    actor: str = "local"

    def check_scope(self, scope_id):
        if scope_id not in self.allowed_scopes:
            raise TurnkeepError("FORBIDDEN",
                                f"scope {scope_id!r} is not allowed for this transport binding")


def bind_for_server(scope_id, principal_binding, *, actor="local"):
    return TransportBinding(frozenset([_text(scope_id, "scope")]),
                            _text(principal_binding, "principal_binding"),
                            _text(actor, "actor"))
