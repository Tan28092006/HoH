"""Real trusted observer runs, including reports transported by xdist."""
import sys

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

    def run(source, workers):
        (root / "test_sample.py").write_text(source, encoding="utf-8")
        command = [sys.executable, "-m", "pytest", "-p", "xdist.plugin",
                   "-n", str(workers), "-q", "--tb=short", str(root)]
        execution = ProjectTestRunner(tmp_path / "evidence").execute(command, str(root), 120,
                                                                   provenance_modules=[])
        child = execution.environment_manifest["child"]
        assert not execution.timed_out, execution.stderr
        assert not execution.capture_errors, execution.capture_errors
        assert child["phase"] == "session_finished", execution.stderr
        assert child["pytest_exit_status"] == execution.exit_code
        assert child["provenance_status"] == "not_applicable"
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


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
def test_printed_summary_cannot_supply_trusted_counts(observe, workers):
    execution, child = observe("def test_a():\n    print('999 passed, 77 failed, 55 skipped')\n", workers)
    assert child["test_counts"] == {"passed": 1, "failed": 0, "skipped": 0, "collection_errors": 0}
    assert execution.termination_reason == "passed"
