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

# Only skill directories are installed. The wakeup reference also names
# deploy/ and docs/ paths, but every one of those files hardcodes this host's
# absolute paths, so shipping them would put local workspace topology inside an
# installed artifact. For portable HTTP MCP configuration alongside skills,
# build a role kit with hoh-agent-kit; see docs/AGENT_KIT.md in this repository.
Write-Host "Installed $($skillNames -join ', ') for $Target at $targetRoot"
Write-Host "Restart the agent client so it discovers the copied skills."
