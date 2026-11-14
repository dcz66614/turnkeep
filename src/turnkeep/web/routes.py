"""Route table for the local workbench API (T027). One dispatch per route,
all calling the same core functions as CLI/MCP. No route reads files or
exposes the DB path.
"""
from __future__ import annotations

import json

from .. import briefs as _br
from .. import commits as _com
from .. import corrections as _corr
from .. import memory as _mem
from .. import recovery as _rec
from .. import retrieval as _ret
from .. import runs as _runs
from ..context import from_request
from ..errors import TurnkeepError
from ..transports.context import bind_for_server
from ..transports.policy import validate_request_body

_ROUTES = [
    ("GET", "/api/v1/health", "health"),
    ("GET", "/api/v1/scopes", "scopes"),
    ("GET", "/api/v1/runs", "runs"),
    ("GET", "/api/v1/impact", "impact"),
    ("GET", "/api/v1/conflicts", "conflicts"),
    ("GET", "/api/v1/recall", "recall"),
    ("GET", "/api/v1/brief", "brief"),
    ("POST", "/api/v1/remember", "remember"),
    ("POST", "/api/v1/begin", "begin"),
    ("POST", "/api/v1/correct", "correct"),
    ("POST", "/api/v1/retract", "retract"),
    ("POST", "/api/v1/resolve_conflict", "resolve_conflict"),
    ("POST", "/api/v1/finish", "finish"),
    ("POST", "/api/v1/resume", "resume"),
    ("POST", "/api/v1/rebase", "rebase"),
]

# POST ops on which PUBLIC_API.json requires the Idempotency-Key header
# (required: true, 8-128 chars). Enforced in web/app.py; the generated
# openapi() above documents the header on exactly these routes.
IDEMPOTENT_ROUTES = frozenset({"remember", "begin", "finish", "correct",
                               "retract", "resolve_conflict"})


