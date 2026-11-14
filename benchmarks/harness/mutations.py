"""Deliberately broken engines (T041). Test doubles ONLY — they exist to
prove the replay distinguishes a working implementation from a broken one.
Never used as product evidence; mutation runs are labelled as such.

- no-taint: retract/expire changes state but never taints descendants, so a
  child that depends on a retracted parent stays usable.
- silent-overwrite: correct() ignores the expected revision and overwrites
  whatever the current value is.
"""
from turnkeep.storage import row_or_404, set_state, transaction, ensure_scope
from turnkeep.storage import publish_event, dump_record, _now, _json
from turnkeep.errors import TurnkeepError


class _Mutated:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    @property
    def db(self):
        return self._inner.db

    @db.setter
    def db(self, value):
        self._inner.db = value


class NoTaint(_Mutated):
    def retract(self, key, reason="correction", scope=None):
        from turnkeep.retrieval import search_by_key
        db, scope = self._inner.db, scope or self._inner.scope
        rec = search_by_key(db, scope, key, include_history=True)[-1]
        with transaction(db, write=True):
            ensure_scope(db, scope)
            row = row_or_404(db, scope, rec["id"])
            # Broken: change state WITHOUT taint_descendants.
            set_state(db, scope, rec["id"], "retracted", {})
            publish_event(db, scope, "retract", "local", {rec["id"]: None},
                          metadata={"key": key, "reason": reason})
        return {"ok": True, "id": rec["id"]}


class SilentOverwrite(_Mutated):
    def correct(self, key, value, expected_revision=None, scope=None, which="last"):
        from turnkeep.retrieval import search_by_key
        db, scope = self._inner.db, scope or self._inner.scope
        recs = search_by_key(db, scope, key, include_history=True)
        rec = recs[0] if which == "first" else recs[-1]
        # Broken: ignore the expected revision entirely.
        with transaction(db, write=True):
            ensure_scope(db, scope)
            row = row_or_404(db, scope, rec["id"])
            db.execute("UPDATE memories SET value_json=?, revision=revision+1, updated_at=? "
                       "WHERE scope_id=? AND id=?",
                       (_json(value), _now(), scope, rec["id"]))
        return {"ok": True, "id": rec["id"], "revision": row["revision"] + 1}


def apply_mutation(name, engine):
    if name == "no-taint":
        return NoTaint(engine)
    if name == "silent-overwrite":
        return SilentOverwrite(engine)
    raise ValueError(f"unknown mutation {name!r}")
