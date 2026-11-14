"""Idempotency (T009). Request digest + scoped idempotency keys.

- request_digest(operation, canonical_request): sha256 of the canonical JSON
  of {operation, scope, principal, request}. The digest spec is versioned:
  DIGEST_VERSION=2; a future digest change must bump the version and keep a
  compatibility path — never silently ignore payload differences.
  v2 (current): the finish digest covers run_id + result + sorted parent_ids.
  v1 (legacy): run_id + result only. Replays accept EITHER digest for runs
  accepted before the v2 change (see check_any / legacy_request_digest);
  runs accepted under v2 only match their exact v2 digest, so a replay with
  different parents is still IDEMPOTENCY_MISMATCH.
- check(db, scope_id, principal, operation, key, digest): returns the stored
  response dict if (key, digest) match; raises IDEMPOTENCY_MISMATCH if the
  key exists with a different digest; returns None if unseen.
- check_any(db, ..., key, digests): same, but accepts the stored response
  when the stored digest equals ANY of the given digests (v2 or legacy v1).
- store(db, ...): inserts the response in the SAME transaction as the
  business write (caller holds the tx). A failed request stores nothing, so
  a retry can never return a fabricated success receipt.
- Keys are scoped to (scope, principal, operation): the same key in another
  scope/principal/operation never returns another scope's result.
- Erasure neutralizes digests (P0-R10-01): when a record is erased, the
  erasure flow replaces the request_digest of every idempotency row whose
  stored response belongs to that record with NEUTRALIZED_DIGEST (and
  removes the digest from a finish receipt / runs row the same way).
  A replay then raises IDEMPOTENCY_MISMATCH instead of returning the
  stored response — the stored response belonged to an erased record
  and must not be re-issued. The marker is NOT a hash of anything: a
  plain SHA-256 digest of a request containing a low-entropy value
  (e.g. a 4-digit PIN) is enumerable offline by re-computing it over
  0000-9999, and re-keying it with HMAC would NOT fix that — the
  per-scope HMAC key lives in the same DB (scope_secrets), so a DB
  holder enumerates an HMAC just as easily (verified: R10
  verification repro A1b.2). Neutralization at erase time is the only
  fix. The digest COMPUTATION is unchanged (no version bump, no
  dual-accept path needed): only stored digests of erased records are
  neutralized; rows belonging to live records are untouched, so
  pre-change rows keep replaying exactly as before.
"""
from __future__ import annotations
import json
from .errors import TurnkeepError
from .storage import _digest, _json

DIGEST_VERSION = 2
LEGACY_DIGEST_VERSION = 1

# Marker a stored request_digest is replaced with when its record is
# erased (P0-R10-01). Deliberately NOT a hex digest: real digests are
# 64-char hex, so this value can never equal a computed digest and a
# replay against a neutralized row always raises IDEMPOTENCY_MISMATCH.
# It carries no payload material (it is the same fixed string for
# every neutralized row), so it is safe to store and to compare
# against. See the module docstring for why HMAC is not used here.
NEUTRALIZED_DIGEST = "erased"


def validate_idempotency_key(key):
    """Spec validation for a supplied Idempotency-Key (PUBLIC_API.json:
    8-128 chars). Used by the HTTP layer, which requires the header on
    the six POST ops. The core functions accept any non-empty key (the
    finish precedent — existing callers use short keys such as "k-1");
    length is a transport/spec rule, not a core rule, so it is enforced
    here at the boundary, not inside check()/store()."""
    if key and not (8 <= len(key) <= 128):
        raise TurnkeepError("INVALID_INPUT",
                            "Idempotency-Key must be 8-128 characters")
    return key


def request_digest(operation, scope_id, principal_binding, request, *, version=DIGEST_VERSION):
    payload = {"v": version, "op": operation, "scope": scope_id,
               "principal": principal_binding, "request": request}
    return _digest(payload)


def legacy_request_digest(operation, scope_id, principal_binding, request):
    """The v1 digest of the same request (DIGEST_VERSION=1 payload).

    v1 digests carry no parent_ids, so they cannot distinguish replays with
    different parents; they exist only so runs accepted before the v2 change
    still replay to their original receipt.
    """
    return request_digest(operation, scope_id, principal_binding, request,
                          version=LEGACY_DIGEST_VERSION)


def check(db, scope_id, principal_binding, operation, key, digest):
    row = db.execute(
        "SELECT request_digest, response_json FROM idempotency "
        "WHERE scope_id=? AND principal_binding=? AND operation=? AND idempotency_key=?",
        (scope_id, principal_binding, operation, key)).fetchone()
    if row is None:
        return None
    if row[0] != digest:
        raise TurnkeepError("IDEMPOTENCY_MISMATCH",
                            "Same idempotency key with a different request")
    return json.loads(row[1])


def check_any(db, scope_id, principal_binding, operation, key, digests):
    """Like check(), but the stored response is returned when the stored
    digest equals ANY of the given digests (current v2 or legacy v1).

    Raises IDEMPOTENCY_MISMATCH only when the key exists and NO given digest
    matches the stored one; returns None when the key is unseen.
    """
    row = db.execute(
        "SELECT request_digest, response_json FROM idempotency "
        "WHERE scope_id=? AND principal_binding=? AND operation=? AND idempotency_key=?",
        (scope_id, principal_binding, operation, key)).fetchone()
    if row is None:
        return None
    if row[0] not in set(digests):
        raise TurnkeepError("IDEMPOTENCY_MISMATCH",
                            "Same idempotency key with a different request")
    return json.loads(row[1])


def store(db, scope_id, principal_binding, operation, key, digest, response):
    from .storage import _now
    db.execute(
        "INSERT INTO idempotency(scope_id, principal_binding, operation, idempotency_key, "
        "request_digest, response_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (scope_id, principal_binding, operation, key, digest, _json(response), _now()))
