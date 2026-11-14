# Turnkeep installer — Windows PowerShell, T028.
# No admin rights, no execution-policy change (run as: powershell -ExecutionPolicy Bypass -File scripts\install.ps1),
# no system Python changes, no Docker. Uses the project venv.
# Uninstall never deletes data. NOT_RUN on a real Windows machine yet (T043).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = if ($env:PYTHON) { $env:PYTHON } else { "py" }
if (-not (Get-Command $Py -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.11+ not found on PATH. Install Python 3.11+ first (python.org or Microsoft Store)."
    exit 1
}
$ver = & $Py -c "import sys; print(sys.version_info[:2])"
Write-Host "python: $ver (need >= (3, 11))"

if (-not (Test-Path "$Root\.venv")) {
    Write-Host "creating venv at $Root\.venv ..."
    & $Py -m venv "$Root\.venv"
}
& "$Root\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
# Install the project itself into the venv so the `turnkeep` console
# script lands in $Root\.venv\Scripts (P1-f: previously the venv was
# created but the package was never installed and no `turnkeep` was on PATH).
Write-Host "installing turnkeep into the venv ..."
& "$Root\.venv\Scripts\python.exe" -m pip install -q -e "$Root"
Write-Host "turnkeep installed as $Root\.venv\Scripts\turnkeep.exe"
Write-Host "To run it from anywhere, add the venv Scripts dir to PATH (current session):"
Write-Host "  `$env:PATH = `"$Root\.venv\Scripts;`" + `$env:PATH"
# Core is stdlib-only; MCP extra is optional and installed only on request.
if ($env:TURNKEEP_WITH_MCP -eq "1") {
    & "$Root\.venv\Scripts\python.exe" -m pip install -q -r "$Root\requirements\mcp.lock"
}

if ($env:TURNKEEP_DATA_DIR) {
    New-Item -ItemType Directory -Force -Path $env:TURNKEEP_DATA_DIR | Out-Null
    Write-Host "data dir: $env:TURNKEEP_DATA_DIR (from TURNKEEP_DATA_DIR)"
} else {
    Write-Host "data dir: %APPDATA%\Turnkeep"
}

& "$Root\.venv\Scripts\turnkeep.exe" doctor --json | Select-Object -First 20
Write-Host ""
Write-Host "Installed. Try (with $Root\.venv\Scripts on PATH):"
Write-Host "  turnkeep demo"
Write-Host "  turnkeep init --scope myproject"
Write-Host "  turnkeep serve --scope myproject   # loopback workbench API"
Write-Host "Or without changing PATH:"
Write-Host "  $Root\.venv\Scripts\turnkeep.exe demo"
Write-Host "Uninstall: Remove-Item -Recurse $Root (data in %APPDATA%\Turnkeep is kept)."
