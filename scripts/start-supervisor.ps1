[CmdletBinding()]
param(
    [ValidateRange(1, 32)]
    [int]$MaxWorkers = 2,
    [ValidateRange(60, 86400)]
    [int]$WorkerTimeoutSeconds = 2100,
    # Defaults to D:\HoH\tmp\dev\control-plane.db (or HOH_DEV_DATABASE_PATH); the
    # real database under D:\HoH\data or D:\HoH-service is refused.
    [string]$DatabasePath
)

# Queued jobs (worktree provisioning, candidate submission, QA test runs, merges)
# run only while a supervisor drains the queue: HOH_WORKER_MODE defaults to
# "supervised", so without this process they stay QUEUED.
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "dev-environment.ps1")
$database = Resolve-HohDevDatabase -DatabasePath $DatabasePath -RepoRoot $repoRoot
$venvPython = Join-Path $repoRoot ".venv-hoh\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Local environment is missing. Run .\scripts\bootstrap-local.ps1 first."
}

$env:HOH_DATABASE_PATH = $database
$env:HOH_WORKER_MODE = "supervised"
Write-Host "Harness Control dev supervisor: $MaxWorkers worker(s) (database $database)"

& $venvPython -m hoh_control_plane.supervisor --max-workers "$MaxWorkers" --worker-timeout "$WorkerTimeoutSeconds"
exit $LASTEXITCODE
