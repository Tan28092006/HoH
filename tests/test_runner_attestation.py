"""The test process must prove its own imports and environment after pytest setup."""
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from hoh_control_plane.qa_runner import ProjectTestRunner


def project(tmp_path, test="assert sample.value == 7", pythonpath="src"):
    root = tmp_path / "candidate"
    (root / "src/sample").mkdir(parents=True)
    (root / "src/sample/__init__.py").write_text("value = 7\n")
    (root / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["src/sample"]\n'
        '[tool.pytest.ini_options]\npythonpath = ' + json.dumps([pythonpath]) + '\n'
    )
    (root / "test_sample.py").write_text("import sample\ndef test_value():\n    " + test + "\n")
    return root


def test_isolated_pytest_attests_source_after_framework_pythonpath(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.setenv("HOH_MCP_TOKENS_JSON", "never-retain-this-secret")
    command = [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    result = ProjectTestRunner(tmp_path / "evidence").execute(command, str(root), 30)
    assert result.exit_code == 0, result.stderr
    assert result.provenance_status == "verified"
    manifest = result.environment_manifest
    assert manifest["command"] == command
    assert Path(manifest["child"]["executable"]).resolve() == Path(sys.executable).resolve()
    assert manifest["child"]["python_version"]
    assert manifest["child"]["dependencies"]["pytest"]
    assert Path(result.resolved_source).is_relative_to(root)
    assert manifest["child"]["phase"] == "session_finished"
    assert "never-retain-this-secret" not in json.dumps(manifest)


def test_pytest_outside_candidate_import_fails_closed_before_tests(tmp_path):
    other = tmp_path / "editable-main"
    (other / "sample").mkdir(parents=True)
    (other / "sample/__init__.py").write_text("value = 7\n")
    root = project(tmp_path, test="Path('executed').touch()", pythonpath=str(other))
    result = ProjectTestRunner(tmp_path / "evidence").execute(
        [sys.executable, "-I", "-m", "pytest", "-q"], str(root), 30)
    assert result.artifact_mismatch
    assert result.provenance_status == "mismatch"
    assert not (root / "executed").exists()


@pytest.mark.parametrize("body,expected", [
    ("assert False", "test_failure"),
    ("raise MemoryError('allocation refused')", "resource_exhausted"),
])
def test_structured_pytest_failure_distinguishes_allocation_from_assertion(tmp_path, body, expected):
    root = project(tmp_path, test=body)
    result = ProjectTestRunner(tmp_path / "evidence").execute(
        [sys.executable, "-I", "-m", "pytest", "-q"], str(root), 30)
    assert result.termination_reason == expected
    assert result.environment_manifest["child"]["test_counts"]["failed"] == 1


def test_low_memory_admission_does_not_launch_candidate(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.setattr("hoh_control_plane.qa_runner.resource_snapshot", lambda: {
        "available_physical_bytes": 0, "available_commit_bytes": 0,
    }, raising=False)
    result = ProjectTestRunner(tmp_path / "evidence").execute(
        [sys.executable, "-I", "-m", "pytest", "-q"], str(root), 30)
    assert result.exit_code is None
    assert result.termination_reason == "resource_unready"


def test_managed_output_reports_truncation_as_data(tmp_path):
    from hoh_control_plane.managed_process import run_managed
    result = run_managed([sys.executable, "-c", "print('x' * 1000)"],
                         cwd=tmp_path, timeout=20, output_limit=64)
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False
    assert result.capture_errors == ()


def test_pytest_cannot_pass_after_mutating_tracked_candidate_source(tmp_path):
    import subprocess
    root = project(tmp_path, test="__import__('pathlib').Path('src/sample/__init__.py').write_text('value = 8')")
    for args in [("init",), ("add", "."), ("-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-m", "candidate")]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    result = ProjectTestRunner(tmp_path / "evidence").execute(
        [sys.executable, "-I", "-m", "pytest", "-q"], str(root), 30)
    assert result.termination_reason == "artifact_mismatch"


@pytest.mark.parametrize("code,truncated,errors,reason", [
    (0xC0000005, False, (), "native_termination"),
    (0, True, (), "output_truncated"),
    (0, False, ("OSError",), "capture_failure"),
])
def test_non_pytest_commands_retain_process_failure_classification(tmp_path, monkeypatch, code, truncated, errors, reason):
    completed = SimpleNamespace(returncode=code, timed_out=False, cancelled=False,
        stdout="", stderr="", stdout_truncated=truncated, stderr_truncated=False, capture_errors=errors)
    monkeypatch.setattr("hoh_control_plane.qa_runner.run_managed", lambda *a, **kw: completed)
    result = ProjectTestRunner(tmp_path).execute(["node", "test.js"], str(tmp_path), 30)
    assert result.termination_reason == reason
    assert result.capture_errors == errors
    assert result.stdout_truncated == truncated
