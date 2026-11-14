"""Turnkeep Python SDK (T020). Small, stable, embedded-local only.

- TurnkeepClient(db_path, scope_id, *, actor="local", principal_binding="cli:local"):
  context manager; one SQLite connection; explicit DB + scope.
  Remote mode is NOT promised.
- Methods mirror the core exactly (remember/correct/retract/resolve/
  begin/finish/resume/rebase/recall/brief) — the SDK does NOT copy the
  state machine or retrieval rules; it calls the same modules as the CLI.
- Errors become typed exceptions carrying record IDs, changed keys and a
  retryable classification (TurnkeepError is re-raised unchanged — it
  already carries code + details; SDKError wraps only unexpected errors).
- close() releases the connection; no long read transaction is ever held
  (begin() ends its read tx before returning).
"""
from __future__ import annotations
from pathlib import Path

from . import memory as _mem, corrections as _corr, runs as _runs
from . import commits as _com, recovery as _rec, retrieval as _ret, briefs as _br
from .context import bind_context
from .errors import TurnkeepError
from .scopes import register_scope
from .storage import open_db, scope_epoch


class SDKError(Exception):
    """Unexpected SDK-level failure (not a domain TurnkeepError)."""


class TurnkeepClient:
    def __init__(self, db_path, scope_id, *, actor="local", principal_binding="cli:local",
                 display_name=None):
        self.db_path = Path(db_path)
        self.scope_id = scope_id
        self.ctx = bind_context(scope_id=scope_id, principal_binding=principal_binding, actor=actor)
        self._db = None
        self._display_name = display_name

    def __enter__(self):
        self._db = open_db(self.db_path)
        register_scope(self._db, self.scope_id, self._display_name)
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def epoch(self):
        return scope_epoch(self._db, self.scope_id)

    def remember(self, key, value, *, kind="fact", source_refs, parent_ids=(),
                 expected_revision=None, replaces_ids=()):
        return _mem.remember(self._db, self.scope_id, key, value, kind=kind,
                             source_refs=source_refs, parent_ids=parent_ids,
                             expected_revision=expected_revision, replaces_ids=replaces_ids,
                             actor_binding=self.ctx.principal_binding)

    def correct(self, record_id, expected_revision, new_value, *, source_refs, parent_ids=()):
        return _corr.correct(self._db, self.scope_id, record_id, expected_revision,
                             new_value, source_refs=source_refs, parent_ids=parent_ids,
                             actor_binding=self.ctx.principal_binding)

    def retract(self, record_id, expected_revision, reason):
        return _corr.retract(self._db, self.scope_id, record_id, expected_revision,
                             reason, actor_binding=self.ctx.principal_binding)

    def resolve(self, key, expected_candidates, new_value, *, source_refs, parent_ids=()):
        return _corr.resolve_conflict(self._db, self.scope_id, key, expected_candidates,
                                      new_value, source_refs=source_refs, parent_ids=parent_ids,
                                      actor_binding=self.ctx.principal_binding)

    def begin(self, task):
        return _runs.begin(self._db, self.scope_id, task, actor_binding=self.ctx.principal_binding)

    def finish(self, run_id, key, value, *, parent_ids=(), idempotency_key=""):
        return _com.finish(self._db, self.scope_id, run_id, {"key": key, "value": value},
                           parent_ids=parent_ids, idempotency_key=idempotency_key,
                           actor_binding=self.ctx.principal_binding)

    def resume(self, run_id):
        return _rec.resume(self._db, self.scope_id, run_id)

    def rebase(self, run_id, candidate_result=None):
        # candidate_result: the caller's recompute from the latest rebased
        # run, {"key": ..., "value": ...}; persisted only when rebase is
        # exhausted (see recovery.rebase).
        return _rec.rebase(self._db, self.scope_id, run_id,
                           actor_binding=self.ctx.principal_binding,
                           candidate_result=candidate_result)

    def get_quarantine(self, run_id):
        """The quarantined candidate + next action for an exhausted run
        (T015 read path), or None when nothing was quarantined."""
        return _rec.get_quarantine(self._db, self.scope_id, run_id)

    def recall(self, query="", *, include_history=False, limit=20):
        return _ret.recall(self._db, self.scope_id, query,
                           include_history=include_history, limit=limit)

    def brief(self, goal=""):
        return _br.build_brief(self._db, self.scope_id, goal=goal)
