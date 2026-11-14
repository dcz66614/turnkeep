"""Events (T009). One event row per change-set, written in the SAME
transaction as the business change (via storage.publish_event).

- write_event() is a thin wrapper: it records the event type + affected ids
  and returns the new scope epoch. Metadata carries refs only — NEVER record
  bodies (values, full text) in events; point to controlled payload refs.
- list_events(db, scope_id, *, after_epoch=None): audit read, oldest first.
"""
from __future__ import annotations
from .storage import publish_event, _json


def write_event(db, scope_id, event_type, actor_binding, changes, *, metadata=None):
    return publish_event(db, scope_id, event_type, actor_binding, changes,
                         metadata=metadata or {})


def list_events(db, scope_id, *, after_epoch=None, limit=200):
    q = ("SELECT id, scope_id, scope_epoch, event_type, actor_binding, "
         "affected_ids_json, metadata_json, created_at FROM events WHERE scope_id=?")
    args = [scope_id]
    if after_epoch is not None:
        q += " AND scope_epoch > ?"; args.append(after_epoch)
    q += " ORDER BY scope_epoch ASC, created_at ASC LIMIT ?"; args.append(limit)
    out = []
    for r in db.execute(q, args):
        d = dict(r)
        d["affected_ids"] = __import__("json").loads(d.pop("affected_ids_json"))
        d["metadata"] = __import__("json").loads(d.pop("metadata_json"))
        out.append(d)
    return out
