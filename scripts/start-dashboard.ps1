[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8775,
    # Defaults to D:\HoH\tmp\dev\control-plane.db (or HOH_DEV_DATABASE_PATH); the
    # real database under D:\HoH\data or D:\HoH-service is refused.
    [string]$DatabasePath,
    # Opt-in: a demo project is seeded into the dev database only when asked.
    [switch]$SeedDemo
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "dev-environment.ps1")
Assert-HohDevPort $Port
$database = Resolve-HohDevDatabase -DatabasePath $DatabasePath -RepoRoot $repoRoot
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run .\scripts\bootstrap-local.ps1 first."
}

$env:HOH_DATABASE_PATH = $database
$env:HOH_HOST = "127.0.0.1"
$env:HOH_PORT = "$Port"
$env:HOH_LOCAL_MODE = "1"
# Seeding is opt-in, and the demo project only ever goes into the dev database
# resolved above.
$env:HOH_SEED_DEMO = if ($SeedDemo) { "1" } else { "0" }

Write-Host "Harness Control dev dashboard: http://127.0.0.1:$Port (database $database)"
& $venvPython -m hoh_control_plane
