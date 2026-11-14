"""Expiry (T038). Explicit expires_at only — never probabilistic time decay.

- set_expiry(): CAS-set valid_until on one record.
- sweep_expired(): records whose valid_until has passed leave the active
  view (state -> 'expired') and their descendants are tainted through the
  same stored taint propagation retract uses, so no stale child stays usable.
An expired record is still in history (state 'expired'), never silently
deleted; erasure is a separate, explicit operation (erasure.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .errors import TurnkeepError
from .erasure import _check_reason_against_needles, _needles_for_payload
from .storage import (check_revision, dump_record, ensure_scope, publish_event,
                      row_or_404, set_state, taint_descendants, transaction,
                      _id, _json, _now, _text)


def _check_valid_until(valid_until):
    """Validate valid_until as an ISO-8601 timestamp with a timezone
    (P0-R6-01).

    valid_until is caller-supplied text copied verbatim into the
    expire_set event's metadata AND stored in memories.valid_until
    (exported in records.jsonl, never cleared by erase). The old
    verbatim-needle check could not see a reversible encoding of the
    payload (Base64 / percent / reversed / hex / rot13 contain no
    verbatim needle), so an encoding survived erasure in the
    tombstoned row and in the export. A timestamp cannot carry one:
    the value must parse with datetime.fromisoformat AND be
    timezone-aware (tzinfo not None) — the format _now() produces
    (datetime.now(timezone.utc).isoformat(timespec="microseconds")).
    Anything else is INVALID_INPUT, before anything is written.
    The same check now guards created_at / updated_at at import
    (portability._plan_import, P0-R7-01) and the sweep parses
    timestamps timezone-aware instead of comparing them as strings
    (P1-R7-02). import_package applies the same check
    (see portability._plan_import).
    """
    if not isinstance(valid_until, str):
        # P2-R7-03: datetime.fromisoformat only raises ValueError, not
        # TypeError — a non-str (a JSON number from import) reached it
        # unguarded and raised a raw TypeError instead of INVALID_INPUT.
        raise TurnkeepError(
            "INVALID_INPUT",
            "valid_until must be an ISO-8601 timestamp string with a timezone "
            "(e.g. '2026-01-01T00:00:00+00:00'): a non-string cannot "
            "be an expiry, and free text is never stored in valid_until")
    try:
        parsed = datetime.fromisoformat(valid_until)
    except ValueError:
        raise TurnkeepError(
            "INVALID_INPUT",
            "valid_until must be an ISO-8601 timestamp with a timezone "
            "(e.g. '2026-01-01T00:00:00+00:00'): a non-timestamp cannot "
            "be an expiry, and free text is never stored in valid_until")
    if parsed.tzinfo is None:
        raise TurnkeepError(
            "INVALID_INPUT",
            "valid_until must be an ISO-8601 timestamp with a timezone "
            "(e.g. '2026-01-01T00:00:00+00:00'): a naive timestamp has "
            "no defined meaning against UTC expiries")


def _parse_expiry(value):
    """Parse a stored valid_until to an aware UTC datetime, or None
    (P1-R7-02).

    Naive legacy timestamps are read as UTC (documented rule — the
    product has only ever written UTC, so a naive stored value is a
    legacy UTC value, not a local time). An unparseable value returns
    None: it has no defined expiry and is never swept on a guess (the
    caller reports it as skipped)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_now(now):
    """Parse the sweep's `now` (an ISO-8601 str, or a datetime) to an
    aware UTC datetime (P1-R7-02). A value that is neither raises
    INVALID_INPUT instead of silently comparing the wrong thing."""
    if isinstance(now, datetime):
        parsed = now
    elif isinstance(now, str):
        try:
            parsed = datetime.fromisoformat(now)
        except ValueError:
            raise TurnkeepError(
                "INVALID_INPUT",
                "sweep now must be an ISO-8601 timestamp with a timezone")
    else:
        raise TurnkeepError(
            "INVALID_INPUT",
            "sweep now must be an ISO-8601 timestamp with a timezone")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def set_expiry(db, scope_id, record_id, valid_until, expected_revision, *, actor_binding="local"):
    scope_id = _text(scope_id, "scope")
    valid_until = _text(valid_until, "valid_until")
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        row = row_or_404(db, scope_id, record_id)
        check_revision(row, expected_revision)
        if row["state"] in ("erased", "retracted"):
            raise TurnkeepError("INVALID_INPUT", f"cannot set expiry on a {row['state']} record")
        # P0-R6-01: valid_until is caller-supplied text copied verbatim
        # into the expire_set event's metadata and stored in
        # memories.valid_until (exported, never cleared by erase). The
        # ISO-8601 check runs FIRST — a non-timestamp is INVALID_INPUT,
        # so no encoding of the payload can be stored here. The needle
        # check survives as a second layer for the corner where the
        # payload itself IS a timestamp (valid_until equal to the
        # record's own timestamp value).
        _check_valid_until(valid_until)
        _check_reason_against_needles(valid_until, _needles_for_payload(row["value_json"]))
        # P1-R2-03b: ONE event id generated up front (as sweep_expired()
        # already does): the revision bump below makes expected_revision a
        # real CAS guard for valid_until, and the record cites the event
        # that set its expiry instead of keeping the remember event.
        ev = _id()
        db.execute("UPDATE memories SET valid_until=?, updated_at=?, revision=revision+1, cause_event_id=? WHERE scope_id=? AND id=?",
                   (valid_until, _now(), ev, scope_id, record_id))
        publish_event(db, scope_id, "expire_set", actor_binding, {record_id: None},
                      metadata={"key": row["key"], "valid_until": valid_until},
                      event_id=ev)
        return dump_record(db, row_or_404(db, scope_id, record_id))


