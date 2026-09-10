[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Codex", "ClaudeCode")]
    [string]$Target,

    [Parameter(Mandatory)]
    [ValidateSet("planner", "developer", "qa", "integrator")]
    [string]$Role,

    [ValidateSet("user", "project")]
    [string]$Scope = "user"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$skillSourceRoot = Join-Path $repoRoot "skills"

if ($Scope -eq "project") {
    $targetRoot = if ($Target -eq "Codex") {
        Join-Path $repoRoot ".codex\skills"
    } else {
        Join-Path $repoRoot ".claude\skills"
    }
} else {
    $targetRoot = if ($Target -eq "Codex") {
        Join-Path $env:USERPROFILE ".codex\skills"
    } else {
        Join-Path $env:USERPROFILE ".claude\skills"
    }
}

$skillNames = @("hoh-control-plane", "hoh-$Role")
New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

foreach ($skillName in $skillNames) {
    $source = Join-Path $skillSourceRoot $skillName
    $destination = Join-Path $targetRoot $skillName
    if (-not (Test-Path -LiteralPath (Join-Path $source "SKILL.md"))) {
        throw "Skill source is missing: $source"
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Copy-Item -Path (Join-Path $source "*") -Destination $destination -Recurse -Force
}

# The base skill's wakeup reference names repository-relative paths. Ship those
# assets beside the base skill so an installed agent can finish wakeup setup from
# the installed artifact alone, instead of being sent to a repository it may not
# have. The layout mirrors the repository so the referenced paths still resolve.
$companionRoot = Join-Path $targetRoot "hoh-control-plane"
$companionPaths = @("deploy\codex", "deploy\claude-code", "docs\ROLE_WAKEUPS.md")

foreach ($companionPath in $companionPaths) {
    $companionSource = Join-Path $repoRoot $companionPath
    if (-not (Test-Path -LiteralPath $companionSource)) {
        throw "Wakeup asset referenced by the base skill is missing: $companionSource"
    }
    $companionDestination = Join-Path $companionRoot $companionPath
    $companionParent = Split-Path -Parent $companionDestination
    New-Item -ItemType Directory -Force -Path $companionParent | Out-Null
    if (Test-Path -LiteralPath $companionSource -PathType Container) {
        New-Item -ItemType Directory -Force -Path $companionDestination | Out-Null
        Copy-Item -Path (Join-Path $companionSource "*") -Destination $companionDestination -Recurse -Force
    } else {
        Copy-Item -Path $companionSource -Destination $companionDestination -Force
    }
}

Write-Host "Installed $($skillNames -join ', ') for $Target at $targetRoot"
Write-Host "Restart the agent client so it discovers the copied skills."
