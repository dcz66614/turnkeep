"""Structured briefs (T017). Deterministic template — no LLM required.
Fixed fields: goal, current status, valid decisions, open questions, recent
changes, suggested next steps, evidence links (record IDs). Every key claim
carries a record ID; missing evidence is written as 'unknown', never filled
in. Budget trimming (budget.py) keeps conflicts + changes (must_keep).
Secret-like values (api keys, passwords, tokens) are redacted from briefs.
"""
from __future__ import annotations
import json
import re
from .memory import active_records, list_conflicts
from .storage import scope_epoch
from .budget import count_chars, estimate_tokens, trim_to_budget

_SECRET = re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token|bearer)\s*[:=]\s*\S+")
_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token|bearer)")


def redact(text):
    return _SECRET.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + ": [REDACTED]", str(text))


def redact_value(value):
    """Recursively redact secrets in a structured value (P0-4 rework).

    Walks dicts/lists; any dict key matching the secret pattern has its
    whole value replaced with "[REDACTED]"; every string leaf also goes
    through the flat-text redact() rule. Non-string leaves pass through
    unchanged. The flat regex alone cannot match inside JSON serialisations
    (e.g. {"api_key": "sk-..."}), so structure must be redacted first.
    """
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact_value(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def build_brief(db, scope_id, *, goal="", max_chars=4000):
    records = active_records(db, scope_id)
    conflicts = list_conflicts(db, scope_id)
    decisions = [r for r in records if r["kind"] == "decision"]
    attempts = [r for r in records if r["kind"] == "attempt"]
    changes = db.execute(
        "SELECT scope_epoch, event_type, affected_ids_json, created_at FROM events "
        "WHERE scope_id=? ORDER BY scope_epoch DESC LIMIT 5", (scope_id,)).fetchall()
    items = []
    for r in records:
        # Redact the structure first, then serialise: the flat redact()
        # regex cannot match a secret inside JSON text.
        items.append({"must_keep": False, "text": f"{r['key']}: {redact(json.dumps(redact_value(r['value']), ensure_ascii=False))} [{r['id']}]"})
    for c in conflicts:
        items.append({"must_keep": True, "text": f"CONFLICT {c['key']}: {redact(json.dumps(redact_value(c['value']), ensure_ascii=False))} [{c['id']}]"})
    trimmed = trim_to_budget(items, max_chars=max_chars, render=lambda it: it["text"])
    # P2 (R2 D13): trim_to_budget() only ever saw the rendered record
    # text, but valid_decisions / open_questions / attempts are emitted
    # below as full structured values OUTSIDE that budget. Count them:
    # chars_used / budget_overrun must reflect what the brief actually
    # contains, not just the trimmed record list. The structured
    # sections are not trimmed (decisions and conflicts are the point
    # of the brief); instead the overrun is reported honestly.
    decisions_out = [{"key": r["key"], "value": redact_value(r["value"]), "record_id": r["id"]} for r in decisions]
    questions_out = [{"key": c["key"], "value": redact_value(c["value"]), "record_id": c["id"]} for c in conflicts]
    attempts_out = [{"key": r["key"], "value": redact_value(r["value"]), "record_id": r["id"]} for r in attempts]
    structured_chars = sum(
        len(json.dumps(section, ensure_ascii=False))
        for section in (decisions_out, questions_out, attempts_out))
    chars_used = trimmed["chars_used"] + structured_chars
    brief = {
        "scope": scope_id, "goal": goal or "unknown",
        "epoch": scope_epoch(db, scope_id),
        "current_status": "unknown" if not records else f"{len(records)} active records",
        "valid_decisions": decisions_out,
        "open_questions": questions_out,
        "attempts": attempts_out,
        "recent_changes": [{"epoch": ch[0], "event": ch[1], "affected_ids": json.loads(ch[2]), "at": ch[3]} for ch in changes],
        "suggested_next_steps": (["Resolve open conflicts before relying on affected records"] if conflicts
                                 else ["Continue from the latest active records"]),
        "records": trimmed["kept"],
        "dropped_record_count": len(trimmed["dropped"]),
        "chars_used": chars_used, "max_chars": max_chars,
        "estimated_tokens": estimate_tokens(" ".join(i["text"] for i in trimmed["kept"])),
        "budget_overrun": chars_used > max_chars,
    }
    return brief
