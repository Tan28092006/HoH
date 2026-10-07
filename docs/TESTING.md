# Testing HoH

How the suite is run locally and in CI, which lanes exist, and what the rules
for test code are. The command that pins the right interpreter is
[`scripts/test.ps1`](../scripts/test.ps1); [`AGENTS.md`](../AGENTS.md) says
when to run it.

## Lanes

| Lane | Local command | CI | Coverage gate |
| --- | --- | --- | --- |
| Targeted | `scripts\test.ps1 tests\test_x.py [pytest args]` | no | no |
| Fast | `scripts\test.ps1 --fast` (`-m "not slow and not git"`) | every pull request | no |
| Full | `scripts\test.ps1` | every push to `main`, manual runs | `--cov-fail-under=80` |

The gate is not in `pyproject.toml` `addopts`: a targeted run that passes must
exit 0. `test.ps1` adds the coverage options only when the arguments select
nothing (no path, node id, `-k`, `-m`, `--lf` or `--deselect`).

Markers (`tests/conftest.py` assigns `slow` and `git` per module at collection):

- `git`: the module builds real Git repositories (detected from the calls it
  makes, including the `tests/helpers` setups that build one).
- `slow`: modules that dominate wall time, listed in `SLOW_MODULES`.
- `ui`: browser tests, skipped unless `--run-ui`.
- `production_git_timeout`: the test keeps the shipped 15 s Git budget. Every
  other test runs Git with 600 s, so a loaded machine does not fail it for
  being slow.

## What `test.ps1` does

- Pins `.venv-hoh` (or `HOH_TEST_PYTHON`) and puts this checkout's `src/` first.
- Sets `PYTHONUTF8=1`.
- Runs with `-n 6` unless you pass `-n`.
- Gives each run its own basetemp, `<HOH_TEST_TEMP_ROOT or D:\.hoh-pytest>\<checkout-hash>-<pid>`.
  pytest empties its basetemp at start, so a shared one let two concurrent
  runs delete each other's repositories. Keep the root path short: Git object
  paths under it must stay below Windows `MAX_PATH`.
- Removes the basetemp after a passing run and keeps it after a failure.
- Lets pytest finish even when it writes to stderr.
- Writes the whole output to `<root>\logs\<checkout-hash>-<pid>.log` and ends
  with a summary: exit code, pass/fail line, coverage total and log path.

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs Windows and
Ubuntu with Python 3.12 and 3.13 (`requires-python = ">=3.12"`). It uses no
secrets: the PostgreSQL suite skips without `HOH_TEST_POSTGRES_DSN` and the
browser tests skip without `--run-ui`.

## Rules for test code

- Every test runs with no `HOH_*` variable from the shell, except `HOH_TEST_*`.
  A test that needs one sets it with `monkeypatch`.
- A test module never imports another test module. A setup that two modules
  need lives in `tests/helpers/`.
- Do not prove that something did not happen by sleeping and then checking.
  Give the process or job something to report (a PID, a terminal status) and
  wait for that with a deadline.
- A gap whose fix belongs to another open branch is a strict `xfail` that says
  which branch owns it, collected in `tests/test_known_gaps.py`. Whoever merges
  the fix removes the test or the marker (and the file when it empties).
- Every reconciliation-barrier call site has a test in
  `tests/test_barrier_guards.py`; its inventory test fails on a new site without one.
