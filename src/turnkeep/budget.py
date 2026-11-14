"""Context budget (T016). Counts are CHARACTERS unless stated otherwise.
estimated_tokens is a conservative chars/4 estimate, labelled as an
estimate — Python len() is never presented as an exact token count.
Budget trimming never drops conflict labels or source refs.
"""
from __future__ import annotations

CHARS_PER_TOKEN_ESTIMATE = 4


def count_chars(text):
    return len(str(text))


def estimate_tokens(text):
    """Conservative estimate only; report as 'estimated_tokens'."""
    return max(1, (len(str(text)) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE)


def trim_to_budget(items, *, max_chars, render):
    """Keep items in order until max_chars; always keep items flagged
    must_keep (conflicts / changes) even if that exceeds the budget, and
    report the overrun instead of silently dropping them."""
    kept, used, dropped = [], 0, []
    for it in items:
        text = render(it)
        n = len(text)
        if used + n > max_chars and not it.get("must_keep"):
            dropped.append(it)
            continue
        kept.append(it)
        used += n
    return {"kept": kept, "dropped": dropped, "chars_used": used,
            "max_chars": max_chars, "over_budget": used > max_chars}
