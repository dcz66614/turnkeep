"""T016 budget tests: chars, not tokens; must_keep never dropped silently."""
import pytest
from turnkeep.budget import count_chars, estimate_tokens, trim_to_budget


def test_counts_are_chars():
    assert count_chars("hello") == 5
    assert count_chars("你好") == 2  # characters, not bytes


def test_estimate_is_labelled_estimate_not_exact():
    # 8 chars -> 2 estimated tokens at 4 chars/token; the function name and
    # key say "estimate" — callers must never present it as exact.
    assert estimate_tokens("12345678") == 2


def test_trim_keeps_must_keep_and_reports_overrun():
    items = [{"must_keep": False, "text": "x" * 90},
             {"must_keep": True, "text": "CONFLICT!"},
             {"must_keep": False, "text": "y" * 90}]
    out = trim_to_budget(items, max_chars=100, render=lambda it: it["text"])
    assert any("CONFLICT" in i["text"] for i in out["kept"])
    assert len(out["dropped"]) == 1
    assert out["over_budget"] is False  # must_keep (9 chars) fits after first item
