"""Real trusted observer runs, including reports transported by xdist."""
import sys
from pathlib import Path

import pytest

from hoh_control_plane.qa_runner import ProjectTestRunner


@pytest.fixture
def observe(tmp_path, monkeypatch):
    # Admission capacity is not the subject of these disposable tiny suites.
    monkeypatch.setattr("hoh_control_plane.qa_runner.resource_snapshot", lambda: {
        "available_physical_bytes": 4 * 1024**3,
        "available_commit_bytes": 4 * 1024**3,
    })
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    root = tmp_path / "candidate"
    root.mkdir()

    def run(source, workers, *, flags=(), modules=(), plugins=()):
        (root / "test_sample.py").write_text(source, encoding="utf-8")
        command = [sys.executable, *flags, "-m", "pytest", "-p", "xdist.plugin",
                   "-n", str(workers), "-q", "--tb=short", str(root)]
        runner = ProjectTestRunner(tmp_path / "evidence")
        if plugins:
            # Exercise worker plugin preservation after the runner's environment sanitization.
            original_environment = runner._build_environment
            monkeypatch.setattr(runner, "_build_environment", lambda path: {
                **original_environment(path), "PYTEST_PLUGINS": ",".join(plugins),
            })
        execution = runner.execute(command, str(root), 120, provenance_modules=list(modules))
        child = execution.environment_manifest["child"]
        assert not execution.timed_out, execution.stderr
        assert not execution.capture_errors, execution.capture_errors
        assert child["phase"] == "session_finished", execution.stderr
        assert child["pytest_exit_status"] == execution.exit_code
        assert child["provenance_status"] == ("verified" if modules else "not_applicable")
        return execution, child

    return run


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
@pytest.mark.parametrize("source,counts,exit_code,reason", [
    ("def test_a(): pass\ndef test_b(): pass\n", (2, 0, 0, 0), 0, "passed"),
    ("import pytest\ndef test_a(): pass\ndef test_b(): assert False\n"
     "@pytest.mark.skip(reason='expected skip')\ndef test_c(): pass\n",
     (1, 1, 1, 0), 1, "test_failure"),
    ("import pytest\n@pytest.fixture\ndef broken(): assert False\n"
     "def test_a(broken): pass\n", (0, 1, 0, 0), 1, "test_failure"),
    ("import pytest\n@pytest.mark.skip(reason='all skipped')\ndef test_a(): pass\n",
     (0, 0, 1, 0), 0, "no_tests_passed"),
    ("", (0, 0, 0, 0), 5, "test_configuration_error"),
    ("raise ValueError('candidate collection failure')\n", (0, 0, 0, 1),
     None, "test_failure"),
])
def test_actual_serial_and_parallel_counts(observe, workers, source, counts, exit_code, reason):
    execution, child = observe(source, workers)
    assert child["test_counts"] == dict(zip(
        ("passed", "failed", "skipped", "collection_errors"), counts))
    # xdist returns 1 for collection failures, serial pytest returns 2.
    expected_exit = (1 if workers else 2) if exit_code is None else exit_code
    assert execution.exit_code == expected_exit
    assert execution.termination_reason == reason
    if counts[2]:
        assert len(child["skipped_tests"]) == counts[2]
        assert child["skipped_tests"][0]["nodeid"].endswith("test_a" if counts[0] == 0 else "test_c")


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
def test_parallel_memory_failure_remains_infrastructure(observe, workers):
    execution, child = observe("def test_a(): raise MemoryError('allocation failed')\n", workers)
    assert child["test_counts"] == {"passed": 0, "failed": 1, "skipped": 0, "collection_errors": 0}
    assert child["resource_failures"] == 1
    assert execution.termination_reason == "resource_exhausted"
    assert execution.environment_manifest["classification"] == "infrastructure_failure"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial-retry", "xdist"])
@pytest.mark.parametrize("base,resource", [("'Memory' + 'Error'", True), ("'RuntimeError'", False)])
def test_neutral_exception_ancestry_survives_worker_transport(observe, workers, base, resource):
    # Split the builtin name so printed source cannot trigger stderr heuristics.
    source = ("import builtins\n"
              f"AllocationFailure = type('AllocationFailure', (getattr(builtins, {base}),), {{}})\n"
              "def test_a(): raise AllocationFailure('neutral failure')\n")
    execution, child = observe(source, workers)
    assert child["resource_failures"] == int(resource)
    assert child["test_counts"] == {"passed": 0, "failed": 1, "skipped": 0, "collection_errors": 0}
    assert execution.termination_reason == ("resource_exhausted" if resource else "test_failure")
    assert execution.environment_manifest["classification"] == (
        "infrastructure_failure" if resource else "candidate_failure")
    if resource:
        assert execution.environment_manifest["retry_of"]["reason"] == "resource_exhausted"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
@pytest.mark.parametrize("flags,modules", [(("-I",), ()), (("-B",), ("test_sample",))])
def test_worker_observer_preserves_flags_and_provenance(observe, workers, flags, modules):
    execution, child = observe("def test_a(): pass\n", workers, flags=flags, modules=modules)
    assert execution.termination_reason == "passed"
    assert child["isolated"] == ("-I" in flags)
    assert execution.environment_manifest["instrumentation"]["python_flags"] == list(flags)
    if modules:
        source = Path(child["source_modules"]["test_sample"]["path"])
        assert source.name == "test_sample.py" and source.parent.name == "candidate"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
def test_printed_summary_cannot_supply_trusted_counts(observe, workers):
    execution, child = observe("def test_a():\n    print('999 passed, 77 failed, 55 skipped')\n", workers)
    assert child["test_counts"] == {"passed": 1, "failed": 0, "skipped": 0, "collection_errors": 0}
    assert execution.termination_reason == "passed"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
@pytest.mark.parametrize("resource", [False, True], ids=["passed", "resource-failure"])
def test_nested_pytest_preserves_user_plugins_and_worker_observation(observe, workers, resource):
    source = (
        "import builtins, os, subprocess, sys\n"
        "def test_nested(tmp_path):\n"
        "    (tmp_path / 'test_child.py').write_text('def test_child(): assert True\\n')\n"
        "    result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', "
        "'-p', 'no:cacheprovider', 'test_child.py'], cwd=tmp_path, "
        "capture_output=True, text=True, timeout=60)\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
        "    assert os.environ['PYTEST_PLUGINS'] == 'pytest_cov'\n"
    )
    if resource:
        source += (
            "    AllocationFailure = type('AllocationFailure', "
            "(getattr(builtins, 'Memory' + 'Error'),), {})\n"
            "    raise AllocationFailure('neutral failure')\n"
        )
    execution, child = observe(source, workers, flags=("-B",), modules=("test_sample",), plugins=("pytest_cov",))
    assert child["resource_failures"] == int(resource), execution.stdout + execution.stderr
    assert child["test_counts"] == {
        "passed": int(not resource), "failed": int(resource), "skipped": 0, "collection_errors": 0,
    }, execution.stdout + execution.stderr
    assert execution.termination_reason == ("resource_exhausted" if resource else "passed")
