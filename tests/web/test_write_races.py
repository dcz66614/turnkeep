"""T032 write-race tests over the real HTTP API: concurrent corrects must
produce exactly one winner + 409s (never a silent lost update); a resolve
with a changed candidate set is rejected whole; a browser body cannot
forge actor/scope.
"""
import http.client
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from turnkeep.web.app import serve


@pytest.fixture()
def server(tmp_path):
    srv, token = serve(tmp_path / "w.sqlite3", "web", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()


def req(server, method, path, body=None, headers=None):
    # P1-R2-04: POSTs to idempotent routes need an Idempotency-Key header.
    # Default is a fresh unique key per call, so distinct concurrent
    # requests don't collide on one key; callers that need a shared key
    # (the finish race) pass it explicitly via headers.
    srv, token = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=15)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if method == "POST":
        h["Idempotency-Key"] = uuid.uuid4().hex
    if headers:
        h.update(headers)
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    r = conn.getresponse()
    data = json.loads(r.read().decode())
    conn.close()
    return r.status, data


def test_concurrent_corrects_one_winner(server):
    _, rec = req(server, "POST", "/api/v1/remember", {"key": "k", "value": "v0"})
    rid = rec["id"]

    def correct(i):
        return req(server, "POST", "/api/v1/correct",
                   {"record_id": rid, "expected_revision": 1, "new_value": f"v{i}"})

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(correct, range(4)))
    codes = [s for s, _ in results]
    assert codes.count(200) == 1, codes
    assert codes.count(409) == 3, codes
    _, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert len(hits["records"]) == 1  # no duplicate, no silent loss


def test_resolve_with_changed_candidate_set_rejected_whole(server):
    _, a = req(server, "POST", "/api/v1/remember", {"key": "k", "value": "a"})
    _, b = req(server, "POST", "/api/v1/remember", {"key": "k", "value": "b"})
    _, conf = req(server, "GET", "/api/v1/conflicts?key=k")
    assert len(conf["conflicts"]) == 2
    # A third candidate appears after the client fetched the set.
    req(server, "POST", "/api/v1/remember", {"key": "k", "value": "c"})
    stale = [{"id": r["id"], "revision": r["revision"]} for r in conf["conflicts"]]
    status, err = req(server, "POST", "/api/v1/resolve_conflict",
                      {"key": "k", "value": "a", "candidates": stale,
                       "sources": [{"id": "doc:9", "type": "imported_text"}]})
    assert status == 409, err
    assert err["error"]["code"] == "CANDIDATE_SET_CHANGED", err
    _, conf2 = req(server, "GET", "/api/v1/conflicts?key=k")
    assert len(conf2["conflicts"]) == 3  # nothing was resolved


def test_body_cannot_forge_actor_or_scope(server):
    # A body that claims another scope (without forging the actor) still
    # lands in the server-bound scope — the scope field is inert.
    status, rec = req(server, "POST", "/api/v1/remember",
                      {"key": "k", "value": "v", "scope": "other-scope"})
    assert status == 200, rec
    _, hits = req(server, "GET", "/api/v1/recall?query=k")
    assert len(hits["records"]) == 1
    assert hits["records"][0]["scope"] == "web"
    # A body that claims another actor is rejected outright (P1-d: the
    # transport policy is now enforced at the HTTP layer; the old test
    # asserted the pre-wiring behaviour where the fields were ignored).
    # (Recall on a never-registered scope 404s, so the scope is registered
    # by the conforming write above before asserting nothing was stored.)
    status, err = req(server, "POST", "/api/v1/remember",
                      {"key": "k2", "value": "v2", "scope": "other-scope",
                       "actor_id": "admin", "tenant_id": "t1"})
    assert status == 403, err
    assert err["error"]["code"] == "FORBIDDEN", err
    _, hits = req(server, "GET", "/api/v1/recall?query=k2")
    assert len(hits["records"]) == 0  # nothing stored


def test_concurrent_finish_same_idempotency_key_one_receipt(server):
    _, rec = req(server, "POST", "/api/v1/remember", {"key": "k", "value": "v"})
    _, run = req(server, "POST", "/api/v1/begin", {"task": "work"})

    def finish(_):
        # Header is the spec'd key source and wins over the body key
        # ("race-1" here is a stale short body key, deliberately kept
        # to prove the header governs: all four share header "race-key-1").
        return req(server, "POST", "/api/v1/finish",
                   {"run_id": run["run_id"], "key": "out", "value": "x",
                    "idempotency_key": "race-1"},
                   headers={"Idempotency-Key": "race-key-1"})

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(finish, range(4)))
    bodies = [b for _, b in results]
    assert all(b == bodies[0] for b in bodies), bodies
    assert bodies[0].get("record_id"), bodies[0]
