#!/usr/bin/env bash
# Turnkeep installer — POSIX (Linux/macOS), T028.
# No admin rights, no system Python changes, no Docker. Uses the project
# venv (created if missing) and installs nothing outside the checkout
# except the data directory. Uninstall never deletes data.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${TURNKEEP_DATA_DIR:-}"
PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: python3 (>=3.11) not found on PATH. Install Python 3.11+ first." >&2
  exit 1
fi
VER="$("$PY" -c 'import sys; print(sys.version_info[:2])')"
echo "python: $VER (need >= (3, 11))"

if [ ! -d "$ROOT/.venv" ]; then
  echo "creating venv at $ROOT/.venv ..."
  "$PY" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip 2>/dev/null || true
# Install the project itself into the venv so the `turnkeep` console
# script lands in $ROOT/.venv/bin (P1-f: previously the venv was created
# but the package was never installed and no `turnkeep` was on PATH).
#
# P1-f / R2 A5: `pip install -e "$ROOT"` FAILS with no network — pip's
# isolated build env must download setuptools>=68 (pyproject.toml
# build-system requires) and there is no index to fetch it from. The
# shipped wheel (release/artifacts/) needs no build backend at all, so
# it installs offline. Rules:
#   * offline (PIP_NO_INDEX=1 or TURNKEEP_OFFLINE=1) + wheel present
#     -> install from the wheel, never attempt the editable build;
#   * otherwise try the editable install first, and fall back to the
#     wheel if that fails (e.g. no network) instead of dying.
# The wheel is stdlib-only, so --no-deps is safe and keeps the install
# offline. The editable install is still preferred online so the venv
# tracks the checkout source.
WHEEL="$ROOT/release/artifacts/turnkeep-0.1.0-py3-none-any.whl"
install_from_wheel() {
  echo "installing turnkeep from the shipped wheel (offline-safe) ..."
  "$ROOT/.venv/bin/python" -m pip install -q --no-index --no-deps "$WHEEL"
}
echo "installing turnkeep into the venv ..."
if [ -f "$WHEEL" ] && { [ "${PIP_NO_INDEX:-}" = "1" ] || [ "${TURNKEEP_OFFLINE:-}" = "1" ]; }; then
  install_from_wheel
elif "$ROOT/.venv/bin/python" -m pip install -q -e "$ROOT" 2>/dev/null; then
  echo "turnkeep installed editable from $ROOT"
elif [ -f "$WHEEL" ]; then
  echo "WARN: editable install failed (no network?) — falling back to the shipped wheel."
  install_from_wheel
else
  echo "ERROR: editable install failed and no shipped wheel found at $WHEEL." >&2
  exit 1
fi
echo "turnkeep installed as $ROOT/.venv/bin/turnkeep"
echo "To run it from anywhere, add the venv bin dir to PATH:"
echo "  export PATH=\"$ROOT/.venv/bin:\$PATH\""
# Core is stdlib-only; MCP extra is optional and installed only on request.
if [ "${TURNKEEP_WITH_MCP:-}" = "1" ]; then
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements/mcp.lock" 2>/dev/null || \
    echo "WARN: MCP extra install failed (offline?) — core still works; retry later."
fi

if [ -n "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR"
  echo "data dir: $DATA_DIR (from TURNKEEP_DATA_DIR)"
else
  case "$(uname -s)" in
    Darwin) echo "data dir: ~/Library/Application Support/turnkeep" ;;
    *)      echo "data dir: ~/.local/share/turnkeep" ;;
  esac
fi

"$ROOT/.venv/bin/turnkeep" doctor --json | head -20
echo ""
echo "Installed. Try (with $ROOT/.venv/bin on PATH):"
echo "  turnkeep demo"
echo "  turnkeep init --scope myproject"
echo "  turnkeep serve --scope myproject   # loopback workbench API"
echo "Or without changing PATH:"
echo "  $ROOT/.venv/bin/turnkeep demo"
echo "Uninstall: rm -rf $ROOT (data in the data dir above is kept)."
