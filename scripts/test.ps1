# The one supported way to run this checkout's tests.
#
# .venv-hoh carries an editable install of an older worktree, a bare `python`
# resolves to an msys interpreter without pytest, and the system Python lacks
# pytest-cov. Each of those fails differently, or worse, passes against the wrong
# source. This script pins the interpreter and puts this checkout's src/ first.
# tests/conftest.py refuses to run if the imported package is not this checkout's.
#
# Usage:
#   scripts\test.ps1                 full suite, with the 80% coverage gate
#   scripts\test.ps1 --fast          fast lane: -m "not slow and not git", no gate
#   scripts\test.ps1 tests\test_x.py [pytest args]   targeted run, no gate
# Lanes, markers and CI are described in docs/TESTING.md.

$ErrorActionPreference = "Stop"
$checkout = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = $env:HOH_TEST_PYTHON
if (-not $python) {
    # Worktrees under tmp/ share the main checkout's environment.
    $candidates = @(
        (Join-Path $checkout ".venv-hoh\Scripts\python.exe"),
        "D:\HoH\.venv-hoh\Scripts\python.exe"
    )
    $python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $python) {
    throw "No .venv-hoh interpreter found. Run scripts\bootstrap-local.ps1 or set HOH_TEST_PYTHON."
}

$PytestArgs = @($args)
# PowerShell 7's -File binder splits e.g. --cov-report=xml:report.py at
# the colon. Recover the original tokens only when this is the CLI entry point;
# calls from another PowerShell script must keep their own invocation arguments.
$nativeArgs = [Environment]::GetCommandLineArgs()
for ($index = 1; $index -lt ($nativeArgs.Count - 1); $index++) {
    if ($nativeArgs[$index] -ieq '-File' -or $nativeArgs[$index] -ieq '-f') {
        if ([System.IO.Path]::GetFullPath($nativeArgs[$index + 1]) -eq $PSCommandPath) {
            $PytestArgs = @($nativeArgs | Select-Object -Skip ($index + 2))
        }
        break
    }
}
$fast = $PytestArgs -contains "--fast"
$PytestArgs = @($PytestArgs | Where-Object { $_ -ne "--fast" })
# A run that names tests or filters them is targeted; anything else is the full
# suite and must clear the coverage gate (it is not in pyproject addopts, so a
# targeted run never fails on coverage).
$selected = $false
$ambiguous = $false
# Option values are not test paths (e.g. --junitxml report.py). Keep this
# bounded to supported/common options; unknown ambiguous values stay full.
$valueOptions = @('-n', '--numprocesses', '-p', '-o', '--override-ini', '-c', '--config-file',
    '--junitxml', '--junit-xml', '--cov', '--cov-report', '--cov-config', '--cov-fail-under',
    '--basetemp', '--log-file', '--durations', '--durations-min', '--maxfail', '--dist', '--maxschedchunk')
$flags = @('-q', '-v', '-vv', '-s', '-x', '--ff', '--failed-first', '--no-cov', '--cov-branch',
    '--cov-append', '--collect-only', '--co', '--strict-markers', '--run-ui', '--disable-warnings')
