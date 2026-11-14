# Claude Code integration (T024)

Status: config-only. Live verification is **NOT_RUN** — the `claude` CLI is
not installed on this VM and no Claude account login is available here.

## Install

Copy `adapters/claude-code/.mcp.json` to the project root as `.mcp.json`
(project scope, committed) or merge the `turnkeep` entry into
`~/.claude.json` (user scope). Replace `<turnkeep-work>` with the absolute
path of this checkout and `~` in `--db` with the absolute home path.
Restart Claude Code, then run `/mcp` — `turnkeep` should appear connected.

Or via CLI (in the project folder):

```
claude mcp add --transport stdio turnkeep -- /usr/bin/env python3 -m turnkeep mcp \
  --scope claude-code --db ~/.local/share/turnkeep/turnkeep.sqlite3
```

## Binding

One server process = one scope + one principal. This adapter binds scope
`claude-code` and principal `claude-code`. The 9 tools are:
begin, recall, remember, correct, retract, resolve_conflict, resume,
finish, brief. Tools take no scope argument; the binding is fixed at
server start, so a client cannot read another scope's records.

## Sharing a DB with Codex

Point both adapters at the same `--db` path. Each keeps its own actor
label and run ids; the core (no silent overwrite, CAS, AND parents, taint)
is enforced by the store, not by the client.

## Doctor

```
python scripts/check_adapter.py --client claude-code --mode config
```

config PASS = template parses and names turnkeep with command+args.
live is NOT_RUN on this machine (claude CLI absent).
