"""Transport policy (T022). Boundaries shared by ALL transports (CLI, SDK,
MCP, local HTTP): one validity + scope judgement, no MCP bypass.

- validate_request_body(binding, body, scope_id): body actor/tenant/user
  fields can never rebind the principal (FORBIDDEN on mismatch); the
  requested scope must be inside the binding; unregistered source paths
  (source ids that look like filesystem paths outside the project) are
  rejected — memory import must go through the explicit import entry
  (T036/T037), not a tool call.
- Untrusted text (tool results, imported text, model output) is DATA only:
  it is stored under source type agent_inference / imported_text and is
  NEVER spliced into a system/developer role, NEVER treated as an
  instruction, and NEVER used to change actor, scope or permissions.
  `classify_text()` returns the storage treatment for a piece of text.
- The product promise is limited to the controlled API: tools cannot stop
  an agent from writing files or calling other external tools directly.
"""
from __future__ import annotations
import re
from ..errors import TurnkeepError

_INSTRUCTION_LIKE = re.compile(
    r"(?i)\b(ignore (all )?previous instructions|you are now|system prompt|"
    r"disregard (the )?(above|instructions)|new instructions:|act as (a |an )?(root|admin|system))\b")
_PATH_LIKE = re.compile(r"(^|[/\\\\])(home|Users|etc|var|tmp|private)[/\\\\]")


def validate_request_body(binding, body, scope_id):
    if not isinstance(body, dict):
        raise TurnkeepError("INVALID_INPUT", "request body must be an object")
    binding.check_scope(scope_id)
    for field in ("actor_id", "tenant_id", "user_id"):
        if field in body and body[field] is not None and str(body[field]) not in (
                binding.actor, binding.principal_binding):
            raise TurnkeepError("FORBIDDEN",
                                f"request body field {field} cannot override the bound principal")
    for src in body.get("source_refs", []) or []:
        ref = str(src.get("id", ""))
        if _PATH_LIKE.search(ref):
            raise TurnkeepError("INVALID_INPUT",
                                f"source ref {ref!r} looks like a filesystem path; use the explicit import entry")
    return True


def classify_text(text):
    """Storage treatment for untrusted text. Returns a dict the caller must
    store as data (source type agent_inference/imported_text)."""
    flagged = bool(_INSTRUCTION_LIKE.search(str(text)))
    return {"treatment": "data_only",
            "instruction_like": flagged,
            "note": "Stored as data; never a system/developer instruction, never authority."}