for ($index = 0; $index -lt $PytestArgs.Count; $index++) {
    $argument = $PytestArgs[$index]
    if ($argument -match '^-(k|m)(=|[^-]|$)' -or $argument -match '^--(lf|last-failed|deselect)(=|$)') {
        $selected = $true
        if ($argument -in @('-k', '-m', '--deselect')) { $index++ }
    } elseif ($argument -in $valueOptions) {
        $index++
    } elseif ($argument.StartsWith('-')) {
        if ($argument -notin $flags -and $argument -notmatch '=') { $ambiguous = $true }
    } elseif ($argument -match '::|\.py$|^(tests|\.\\tests|\./tests)([\\/]|$)') {
        $selected = $true
    } else {
        $ambiguous = $true
    }
}
$fullRun = (-not $fast) -and ((-not $selected) -or $ambiguous)
if ($fast) {
    $PytestArgs = @('-m', 'not slow and not git') + @($PytestArgs)
}
if ($fullRun) {
    # A full run owns its coverage policy. One user --cov option must not
    # suppress the gate, and stale coverage must not attest this revision.
    if ($env:PYTEST_ADDOPTS) {
        throw "A full run requires empty PYTEST_ADDOPTS; pass explicit pytest arguments."
    }
    if (Get-ChildItem Env: | Where-Object { $_.Name -match '^(COVERAGE|COV_CORE)' }) {
        throw "A full run requires no ambient COVERAGE/COV_CORE configuration."
    }
    if ($PytestArgs -match '^(addopts=|-o=?addopts=|--override-ini=addopts=)') {
        throw "A full run cannot replace pytest addopts through --override-ini."
    }
    if ($PytestArgs -match '(^-c|^--config-file(?:=|$))') {
        throw "A full run uses this checkout's pyproject.toml, not a foreign pytest config."
    }
    if ($PytestArgs -match '^--(no-cov|cov-append|cov-config)(=|$)') {
        throw "A full run cannot disable coverage, append stale data or replace its coverage config."
    }
    if (@($PytestArgs | Where-Object { $_ -match '^--cov(?:=|$)' -and $_ -ne '--cov=hoh_control_plane' }).Count) {
        throw "A full run measures only --cov=hoh_control_plane."
    }
    $coverageGate = 80.0
    for ($index = 0; $index -lt $PytestArgs.Count; $index++) {
        $argument = $PytestArgs[$index]
        if ($argument -match '^--cov-fail-under(?:=(.*))?$') {
            $value = $Matches[1]
            if ($argument -eq '--cov-fail-under') {
                $index++
                if ($index -ge $PytestArgs.Count) { throw "Missing --cov-fail-under value." }
                $value = $PytestArgs[$index]
            }
            $threshold = 0.0
            if (-not [double]::TryParse($value, [System.Globalization.NumberStyles]::Float,
                [System.Globalization.CultureInfo]::InvariantCulture, [ref]$threshold) -or
                [double]::IsNaN($threshold) -or [double]::IsInfinity($threshold) -or
                $threshold -lt 80 -or $threshold -gt 100) {
                throw "A full run requires --cov-fail-under between 80 and 100."
            }
            $coverageGate = [Math]::Max($coverageGate, $threshold)
        }
    }
    $gateArgument = $coverageGate.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $PytestArgs = @($PytestArgs) + @('-c', (Join-Path $checkout 'pyproject.toml'), '--cov=hoh_control_plane', '--cov-branch',
        '--cov-report=term-missing', "--cov-fail-under=$gateArgument")
}

$env:PYTHONPATH = Join-Path $checkout "src"
# Child processes and pytest's own output stay UTF-8 whatever the console code page.
$env:PYTHONUTF8 = "1"
# The suite is dominated by Git subprocesses and runs about four times faster
# across workers (17.6 min serial vs 4.3 min with 10 workers on the dev host).
# Pass -n 0 to run serially, or your own -n. The default is 6, not auto: more
# workers grew the Windows pagefile on this host until the disk filled.
if (-not ($PytestArgs -match '^-n')) {
    $PytestArgs = @('-n', '6') + @($PytestArgs)
}

# Each run gets its own basetemp, <checkout-hash>-<pid>. pytest empties its
# basetemp at start, so one shared directory let a second run (another agent, or
# a worktree beside main) delete the first run's repositories mid-test. Keep it
# off the small system drive, outside any repository (a test checks a non-Git
# directory), and short: Git object paths under it must stay below MAX_PATH.
$tempRoot = $env:HOH_TEST_TEMP_ROOT
if (-not $tempRoot) { $tempRoot = "D:\.hoh-pytest" }
$sha = [System.Security.Cryptography.SHA256]::Create()
$digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($checkout.ToLowerInvariant()))
$checkoutHash = (($digest | Select-Object -First 4 | ForEach-Object { $_.ToString("x2") }) -join "")
$runName = "$checkoutHash-$PID"
$basetemp = $null
if (-not ($PytestArgs -match '^--basetemp')) {
    $basetemp = Join-Path $tempRoot $runName
    $PytestArgs = @("--basetemp=$basetemp") + @($PytestArgs)
}
$logDir = Join-Path $tempRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "$runName.log"
# Tee-Object creates no file when a native child is silent.
New-Item -ItemType File -Force -Path $log | Out-Null

Push-Location $checkout
try {
    # "Stop" plus redirected stderr could end this script on the first line a
    # worker wrote to stderr; xdist workers then flushed into a closed pipe
    # (OSError Errno 22). Inside the run nothing native may stop the script.
    $ErrorActionPreference = "Continue"
    & $python -m pytest @PytestArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    $summary = Get-Content -LiteralPath $log | Where-Object {
        $_ -match '(passed|failed|error|no tests ran)' -and $_ -match ' in [0-9.]+s'
    } | Select-Object -Last 1
    Write-Host ""
    Write-Host "== HoH test run: exit $code$(if ($fullRun) { ' (full suite, coverage gate)' } elseif ($fast) { ' (fast lane)' })"
    if ($summary) { Write-Host "== $($summary.Trim(' ', '='))" }
    $coverage = Get-Content -LiteralPath $log | Where-Object { $_ -match '^TOTAL\s' } | Select-Object -Last 1
    if ($coverage) { Write-Host "== coverage: $coverage" }
    Write-Host "== log: $log"
    if ($basetemp) {
        if ($code -eq 0) {
            Remove-Item -LiteralPath $basetemp -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "== kept for inspection: $basetemp"
        }
    }
    exit $code
} finally {
    Pop-Location
}
