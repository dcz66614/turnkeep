# Claude Code adapter (T024) — config-only. Live verification is NOT_RUN:
# the `claude` CLI is not installed on this VM and no Claude account login
# is available here. Do not install/login on Sam's behalf.
#
# Install: copy this file to the project root as `.mcp.json` (project scope,
# committed) or merge the `turnkeep` entry into `~/.claude.json` (user scope).
# Replace `<turnkeep-work>` with the absolute path of this checkout and
# `~` in --db with the absolute home path (Claude Code does not expand `~`
# inside args). Then restart Claude Code and run `/mcp` to confirm
# `turnkeep` is connected; the server exposes the 9 tools
# (begin, recall, remember, correct, retract, resolve_conflict, resume,
# finish, brief) bound to scope `claude-code` and principal `claude-code`.
#
# One server process = one scope + one principal. To share one Turnkeep DB
# with Codex, point both adapters at the same --db path; each keeps its own
# actor label (claude-code / codex) and its own run ids.
#
# Doctor: python scripts/check_adapter.py --client claude-code --mode config
#   config: PASS when this file parses and names turnkeep with command+args.
#   live:   NOT_RUN (claude CLI absent on this machine).
