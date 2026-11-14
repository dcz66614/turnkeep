# DRAFT — DO NOT POST. Local draft only (T044).

This is a draft Hacker News post. It has not been posted, and nothing
in this repository posts on your behalf. Posting requires explicit
authorisation and a re-check of the name ("Turnkeep" is a working name
only; turnkeep.app is a live, same-spelled rental product).

---

**Title:** Turnkeep: local-first agent working memory that says what
went stale when requirements change (offline RC, Linux)

**Body:**

I built Turnkeep for a problem I kept hitting when two agents work on
the same project in turns: agent A finishes something, the requirement
changes, agent B picks it up and silently reuses the now-wrong result
— or overwrites it without a trace.

Turnkeep is a local SQLite memory with a few hard rules: same key +
different value is an explicit conflict (no last-writer-wins); work
depends on its evidence through AND parents, so a retracted source
taints everything built on it; a finish based on an old snapshot is
rejected with STALE_SNAPSHOT, and resume shows the diff for a
controlled redo. A receipt proves only the local SQLite commit —
nothing about the outside world.

60-second demo, no key, no network, no model:

```
python -m turnkeep demo --scenario export-format --no-model
```

Honest state of this 0.1.0 offline RC (Linux only): the core, CLI,
Python SDK, 9 MCP tools and a loopback workbench are verified
(225/225 pytest). But the real two-client handover between Claude
Code and Codex has not run yet, no live-model evaluation has run, and
Windows/macOS installs have not run. The performance gate fails at
scale: finish p95 is 7.7s at 10k records and 12.3s at 100k, because
begin() snapshots everything and recall() scans the whole table.
That is documented in docs/KNOWN_LIMITATIONS.md, not hidden.

Nearest neighbours (Turnbook, Truenex, project-memory, Engram, …)
already do cross-tool shared memory, so this is not a "first" claim —
the bet is the change-recovery combination, and it is still to be
proven by a live trial.

MIT, Python ≥ 3.11, stdlib-only core. Happy to hear where the
stale-finish rejection would help or just get in your way.
