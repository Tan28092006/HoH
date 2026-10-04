
# The one supported way to run this checkout's tests.
#
# .venv-hoh carries an editable install of an older worktree, a bare `python`
# resolves to an msys interpreter without pytest, and the system Python lacks
# pytest-cov. Each of those fails differently, or worse, passes against the wrong
# source. This script pins the interpreter and puts this checkout's src/ first.
# tests/conftest.py refuses to run if the imported package is not this checkout's.

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
$env:PYTHONPATH = Join-Path $checkout "src"
# The suite is dominated by Git subprocesses and runs about four times faster
# across workers (17.6 min serial vs 4.3 min with 10 workers on the dev host).
# Pass -n 0 to run serially, or your own -n.
if (-not ($PytestArgs -match '^-n')) {
    $PytestArgs = @('-n', 'auto') + @($PytestArgs)
}
# Each full run creates hundreds of Git repositories. Keep them off the small
# system drive and outside any repository (a test checks a non-Git directory).
if (-not ($PytestArgs -match '^--basetemp')) {
    $PytestArgs = @('--basetemp=D:\.hoh-pytest') + @($PytestArgs)
}
Push-Location $checkout
try {
    & $python -m pytest @PytestArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
