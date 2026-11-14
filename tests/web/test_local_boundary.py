"""T027 local-boundary tests: token, Origin/Host, body cap, traversal,
non-loopback bind refusal."""
import http.client
import json
import threading

import pytest

from turnkeep.web.app import serve, TurnkeepHTTPServer, Handler


@pytest.fixture()
def server(tmp_path):
    srv, token = serve(tmp_path / "b.sqlite3", "web", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()


def raw(server, method, path, headers=None, body=None):
    srv, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    conn.request(method, path, body, headers or {})
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, data


def test_missing_token_unauthorized(server):
    status, _ = raw(server, "GET", "/api/v1/health")
    assert status == 401


def test_wrong_token_unauthorized(server):
    status, _ = raw(server, "GET", "/api/v1/health",
                    {"Authorization": "Bearer wrong-token"})
    assert status == 401


def test_cross_origin_write_forbidden(server):
    srv, token = server
    status, _ = raw(server, "POST", "/api/v1/remember",
                    {"Authorization": f"Bearer {token}",
                     "Origin": "https://evil.example",
                     "Content-Type": "application/json"},
                    json.dumps({"key": "k", "value": "v"}))
    assert status == 403


def test_spoofed_host_forbidden(server):
    srv, token = server
    status, _ = raw(server, "GET", "/api/v1/health",
                    {"Authorization": f"Bearer {token}", "Host": "evil.example"})
    assert status == 403


def test_body_too_large_rejected(server):
    srv, token = server
    big = json.dumps({"key": "k", "value": "x" * 1_200_000})
    status, data = raw(server, "POST", "/api/v1/remember",
                       {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(big))}, big)
    assert status == 400
    assert json.loads(data)["error"]["code"] == "INVALID_INPUT"


def test_path_traversal_not_a_file_read(server):
    for p in ("/../etc/passwd", "/api/v1/../../etc/passwd", "/static/../x"):
        status, data = raw(server, "GET", p,
                           {"Authorization": f"Bearer {server[1]}"})
        assert status in (401, 404), (p, status)
        assert "root:" not in data


def test_non_loopback_bind_refused(tmp_path):
    with pytest.raises(ValueError):
        TurnkeepHTTPServer(("0.0.0.0", 0), Handler, tmp_path / "x.sqlite3",
                           "web", "web:local", "tok")


def test_cross_site_get_cannot_write(server):
    # A plain <img>/fetch GET from another origin has no token and no
    # same-origin Origin; it must not reach the store.
    status, _ = raw(server, "GET", "/api/v1/recall?query=x",
                    {"Origin": "https://evil.example"})
    assert status in (401, 403)


def test_loopback_preflight_and_cross_port_origin_allowed(server):
    srv, token = server
    # Vite dev (5173) talking to the API on another loopback port: allowed.
    status, _ = raw(server, "OPTIONS", "/api/v1/recall",
                    {"Origin": "http://127.0.0.1:5173",
                     "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Headers": "authorization"})
    assert status == 204
    status, data = raw(server, "GET", "/api/v1/health",
                       {"Authorization": f"Bearer {token}",
                        "Origin": "http://127.0.0.1:5173"})
    assert status == 200
    assert json.loads(data)["status"] == "ok"
