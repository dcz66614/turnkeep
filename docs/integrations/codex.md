# Codex integration (T025)

Status: config-only. Live verification is **NOT_RUN** — the `codex` CLI is
installed (codex-cli 0.164.0-alpha.9) but its login/auth state is unknown
here and no login is attempted. `codex mcp list` runs without auth and
reports "No MCP servers configured yet".

## Install

User scope (all projects):

```
codex mcp add turnkeep -- /usr/bin/env python3 -m turnkeep mcp \
  --scope codex --db ~/.local/share/turnkeep/turnkeep.sqlite3
```

Or append the `[mcp_servers.turnkeep]` table from
`adapters/codex/config.toml` to `~/.codex/config.toml` by hand (replace
`<turnkeep-work>` and `~` with absolute paths). Project scope: same table
in `<project>/.codex/config.toml` (only read for trusted projects).

The template in this repo has **not** been installed into
`~/.codex/config.toml` — that is the user's personal config; install it
only when Sam asks.

## Binding

Same DB/scope model as Claude Code, different actor label: scope `codex`,
principal `codex`. Same 9 tools, same core guarantees.

## Doctor

```
python scripts/check_adapter.py --client codex --mode config
```

config PASS = template parses as TOML with command+args.
live is NOT_RUN (no login attempted; install + a `codex` session needed).
