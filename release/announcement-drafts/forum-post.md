# DRAFT — DO NOT POST. Local draft only (T044).

Draft for a forum / discussion post (e.g. a coding-agents or
local-first tools forum). It has not been posted, and nothing in this
repository posts or messages on your behalf. Posting requires explicit
authorisation and a name re-check ("Turnkeep" is a working name only).

---

**Subject:** [Show] Turnkeep 0.1.0 — offline RC: agent handover memory
that rejects stale finishes

**Body:**

Turnkeep is a local-first project memory for agents working in turns
on the same project. The idea is small: when a requirement changes
after work is done, the next agent should be told what went stale —
with the evidence — instead of silently reusing or overwriting it.

How it works (0.1.0, contract v0.1, Linux offline):

- Explicit keys: same key + different value = a conflict, not
  last-writer-wins. Corrections are compare-and-swap.
- AND parents: a result depends on all of its evidence; if any parent
  goes bad (corrected, retracted, conflicted), the child is tainted.
- Server-side snapshot + atomic finish: a finish from an old snapshot
  is rejected (`STALE_SNAPSHOT`); `resume` shows the diff and `rebase`
  recomputes (max 2 automatic attempts).
- Idempotent receipts — but the receipt proves only the local SQLite
  commit, nothing external.
- Export/import (JSONL + manifest), Markdown import of structured
  items only, expiry, erasure with a residual report, backup/restore.

Try the scripted demo (no key, no model, fixed fixtures):

```
python -m turnkeep demo --scenario export-format --no-model
python examples/python/handoff.py --temp-db
```

What is honest about this release: the real handover between two
actual MCP clients (Claude Code ↔ Codex) has not run yet, no
live-model evaluation has run, and Windows/macOS installs have not
run. Performance fails its own gate at scale (finish p95 7.7s at 10k
records, 12.3s at 100k; recall p95 3.2s at 100k). All of it is in
docs/KNOWN_LIMITATIONS.md.

It is not the first shared agent memory — Turnbook, Truenex Memory,
project-memory and others already share memory across tools. The open
question is whether the change-recovery combination is actually worth
it, and that still needs a live trial.

MIT. Python ≥ 3.11, stdlib-only core. Feedback welcome, especially:
where would the stale-finish rejection save you, and where would it
just get in the way?
