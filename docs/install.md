# Install (T028)

Linux is verified here; macOS and Windows are **NOT_RUN** — real
verification is T043. Path rules for those platforms are unit-tested
(`tests/platform/test_paths.py`, monkeypatched `platform.system()`), which
is not platform evidence.

## POSIX (Linux / macOS)

```
bash scripts/install.sh
```

No admin rights, no system Python changes, no Docker. Creates `.venv`
in the checkout if missing (core is stdlib-only; set
`TURNKEEP_WITH_MCP=1` to also install the MCP extra). Data dir:

- Linux: `~/.local/share/turnkeep`
- macOS: `~/Library/Application Support/turnkeep`
- Override: `TURNKEEP_DATA_DIR=/path bash scripts/install.sh`

## Windows (PowerShell)

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

No admin rights, no execution-policy change persisted, no system Python
changes. Data dir: `%APPDATA%\Turnkeep` (or `TURNKEEP_DATA_DIR`).

## After install

```
.venv/bin/python -m turnkeep doctor --json     # diagnostics
.venv/bin/python -m turnkeep demo              # scripted J01 demo
.venv/bin/python -m turnkeep init --scope myproject
.venv/bin/python -m turnkeep serve --scope myproject   # loopback workbench API
```

## Start / stop

- Serve: `python -m turnkeep serve --scope <id> [--port N]` — prints a
  URL + per-process session token, loopback only; Ctrl-C stops it.
- MCP: `python -m turnkeep mcp --scope <id> [--db PATH]` — stdio server
  for Claude Code / Codex (see `docs/integrations/`).
- No daemon is installed; there is nothing to stop beyond the process.

## Uninstall

Delete the checkout (`rm -rf <turnkeep-work>`). Data in the data dir is
kept — delete it explicitly if you want it gone.

## Doctor

`python -m turnkeep doctor --json` reports Python/SQLite versions, data
dir writability, DB schema version + scopes + last event, server commands,
client config status, and unverified capabilities (macOS/Windows/live
clients are NOT_RUN until T026/T043 run for real).
