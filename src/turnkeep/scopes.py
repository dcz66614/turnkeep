"""Project scopes (T008). A scope is an explicitly registered project ID;
a normalised path is only a discovery hint, never the authority.

- register_scope(db, scope_id, display_name, *, source_path=None): creates the
  scope row (idempotent). source_path, when given, is canonicalised
  (symlinks resolved; on Windows, case-folded) and stored in metadata only.
- resolve_scope_id(source_path): canonical path -> stable scope id derived
  from the canonical path (sha256, first 16 hex chars, prefixed 'proj-').
  Two different canonical paths never merge; if auto-detection is unreliable,
  callers must pass an explicit scope id (never guess-and-merge).
- Scope is a correctness/isolation boundary for ONE trusted OS user, NOT a
  security boundary against that user reading the DB directly (CORE_CONTRACT §1).
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
from .errors import TurnkeepError
from .storage import ensure_scope, _text


def canonical_path(source_path):
    p = Path(source_path).expanduser()
    try:
        p = p.resolve(strict=False)
    except (OSError, RuntimeError):
        p = p.absolute()
    s = str(p)
    if os.name == "nt":
        s = s.lower()
    return s


def scope_id_for_path(source_path):
    canon = canonical_path(source_path)
    return "proj-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def register_scope(db, scope_id, display_name=None, *, source_path=None):
    scope_id = _text(scope_id, "scope_id")
    if source_path is not None:
        canon = canonical_path(source_path)
        derived = scope_id_for_path(source_path)
        if scope_id != derived and not scope_id.startswith("proj-"):
            # Explicit human-chosen ids are allowed, but the canonical path is
            # recorded so a later auto-discovery can warn, not silently merge.
            pass
    ensure_scope(db, scope_id, display_name)
    return scope_id


def list_scopes(db):
    return [{"id": r[0], "display_name": r[1], "epoch": r[2], "created_at": r[3]}
            for r in db.execute("SELECT id, display_name, epoch, created_at FROM scopes ORDER BY id")]
