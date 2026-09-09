[CmdletBinding()]
param(
    [ValidateRange(5, 3600)]
    [int]$IntervalSeconds = 30,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run .\scripts\bootstrap-local.ps1 first."
}

$env:HOH_DATABASE_PATH = Join-Path $repoRoot "data\control-plane.db"
Write-Host "Harness Control lease scheduler: every $IntervalSeconds seconds"

$arguments = @("-m", "hoh_control_plane.scheduler", "--interval-seconds", "$IntervalSeconds")
if ($Once) {
    $arguments += "--once"
}
& $venvPython @arguments
exit $LASTEXITCODE
