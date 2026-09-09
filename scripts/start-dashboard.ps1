[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$NoSeedDemo
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run .\scripts\bootstrap-local.ps1 first."
}

$env:HOH_DATABASE_PATH = Join-Path $repoRoot "data\control-plane.db"
$env:HOH_HOST = "127.0.0.1"
$env:HOH_PORT = "$Port"
$env:HOH_LOCAL_MODE = "1"
$env:HOH_SEED_DEMO = if ($NoSeedDemo) { "0" } else { "1" }

Write-Host "Harness Control dashboard: http://127.0.0.1:$Port"
& $venvPython -m hoh_control_plane
