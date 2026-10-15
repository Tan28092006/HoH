"""Exercise the real PowerShell wrapper; its child records args and exit status."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SHELLS = tuple(dict.fromkeys(filter(None, (shutil.which("powershell"), shutil.which("pwsh")))))


def invoke(tmp_path, arguments=(), *, child_exit=0, addopts="", extra_env=None, silent=False, shell=None):
    if POWERSHELL is None:
        pytest.fail("PowerShell is required to verify the supported test wrapper")
    capture = tmp_path / "capture.json"
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['HOH_TEST_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "if os.environ['HOH_TEST_SILENT'] != '1': print('argument-recorder', flush=True)\n"
        "sys.exit(int(os.environ['HOH_TEST_CHILD_EXIT']))\n", encoding="utf-8")
    stub = tmp_path / "python.ps1"
    stub.write_text("& $env:HOH_TEST_REAL_PYTHON $env:HOH_TEST_RECORDER @args\nexit $LASTEXITCODE\n", encoding="utf-8")
    environment = os.environ.copy()
    for key in list(environment):
        if key.upper().startswith(("COVERAGE", "COV_CORE")):
            environment.pop(key)
    environment.update(HOH_TEST_PYTHON=str(stub), HOH_TEST_REAL_PYTHON=sys.executable,
        HOH_TEST_RECORDER=str(recorder), HOH_TEST_CAPTURE=str(capture),
        HOH_TEST_CHILD_EXIT=str(child_exit), HOH_TEST_TEMP_ROOT=str(tmp_path / "run"),
        PYTEST_ADDOPTS=addopts, HOH_TEST_SILENT="1" if silent else "0")
    environment.update(extra_env or {})
    result = subprocess.run([shell or POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
        str(ROOT / "scripts/test.ps1"), *arguments], env=environment,
        capture_output=True, text=True, timeout=60)
    return result, json.loads(capture.read_text()) if capture.exists() else None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("arguments,targeted", [
    (["--cov-report=xml:report.py", "--no-cov"], False),
    (["--deselect=tests/test_api.py::test_health"], True),
    (["--deselect", "tests/test_api.py::test_health"], True),
])
def test_file_entrypoint_preserves_colon_option_tokens(tmp_path, shell, arguments, targeted):
    result, arguments_seen = invoke(tmp_path, arguments, shell=shell)
    if targeted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert all(argument in arguments_seen for argument in arguments)
        assert not any(argument.startswith("--cov") for argument in arguments_seen)
    else:
        assert result.returncode != 0
        assert "cannot disable coverage" in result.stderr
        assert arguments_seen is None


@pytest.mark.parametrize("arguments", [[], ["--cov-report=term"], ["--cov=hoh_control_plane"], ["--cov-fail-under=90"], ["--ff"], ["--failed-first"]])
def test_full_runner_keeps_canonical_source_branch_and_strong_gate(tmp_path, arguments):
    result, arguments_seen = invoke(tmp_path, arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--cov=hoh_control_plane" in arguments_seen
    assert "--cov-branch" in arguments_seen
    assert arguments_seen[arguments_seen.index("-c") + 1] == str(ROOT / "pyproject.toml")
    assert arguments_seen[-1] == ("--cov-fail-under=90" if "--cov-fail-under=90" in arguments else "--cov-fail-under=80")


@pytest.mark.parametrize("arguments", [["--cov-fail-under=0"], ["--cov-fail-under", "79"],
    ["--cov-fail-under=NaN"], ["--no-cov"], ["--cov-append"], ["--cov-config=other.ini"],
    ["--cov=other"], ["--cov"], ["-o", "addopts=--no-cov"],
    ["--override-ini", "addopts=--no-cov"], ["--override-ini=addopts=--no-cov"],
    ["--junitxml=report.py", "--no-cov"], ["--junitxml", "report.py", "--no-cov"],
    ["--cov-report=xml:report.py", "--no-cov"]])
def test_full_runner_rejects_coverage_bypass_before_starting_child(tmp_path, arguments):
    result, arguments_seen = invoke(tmp_path, arguments)
    assert result.returncode != 0
    assert arguments_seen is None


@pytest.mark.parametrize("config_flag", ["-c", "--config-file"])
def test_a_foreign_pytest_config_cannot_inject_no_coverage_into_a_full_run(tmp_path, config_flag):
    config = tmp_path / "foreign.ini"
    config.write_text("[pytest]\naddopts = --no-cov\n", encoding="utf-8")
    result, arguments_seen = invoke(tmp_path, [config_flag, str(config)])
    assert result.returncode != 0
    assert arguments_seen is None


def test_ambient_coverage_configuration_cannot_replace_the_full_policy(tmp_path):
    result, arguments_seen = invoke(tmp_path, extra_env={"COVERAGE_RCFILE": "foreign.ini"})
    assert result.returncode != 0
    assert arguments_seen is None


@pytest.mark.parametrize("arguments", [["--lf"], ["--last-failed"], ["-k", "test_health"],
    ["-m", "ui"], ["--deselect=tests/test_api.py::test_health"], ["tests/test_api.py"]])
def test_actual_selection_stays_targeted_without_a_full_coverage_claim(tmp_path, arguments):
    result, arguments_seen = invoke(tmp_path, arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(argument.startswith("--cov") for argument in arguments_seen)


def test_ambient_pytest_arguments_cannot_change_a_full_run(tmp_path):
    result, arguments_seen = invoke(tmp_path, addopts="-k nonexistent --no-cov")
    assert result.returncode != 0
    assert "PYTEST_ADDOPTS" in result.stderr
    assert arguments_seen is None


@pytest.mark.parametrize("exit_code", [1, 2, 5])
def test_failing_child_and_empty_collection_statuses_reach_the_caller(tmp_path, exit_code):
    result, arguments_seen = invoke(tmp_path, ["tests/test_api.py", "-n", "0"], child_exit=exit_code)
    assert result.returncode == exit_code
    assert arguments_seen[:2] == ["-m", "pytest"]
    assert not any(argument.startswith("--cov") for argument in arguments_seen)


def test_fast_lane_keeps_the_existing_expression_and_explicit_worker_count(tmp_path):
    result, arguments_seen = invoke(tmp_path, ["--fast", "-n", "2"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert arguments_seen[arguments_seen.index("pytest") + 2:][:2] == ["-m", "not slow and not git"]
    assert arguments_seen.count("-n") == 1
    assert arguments_seen[arguments_seen.index("-n") + 1] == "2"
    assert not any(argument.startswith("--cov") for argument in arguments_seen)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 5])
def test_silent_native_child_keeps_its_exit_and_creates_an_empty_log(tmp_path, exit_code):
    result, arguments_seen = invoke(tmp_path, ["tests/test_api.py", "-n", "0"], child_exit=exit_code, silent=True)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert arguments_seen[:2] == ["-m", "pytest"]
    logs = list((tmp_path / "run/logs").glob("*.log"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8-sig") == ""