def sweep_expired(db, scope_id, *, now=None, actor_binding="local"):
    """Expire every active/conflicted record whose valid_until has passed.
    Returns {expired: [ids], tainted: [ids], skipped_unparseable: [ids]}.
    Idempotent: a second sweep finds nothing to do.

    Timezone-aware (P1-R7-02): valid_until and now are parsed with
    datetime.fromisoformat and compared as UTC datetimes — never as
    ISO strings, which compare the offset text instead of the instant
    (a +02:00 expiry an hour past UTC was kept while a -02:00 expiry
    an hour in the future was swept: exactly reversed). A naive legacy
    valid_until is read as UTC (see _parse_expiry); an unparseable one
    is skipped and reported, never swept on a guess."""
    scope_id = _text(scope_id, "scope")
    now = now or _now()
    now_dt = _parse_now(now)
    expired, tainted, skipped = [], [], []
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        rows = db.execute(
            "SELECT * FROM memories WHERE scope_id=? AND valid_until IS NOT NULL "
            "AND state IN ('active','conflicted') ORDER BY key",
            (scope_id,)).fetchall()
        # P1-g: ONE event id for the whole sweep; every expiry + taint
        # cites it. (set_expiry generates its own event id the same way,
        # so its revision bump + cause_event_id cite its expire_set event.)
        ev = _id()
        for row in rows:
            expiry = _parse_expiry(row["valid_until"])
            if expiry is None:
                skipped.append(row["id"])
                continue
            if expiry > now_dt:
                continue
            changes = {}
            set_state(db, scope_id, row["id"], "expired", changes, cause_event_id=ev)
            taint_descendants(db, scope_id, [row["id"]], changes, cause_event_id=ev)
            expired.append(row["id"])
            tainted.extend(c for c in changes if c != row["id"])
        if expired:
            publish_event(db, scope_id, "expire_sweep", actor_binding,
                          {rid: None for rid in expired},
                          metadata={"expired": len(expired), "tainted": len(set(tainted))},
                          event_id=ev)
    return {"scope": scope_id, "now": now, "expired": expired,
            "tainted": sorted(set(tainted)), "skipped_unparseable": skipped}
