[CmdletBinding()]
param(
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# .venv-hoh is shared by every session on this machine, and `pip install -e`
# points it at whichever checkout ran it. Run from a ticket worktree, it left the
# shared hoh-* console scripts running that worktree's (old) code. Only the main
# checkout on the main branch may install into it.
$mainCheckout = "D:\HoH"
if (-not [string]::Equals($repoRoot.TrimEnd('\'), $mainCheckout, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw ("Refusing to bootstrap from $repoRoot. Only $mainCheckout on branch main installs into the " +
        "shared .venv-hoh; a worktree runs tests with scripts\test.ps1, which puts its own src first.")
}
$branch = (& git -C $repoRoot rev-parse --abbrev-ref HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
    throw "Refusing to bootstrap from $repoRoot on branch '$branch'. Check out main in $mainCheckout first."
}

$venvPath = Join-Path $repoRoot ".venv-hoh"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

function Invoke-Checked {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Command)
    & $Command[0] $Command[1..($Command.Count - 1)]
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $($Command -join ' ')"
    }
}

function Find-CompatiblePython {
    param([string]$RequestedCommand)

    $candidates = [System.Collections.Generic.List[string]]::new()
    $requested = Get-Command $RequestedCommand -ErrorAction SilentlyContinue
    if ($requested) { [void]$candidates.Add($requested.Path) }

    $programFilesPython = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $programFilesPython) {
        Get-ChildItem -LiteralPath $programFilesPython -Directory | ForEach-Object {
            $candidate = Join-Path $_.FullName "python.exe"
            if (Test-Path -LiteralPath $candidate) { [void]$candidates.Add($candidate) }
        }
    }

    $codexRuntimes = Join-Path $env:USERPROFILE ".cache\codex-runtimes"
    if (Test-Path -LiteralPath $codexRuntimes) {
        Get-ChildItem -LiteralPath $codexRuntimes -Filter "python.exe" -File -Recurse |
            Where-Object { $_.FullName -like "*\dependencies\python\python.exe" } |
            ForEach-Object { [void]$candidates.Add($_.FullName) }
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        $platform = & $candidate -c "import sysconfig; print(sysconfig.get_platform())" 2>$null
        if ($LASTEXITCODE -eq 0 -and $platform -notmatch "mingw|msys") {
            return $candidate
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Find-CompatiblePython $PythonCommand
    if (-not $python) {
        throw "No compatible CPython runtime was found. Install CPython 3.12+ or pass -PythonCommand <absolute-path>."
    }
    Invoke-Checked $python "-m" "venv" $venvPath
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment creation did not provide $venvPython."
}

Invoke-Checked $venvPython "-m" "pip" "install" "--upgrade" "pip"
Invoke-Checked $venvPython "-m" "pip" "install" "-e" "${repoRoot}[dev]"

Write-Host "Local environment is ready: $venvPython"
Write-Host "Next: .\scripts\start-dashboard.ps1"