class RouteTable:
    def __init__(self, routes):
        self.routes = list(routes)

    def match(self, method, path):
        for m, p, name in self.routes:
            if m == method and p == path:
                return name
        return None

    def openapi(self):
        paths = {}
        for m, p, name in self.routes:
            op = {
                "operationId": name,
                "responses": {"200": {"description": "ok"},
                              "400": {"description": "INVALID_INPUT"},
                              "401": {"description": "UNAUTHORIZED"},
                              "403": {"description": "FORBIDDEN"},
                              "404": {"description": "NOT_FOUND"},
                              "409": {"description": "conflict (VERSION_CONFLICT / STALE_SNAPSHOT / IDEMPOTENCY_MISMATCH)"}},
            }
            if name in IDEMPOTENT_ROUTES:
                # PUBLIC_API.json: Idempotency-Key header, required, 8-128 chars.
                op["parameters"] = [{
                    "in": "header", "name": "Idempotency-Key", "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 128}}]
            paths.setdefault(p, {})[m.lower()] = op
        return {"openapi": "3.0.3",
                "info": {"title": "Turnkeep local workbench API", "version": "0.1.0"},
                "servers": [{"url": "http://127.0.0.1:{port}", "description": "loopback only"}],
                "paths": paths}


ROUTES = RouteTable(_ROUTES)


def _req(body, *fields):
    missing = [f for f in fields if f not in body]
    if missing:
        raise TurnkeepError("INVALID_INPUT", f"missing fields: {', '.join(missing)}")
    return [body[f] for f in fields]


def dispatch(db, scope, ctx, route, body, query, idempotency_key=""):
    """idempotency_key is the Idempotency-Key HEADER value supplied by
    web/app.py (the spec'd source). The JSON-body "idempotency_key" is a
    fallback for direct dispatch callers / legacy finish clients; the
    header wins when both are present and they disagree. The header is
    required (8-128 chars) on the six POST ops — enforced in app.py,
    not here, so direct dispatch callers keep the old behaviour."""
    body = body or {}
    # Effective key for this request: header first, body fallback.
    idempotency_key = idempotency_key or body.get("idempotency_key", "") or ""
    # P1-d: every request goes through the shared transport policy before any
    # core call. from_request() returns ctx unchanged but raises FORBIDDEN if
    # the body tries to override the bound actor/tenant/user; the policy
    # check additionally rejects path-like source ids in body["sources"] and
    # re-checks the scope against the server-side binding (built here from
    # the RequestContext app.py constructed at connect time — the client
    # never supplies scope or principal).
    ctx = from_request(ctx, body)
    tbinding = bind_for_server(scope, ctx.principal_binding, actor=ctx.actor)
    _policy_body = {"source_refs": body.get("sources") or []}
    for _field in ("actor_id", "tenant_id", "user_id"):
        if _field in body:
            _policy_body[_field] = body[_field]
    validate_request_body(tbinding, _policy_body, scope)
    actor = ctx.principal_binding
    if route == "scopes":
        from ..scopes import list_scopes
        return {"scopes": list_scopes(db)}
    if route == "runs":
        rows = db.execute(
            "SELECT id, task, status, created_at, accepted_at FROM runs "
            "WHERE scope_id=? ORDER BY created_at DESC", (scope,)).fetchall()
        return {"runs": [dict(r) for r in rows]}
    if route == "impact":
        from .. import lineage as _lin
        rid = query.get("record_id")
        if not rid:
            raise TurnkeepError("INVALID_INPUT", "record_id query parameter is required")
        return {"record_id": rid, "paths": _lin.impact(db, scope, rid)}
    if route == "conflicts":
        from ..memory import list_conflicts
        return {"conflicts": list_conflicts(db, scope, query.get("key") or None)}
    if route == "health":
        from ..storage import scope_epoch
        try:
            epoch = scope_epoch(db, scope)
        except TurnkeepError:
            epoch = None  # scope not registered yet — still healthy
        return {"status": "ok", "scope": scope, "epoch": epoch}
    if route == "recall":
        return {"scope": scope,
                "records": _ret.recall(db, scope, query.get("query", ""),
                                       include_history=query.get("history") == "1")}
    if route == "brief":
        return _br.build_brief(db, scope, goal=query.get("goal", ""))
    if route == "remember":
        key, value = _req(body, "key", "value")
        return _mem.remember(db, scope, key, value,
                             kind=body.get("kind", "fact"),
                             source_refs=body.get("sources") or [{"id": "web:1", "type": "user_statement"}],
                             parent_ids=body.get("parents", []),
                             expected_revision=body.get("expected_revision"),
                             replaces_ids=body.get("replaces", []),
                             actor_binding=actor,
                             idempotency_key=idempotency_key)
    if route == "begin":
        (task,) = _req(body, "task")
        return _runs.begin(db, scope, task, actor_binding=actor,
                           idempotency_key=idempotency_key)
    if route == "correct":
        rid, rev, value = _req(body, "record_id", "expected_revision", "new_value")
        return _corr.correct(db, scope, rid, rev, value,
                             source_refs=body.get("sources") or [{"id": "web:1", "type": "user_statement"}],
                             actor_binding=actor,
                             idempotency_key=idempotency_key)
    if route == "retract":
        rid, rev, reason = _req(body, "record_id", "expected_revision", "reason")
        return _corr.retract(db, scope, rid, rev, reason, actor_binding=actor,
                             idempotency_key=idempotency_key)
    if route == "resolve_conflict":
        key, value, candidates = _req(body, "key", "value", "candidates")
        return _corr.resolve_conflict(db, scope, key, candidates, value,
                                      source_refs=body.get("sources") or [{"id": "web:1", "type": "user_statement"}],
                                      actor_binding=actor,
                                      idempotency_key=idempotency_key)
    if route == "finish":
        run_id, key, value = _req(body, "run_id", "key", "value")
        return _com.finish(db, scope, run_id, {"key": key, "value": value},
                           parent_ids=body.get("parents", []),
                           idempotency_key=idempotency_key,
                           actor_binding=actor)
    if route == "resume":
        (run_id,) = _req(body, "run_id")
        return _rec.resume(db, scope, run_id)
    if route == "rebase":
        (run_id,) = _req(body, "run_id")
        return _rec.rebase(db, scope, run_id, actor_binding=actor,
                           candidate_result=body.get("candidate_result"))
    raise TurnkeepError("NOT_FOUND", f"unknown route {route}")
