# This script must keep stdout reserved for MCP stdio protocol messages.
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run scripts/bootstrap-local.ps1 first."
}

& $venvPython -m hoh_control_plane.mcp_server
exit $LASTEXITCODE
