"""P1-R2-04: the Idempotency-Key HEADER is read and enforced on the six
POST ops (remember/begin/finish/correct/retract/resolve_conflict), and
replays through the header dedupe writes. Real HTTP server, ephemeral port."""
import http.client
import json
import threading
import uuid

import pytest

from turnkeep.web.app import serve


@pytest.fixture()
def server(tmp_path):
    srv, token = serve(tmp_path / "h.sqlite3", "web", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()


def req(server, method, path, body=None, headers=None):
    srv, token = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    r = conn.getresponse()
    data = json.loads(r.read().decode("utf-8"))
    conn.close()
    return r.status, data


def _key():
    return uuid.uuid4().hex


def test_missing_idempotency_header_rejected_on_all_six_ops(server):
    # No header at all -> 400 INVALID_INPUT on every idempotent POST,
    # and nothing is stored.
    for path, body in [
        ("/api/v1/remember", {"key": "k", "value": "v"}),
        ("/api/v1/begin", {"task": "work"}),
        ("/api/v1/finish", {"run_id": "r", "key": "k", "value": "v"}),
        ("/api/v1/correct", {"record_id": "r", "expected_revision": 1, "new_value": "v"}),
        ("/api/v1/retract", {"record_id": "r", "expected_revision": 1, "reason": "x"}),
        ("/api/v1/resolve_conflict", {"key": "k", "value": "v", "candidates": []}),
    ]:
        status, err = req(server, "POST", path, body)
        assert status == 400, (path, err)
        assert err["error"]["code"] == "INVALID_INPUT", (path, err)
    # ...while a non-idempotent POST (resume) needs no header to reach
    # its own error (unknown run -> 404, not a 400 about the header).
    status, err = req(server, "POST", "/api/v1/resume", {"run_id": "nope"})
    assert status == 404 and err["error"]["code"] == "NOT_FOUND"


def test_short_or_long_idempotency_header_rejected(server):
    for bad in ("short", "x" * 129):
        status, err = req(server, "POST", "/api/v1/remember",
                          {"key": "k", "value": "v"},
                          headers={"Idempotency-Key": bad})
        assert status == 400 and err["error"]["code"] == "INVALID_INPUT", bad
    # Nothing was stored: the scope was never even registered, so recall
    # 404s instead of returning a record.
    status, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert status == 404 and hits["error"]["code"] == "NOT_FOUND"


def test_remember_replay_via_header_one_write(server):
    key = _key()
    h = {"Idempotency-Key": key}
    status, r1 = req(server, "POST", "/api/v1/remember",
                     {"key": "k", "value": "v"}, headers=h)
    assert status == 200, r1
    status, r2 = req(server, "POST", "/api/v1/remember",
                     {"key": "k", "value": "v"}, headers=h)
    assert status == 200 and r2 == r1
    status, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert len(hits["records"]) == 1


def test_remember_same_header_different_request_mismatch(server):
    key = _key()
    h = {"Idempotency-Key": key}
    status, _ = req(server, "POST", "/api/v1/remember",
                    {"key": "k", "value": "v"}, headers=h)
    assert status == 200
    status, err = req(server, "POST", "/api/v1/remember",
                      {"key": "k", "value": "different"}, headers=h)
    assert status == 409 and err["error"]["code"] == "IDEMPOTENCY_MISMATCH"
    status, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert len(hits["records"]) == 1 and hits["records"][0]["value"] == "v"


def test_begin_replay_via_header_one_run(server):
    key = _key()
    h = {"Idempotency-Key": key}
    _, b1 = req(server, "POST", "/api/v1/begin", {"task": "work"}, headers=h)
    _, b2 = req(server, "POST", "/api/v1/begin", {"task": "work"}, headers=h)
    assert b1 == b2
    status, runs = req(server, "GET", "/api/v1/runs")
    assert len(runs["runs"]) == 1


def test_header_wins_over_body_key(server):
    # First call: header H + body key B. Second call: same header H but
    # a DIFFERENT body key B2. If the body governed, this would be a
    # mismatch / new write; the header governs, so it replays.
    h = {"Idempotency-Key": _key()}
    _, r1 = req(server, "POST", "/api/v1/remember",
                {"key": "k", "value": "v", "idempotency_key": "body-key-aaaa"},
                headers=h)
    status, r2 = req(server, "POST", "/api/v1/remember",
                     {"key": "k", "value": "v", "idempotency_key": "body-key-bbbb"},
                     headers=h)
    assert status == 200 and r2 == r1
    status, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert len(hits["records"]) == 1


def test_openapi_documents_header_on_six_post_routes(server):
    status, spec = req(server, "GET", "/api/v1/openapi.json")
    assert status == 200
    for path in ("/api/v1/remember", "/api/v1/begin", "/api/v1/finish",
                 "/api/v1/correct", "/api/v1/retract", "/api/v1/resolve_conflict"):
        params = spec["paths"][path]["post"].get("parameters", [])
        assert any(p["name"] == "Idempotency-Key" and p["required"] for p in params), path
