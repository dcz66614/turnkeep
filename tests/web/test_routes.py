"""T027 route tests: real HTTP server on an ephemeral 127.0.0.1 port.
Same core, same error codes as CLI/MCP; GET recall never mutates."""
import http.client
import json
import threading
import uuid
from pathlib import Path

import pytest

from turnkeep.web.app import serve


def _idem_key():
    # P1-R2-04: every POST to an idempotent route needs a unique
    # Idempotency-Key header (8-128 chars); a fresh key per call keeps
    # these tests' distinct requests from colliding on one key.
    return uuid.uuid4().hex  # 32 chars


@pytest.fixture()
def server(tmp_path):
    dbpath = tmp_path / "web.sqlite3"
    srv, token = serve(dbpath, "web", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()


def req(server, token, method, path, body=None, headers=None):
    srv, tok = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if method == "POST":
        h["Idempotency-Key"] = _idem_key()
    if headers:
        h.update(headers)
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    r = conn.getresponse()
    data = json.loads(r.read().decode("utf-8"))
    conn.close()
    return r.status, data


def test_health_and_remember_recall(server):
    status, out = req(server, None, "GET", "/api/v1/health")
    assert status == 200 and out["status"] == "ok" and out["scope"] == "web"
    status, rec = req(server, None, "POST", "/api/v1/remember",
                     {"key": "k", "value": "v"})
    assert status == 200 and rec["state"] == "active"
    status, hits = req(server, None, "GET", "/api/v1/recall?query=k")
    assert status == 200 and len(hits["records"]) == 1


def test_get_recall_does_not_mutate(server):
    _, before = req(server, None, "GET", "/api/v1/health")
    req(server, None, "GET", "/api/v1/recall?query=x")
    _, after = req(server, None, "GET", "/api/v1/health")
    assert before["epoch"] == after["epoch"]


def test_begin_finish_roundtrip_and_stale(server):
    _, rec = req(server, None, "POST", "/api/v1/remember",
                 {"key": "req", "value": "3.11"})
    _, run = req(server, None, "POST", "/api/v1/begin", {"task": "work"})
    _, _ = req(server, None, "POST", "/api/v1/correct",
               {"record_id": rec["id"], "expected_revision": 1, "new_value": "3.12"})
    status, err = req(server, None, "POST", "/api/v1/finish",
                      {"run_id": run["run_id"], "key": "out", "value": "3.11"})
    assert status == 409 and err["error"]["code"] == "STALE_SNAPSHOT"
    assert "req" in err["error"]["details"]["changed_keys"]


def test_error_shape_matches_cli_mcp(server):
    status, err = req(server, None, "POST", "/api/v1/remember", {"key": "only-key"})
    assert status == 400 and err["error"]["code"] == "INVALID_INPUT"
    status, err = req(server, None, "GET", "/api/v1/nope")
    assert status == 404 and err["error"]["code"] == "NOT_FOUND"
    status, out = req(server, None, "GET", "/api/v1/openapi.json")
    assert status == 200 and out["openapi"].startswith("3.0")


def test_no_db_path_leaked(server):
    _, out = req(server, None, "GET", "/api/v1/health")
    assert "sqlite" not in json.dumps(out) and "/tmp" not in json.dumps(out)
    _, err = req(server, None, "POST", "/api/v1/finish",
                 {"run_id": "nope", "key": "k", "value": "v"})
    assert "/tmp" not in json.dumps(err) and "sqlite3" not in json.dumps(err)


def test_scopes_lists_registered_scopes(server):
    status, out = req(server, None, "GET", "/api/v1/scopes")
    assert status == 200 and out["scopes"] == []


def test_remember_path_like_source_rejected(server):
    # P1-d: a path-like source id in body["sources"] must be rejected (400)
    # before anything is stored, on remember and on correct.
    status, err = req(server, None, "POST", "/api/v1/remember",
                      {"key": "k", "value": "v",
                       "sources": [{"id": "/home/alice/notes.md", "type": "user_statement"}]})
    assert status == 400 and err["error"]["code"] == "INVALID_INPUT"
    status, rec = req(server, None, "POST", "/api/v1/remember",
                      {"key": "k", "value": "v"})
    assert status == 200
    status, err = req(server, None, "POST", "/api/v1/correct",
                      {"record_id": rec["id"], "expected_revision": 1, "new_value": "v2",
                       "sources": [{"id": "C:\\Users\\alice\\notes.md", "type": "user_statement"}]})
    assert status == 400 and err["error"]["code"] == "INVALID_INPUT"
    status, hits = req(server, None, "GET", "/api/v1/recall?query=v")
    assert status == 200 and len(hits["records"]) == 1
    assert hits["records"][0]["value"] == "v"


def test_remember_actor_override_rejected(server):
    # P1-d: a body trying to override the bound actor is FORBIDDEN (403)
    # and nothing is stored.
    status, err = req(server, None, "POST", "/api/v1/remember",
                      {"key": "k", "value": "v", "actor_id": "mallory"})
    assert status == 403 and err["error"]["code"] == "FORBIDDEN"
    # A later legitimate remember works, and recall shows the rejected
    # value was never stored (recall on a never-registered scope is 404,
    # so store one good record first to register the scope).
    status, _ = req(server, None, "POST", "/api/v1/remember",
                    {"key": "ok", "value": "good"})
    assert status == 200
    status, hits = req(server, None, "GET", "/api/v1/recall?query=v")
    assert status == 200 and hits["records"] == []


def test_runs_and_impact(server):
    _, rec = req(server, None, "POST", "/api/v1/remember",
                 {"key": "req", "value": "3.11"})
    _, child = req(server, None, "POST", "/api/v1/remember",
                   {"key": "out", "value": "3.11", "parents": [rec["id"]]})
    _, run = req(server, None, "POST", "/api/v1/begin", {"task": "work"})
    status, runs = req(server, None, "GET", "/api/v1/runs")
    assert status == 200 and len(runs["runs"]) == 1
    assert runs["runs"][0]["status"] == "open"
    status, imp = req(server, None, "GET", f"/api/v1/impact?record_id={rec['id']}")
    assert status == 200 and len(imp["paths"]) == 1
    assert imp["paths"][0]["record_id"] == child["id"]
    assert imp["paths"][0]["dependency_path"] == [rec["id"], child["id"]]
