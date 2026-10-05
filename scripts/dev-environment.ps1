# Shared by the dev start scripts (dashboard, scheduler, supervisor). Dot-source it.
#
# A dev instance never opens the real control-plane database and never takes the
# installed host's ports: a newer checkout pointed at the real database used to
# upgrade its schema (so the installed, older service no longer started) and to
# seed a demo project into it.

$HohDevDefaultDatabase = "D:\HoH\tmp\dev\control-plane.db"
$HohDevDefaultPort = 8775
# The installed desktop host's UI and MCP ports (D:\HoH-service\config\settings.json).
$HohInstalledPorts = @(8765, 8931)

function Resolve-HohDevDatabase {
    param(
        [string]$DatabasePath,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    if (-not $DatabasePath) {
        $DatabasePath = if ($env:HOH_DEV_DATABASE_PATH) { $env:HOH_DEV_DATABASE_PATH } else { $HohDevDefaultDatabase }
    }
    $full = [System.IO.Path]::GetFullPath(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DatabasePath))
    $forbidden = @("D:\HoH\data", "D:\HoH-service", (Join-Path $RepoRoot "data"))
    foreach ($root in $forbidden) {
        $prefix = [System.IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
        if ($full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ("Refusing to start a dev instance on $full. Dev scripts never open a database " +
                "under $root, which holds the real control-plane data. Use the default " +
                "$HohDevDefaultDatabase or another path under tmp.")
        }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $full) | Out-Null
    return $full
}

function Assert-HohDevPort {
    param([Parameter(Mandatory = $true)][int]$Port)
    if ($HohInstalledPorts -contains $Port) {
        throw "Port $Port belongs to the installed HoH host; choose another dev port (default $HohDevDefaultPort)."
    }
}
