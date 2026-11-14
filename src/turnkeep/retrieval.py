"""Filtered-first lexical retrieval + budget (T016).

Order (contract §8): scope + principal filter -> validity filter -> lexical
recall -> sort -> budget trim. Cross-scope records are excluded at the
candidate stage, never merely hidden by the UI. Only active records are
returned by default; include_history=True adds explicit history/失效 labels.
An active record that is not usable() (e.g. a descendant of an erased or
tainted parent) is never returned as active: default read paths drop it,
history read paths label it "unusable". This applies to recall() and to
search_by_key() alike (R2 D14).

Lexical: tokenise on non-alphanumeric boundaries + CJK characters (each
CJK char is a token; Latin words are lower-cased tokens). Score = number of
distinct query tokens present (substring match on the canonical value JSON
+ key). Deterministic order: score desc, updated_at asc, id asc.
No embeddings are required; if an embedding key is absent, lexical
fallback is used and explicitly labelled (no silent semantic claim).
"""
from __future__ import annotations
import re
from .storage import usable, _text

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9]+")


def tokenise(text):
    text = str(text)
    tokens = set(_WORD.findall(text.lower()))
    tokens |= set(_CJK.findall(text))
    return tokens


def recall(db, scope_id, query, *, include_history=False, limit=20, offset=0):
    scope_id = _text(scope_id, "scope")
    from .storage import scope_epoch  # raises NOT_FOUND for unknown scopes
    scope_epoch(db, scope_id)
    query_tokens = tokenise(query)
    rows = db.execute(
        "SELECT * FROM memories WHERE scope_id=? ORDER BY updated_at ASC, id ASC",
        (scope_id,)).fetchall()
    from .storage import dump_record
    scored = []
    for r in rows:
        if r["state"] != "active" and not include_history:
            continue
        # An active record whose ancestors are unavailable is not usable
        # (CORE_CONTRACT §41/§47): never return it as active. Default
        # recall drops it; history recall keeps it visible but labelled
        # "unusable", never "active". Non-active records keep their own
        # state label in history mode.
        unusable_active = r["state"] == "active" and not usable(db, scope_id, r["id"])
        if unusable_active and not include_history:
            continue
        rec = dump_record(db, r)
        hay = " ".join([rec["key"], str(rec["value"])]).lower()
        hay_tokens = tokenise(hay)
        score = len(query_tokens & hay_tokens) if query_tokens else 0
        if query_tokens and score == 0:
            continue
        rec["_score"] = score
        if unusable_active:
            rec["_label"] = "unusable"
        else:
            rec["_label"] = rec["state"] if include_history else "active"
        rec["_lexical_fallback"] = True  # no embedding used; explicit
        scored.append(rec)
    scored.sort(key=lambda r: (-r["_score"], r["updated_at"], r["id"]))
    return scored[offset:offset + limit]


def search_by_key(db, scope_id, key, *, include_history=False):
    from .storage import dump_record
    q = "SELECT * FROM memories WHERE scope_id=? AND key=?"
    args = [scope_id, key]
    if not include_history:
        q += " AND state='active'"
    q += " ORDER BY created_at ASC"
    out = []
    for r in db.execute(q, args):
        # Same usable() rule as recall() (CORE_CONTRACT §41/§47, R2 D14):
        # an active record whose ancestors are unavailable is never
        # returned as active. Default mode drops it; history mode keeps it
        # visible but labelled "unusable", never "active".
        unusable_active = r["state"] == "active" and not usable(db, scope_id, r["id"])
        if unusable_active and not include_history:
            continue
        rec = dump_record(db, r)
        if unusable_active:
            rec["_label"] = "unusable"
        out.append(rec)
    return out
