"""Dependency DAG + transitive taint (T012). AND semantics: if ANY required
parent becomes unavailable, the child is tainted — never silently OR.

- validate_parents: parent must exist, be in the same scope, be usable
  (active incl. all its ancestors); self-loops and cycles rejected
  (CYCLE_DETECTED); missing/invalid/conflicted parents rejected
  (INVALID_PARENT). Re-exports storage.validate_parents for one call site.
- taint_descendants: BFS over dependencies with a visited set; every
  indirect descendant of a newly-unavailable record becomes tainted,
  with the change recorded in the same transaction's event.
- impact(db, scope_id, record_id): readable dependency paths from the
  record to every tainted/active descendant (for J03 / workbench impact view).
- A query must never return a descendant as active while its taint update
  is still unmaterialised: validity is always re-checked live via
  storage.usable() at read time (see retrieval.py).
"""
from __future__ import annotations
from collections import deque
from .storage import (row_or_404, dump_record, record_parents, usable,
                      validate_parents, taint_descendants, _text)


def impact(db, scope_id, record_id):
    scope_id = _text(scope_id, "scope")
    row_or_404(db, scope_id, record_id)
    paths = []
    queue = deque([(record_id, [record_id])])
    seen = {record_id}
    while queue:
        current, path = queue.popleft()
        for row in db.execute(
                "SELECT child_id FROM dependencies WHERE scope_id=? AND parent_id=? ORDER BY child_id",
                (scope_id, current)):
            child = row[0]
            if child in seen:
                continue
            seen.add(child)
            child_path = path + [child]
            child_row = row_or_404(db, scope_id, child)
            paths.append({"record_id": child, "key": child_row["key"],
                          "state": child_row["state"], "dependency_path": child_path})
            queue.append((child, child_path))
    return paths


def check_usable(db, scope_id, record_id):
    """Live validity check (fails closed on unmaterialised taint)."""
    return usable(db, scope_id, record_id)
