"""Local workbench HTTP API (T027). Stdlib only — no FastAPI/Uvicorn
dependency, so the core install stays dependency-free (ui extra empty).

Loopback only (127.0.0.1 / localhost), per-process session token, Origin /
Host limits, 1 MiB body cap, no wildcard CORS, GET never mutates. Every
route calls the same core service functions as the CLI/MCP with a
RequestContext; errors use the same structured codes. No DB absolute path
is ever exposed, and there is no file-read endpoint (static assets are an
embedded page, not a filesystem read).
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..context import bind_context
from ..errors import TurnkeepError
from ..idempotency import validate_idempotency_key
from ..storage import open_db
from .routes import IDEMPOTENT_ROUTES, ROUTES, dispatch

MAX_BODY = 1_048_576  # 1 MiB


def make_token() -> str:
    return secrets.token_urlsafe(32)


class TurnkeepHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True  # loopback-only server; restart must not wait out TIME_WAIT

    def __init__(self, server_address, handler_class, db_path, scope, principal, token):
        if server_address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"refusing non-loopback bind {server_address[0]}: "
                             "workbench API is loopback-only (0.0.0.0 is never a fallback)")
        self.db_path = Path(db_path)
        self.scope = scope
        self.principal = principal
        self.token = token
        super().__init__(server_address, handler_class)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout protocol-clean
        import sys
        print(f"http: {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if origin and self._origin_is_loopback(origin):
            # Reflect only loopback origins (the workbench runs on this
            # machine); never a wildcard. The session token is still
            # required on every request.
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _origin_is_loopback(origin):
        o = urlparse(origin)
        return o.scheme == "http" and o.hostname in ("127.0.0.1", "localhost", "::1")

    def _error(self, status, code, message, details=None):
        self._send(status, {"error": {"code": code, "message": message,
                                      "details": details or {}}})

    def _check_origin_host(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin and not self._origin_is_loopback(origin):
            return False
        # Any loopback port is allowed: the Vite dev server (5173) and the
        # API server run on different loopback ports on the same machine.
        # The per-process session token remains required on every request.
        return True

    def _check_token(self):
        auth = self.headers.get("Authorization") or ""
        token = self.headers.get("X-Turnkeep-Token") or ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
        return token and secrets.compare_digest(token, self.server.token)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # Drain the body before rejecting: closing the connection
            # while the client is still sending makes Python 3.13's
            # http.client raise BrokenPipeError on send (3.12 tolerates
            # it). Read in bounded chunks and stop on disconnect, so a
            # huge Content-Length cannot block forever on a stalled peer.
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise TurnkeepError("INVALID_INPUT", f"request body too large: {length} > {MAX_BODY}")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise TurnkeepError("INVALID_INPUT", f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            raise TurnkeepError("INVALID_INPUT", "request body must be a JSON object")
        return body

    def _require_idempotency_key(self, route):
        """PUBLIC_API.json requires the Idempotency-Key header (8-128
        chars) on remember/begin/finish/correct/retract/resolve_conflict.
        Missing or out-of-range -> 400 INVALID_INPUT; nothing is stored.
        This is the spec'd source of the key — the JSON-body key is only
        a fallback inside dispatch(), and the header wins if both are
        present. Non-idempotent routes (GETs, resume, rebase) need no key."""
        if route not in IDEMPOTENT_ROUTES:
            return ""
        key = (self.headers.get("Idempotency-Key") or "").strip()
        if not key:
            raise TurnkeepError("INVALID_INPUT",
                                "Idempotency-Key header is required for this operation")
        validate_idempotency_key(key)
        return key

    def _handle(self, method):
        try:
            if not self._check_origin_host():
                self._error(403, "FORBIDDEN", "Host/Origin not allowed for this loopback server")
                return
            if not self._check_token():
                self._error(401, "UNAUTHORIZED", "missing or invalid local session token")
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/" and method == "GET":
                self._send(200, {"service": "turnkeep", "version": "0.1.0",
                                 "scope": self.server.scope,
                                 "api": "/api/v1/openapi.json"})
                return
            if path == "/api/v1/openapi.json" and method == "GET":
                self._send(200, ROUTES.openapi())
                return
            route = ROUTES.match(method, path)
            if route is None:
                self._error(404, "NOT_FOUND", f"no route {method} {path}")
                return
            body = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            idempotency_key = self._require_idempotency_key(route)
            db = open_db(self.server.db_path)
            try:
                ctx = bind_context(scope_id=self.server.scope,
                                   principal_binding=self.server.principal)
                out = dispatch(db, self.server.scope, ctx, route, body, query,
                               idempotency_key=idempotency_key)
            finally:
                db.close()
            self._send(200, out)
        except TurnkeepError as e:
            self._error(_http_status(e.code), e.code, str(e),
                        getattr(e, "details", {}) or {})
        except Exception as e:  # never leak internals / DB paths
            self._error(500, "INTERNAL", "internal error")

    def do_OPTIONS(self):
        # CORS preflight: loopback origins only, never a wildcard.
        origin = self.headers.get("Origin")
        if origin and self._origin_is_loopback(origin):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type, idempotency-key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._error(403, "FORBIDDEN", "Origin not allowed for this loopback server")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def _http_status(code):
    return {"NOT_FOUND": 404, "VERSION_CONFLICT": 409, "STALE_SNAPSHOT": 409,
            "IDEMPOTENCY_MISMATCH": 409, "CONFLICT_REQUIRES_RESOLVE": 409,
            "INVALID_INPUT": 400, "VALIDATION_ERROR": 400, "FORBIDDEN": 403,
            "UNAUTHORIZED": 401, "CANDIDATE_SET_CHANGED": 409,
            "REPLACEMENT_REQUIRED": 409, "CYCLE_DETECTED": 400,
            "INVALID_PARENT": 400}.get(code, 400)


def serve(db_path, scope, principal="web:local", port=0, token=None):
    """Start the loopback server. port=0 picks a free local port.
    Returns (server, token). Never binds 0.0.0.0."""
    token = token or make_token()
    try:
        server = TurnkeepHTTPServer(("127.0.0.1", port), Handler,
                                    Path(db_path), scope, principal, token)
    except OSError as e:
        if port != 0:
            # Port occupied: pick a free local port and say so (backlog T027
            # failure handling); never fall back to 0.0.0.0.
            import sys as _sys
            print(f"port {port} busy ({e}); picking a free local port", file=_sys.stderr)
            server = TurnkeepHTTPServer(("127.0.0.1", 0), Handler,
                                        Path(db_path), scope, principal, token)
        else:
            raise
    return server, token
