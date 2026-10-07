[CmdletBinding()]
param(
    [ValidateRange(5, 3600)]
    [int]$IntervalSeconds = 30,
    # Defaults to D:\HoH\tmp\dev\control-plane.db (or HOH_DEV_DATABASE_PATH); the
    # real database under D:\HoH\data or D:\HoH-service is refused.
    [string]$DatabasePath,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "dev-environment.ps1")
$database = Resolve-HohDevDatabase -DatabasePath $DatabasePath -RepoRoot $repoRoot
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run .\scripts\bootstrap-local.ps1 first."
}

$env:HOH_DATABASE_PATH = $database
Write-Host "Harness Control dev scheduler: every $IntervalSeconds seconds (database $database)"

$arguments = @("-m", "hoh_control_plane.scheduler", "--interval-seconds", "$IntervalSeconds")
if ($Once) {
    $arguments += "--once"
}
& $venvPython @arguments
exit $LASTEXITCODE
