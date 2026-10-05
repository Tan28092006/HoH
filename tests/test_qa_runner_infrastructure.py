"""Wave 7A: a test run that failed because of the host is not a QA rejection.

The runner classifies memory exhaustion, interrupted or broken pytest runs and
imports resolved outside the candidate worktree as infrastructure failures,
retries them once with half the xdist workers, and sizes ``-n auto`` from free
memory. Child processes are simulated: no suite runs here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hoh_control_plane import qa_runner
from hoh_control_plane.qa_runner import (
    INFRASTRUCTURE_REASONS,
    ProjectTestRunner,
    memory_sized_workers,
    with_xdist_workers,
    xdist_workers,
)
from hoh_control_plane.service.qa import _INFRASTRUCTURE_OUTCOMES

GB = 1024**3
PLENTY = {"available_physical_bytes": 64 * GB, "available_commit_bytes": 64 * GB}


@dataclass
class _Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    capture_errors: tuple = ()
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _child(**overrides) -> dict:
    return {
        "phase": "session_finished", "provenance_status": "verified",
        "test_counts": {"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 0},
        "resource_failures": 0, "foreign_import_errors": [], **overrides,
    }


def _fake_runs(monkeypatch, outcomes, resources=PLENTY):
    """Each call writes the next (returncode, child facts) and records its argv."""
    calls: list[list[str]] = []

    def run_managed(invocation, *, cwd, env, timeout):
        calls.append(invocation)
        returncode, child = outcomes[min(len(calls), len(outcomes)) - 1]
        probe_index = next(i for i, part in enumerate(invocation) if part.endswith("pytest_probe.py"))
        Path(invocation[probe_index + 1]).write_text(json.dumps(child), encoding="utf-8")
        return _Completed(returncode)

    monkeypatch.setattr(qa_runner, "run_managed", run_managed)
    monkeypatch.setattr(qa_runner, "resource_snapshot", lambda: dict(resources))
    return calls


def _run(tmp_path: Path, arguments: list[str]):
    runner = ProjectTestRunner(tmp_path / "evidence")
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return runner.execute(["python", "-m", "pytest", *arguments], str(worktree), 60)


def test_a_memory_error_is_infrastructure_and_retried_once_with_half_the_workers(
    tmp_path, monkeypatch
) -> None:
    calls = _fake_runs(monkeypatch, [(1, _child(resource_failures=1))] * 2)

    execution = _run(tmp_path, ["-q", "-n", "4"])

    assert execution.termination_reason == "resource_exhausted"
    assert execution.environment_manifest["classification"] == "infrastructure_failure"
    assert len(calls) == 2, "retried exactly once"
    assert xdist_workers(calls[0][calls[0].index("-q"):]) == "4"
    assert xdist_workers(calls[1][calls[1].index("-q"):]) == "2"
    assert execution.environment_manifest["retry_of"]["reason"] == "resource_exhausted"


def test_a_retry_that_passes_is_the_recorded_result(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(3221225773, _child()), (0, _child())])

    execution = _run(tmp_path, ["-n", "2"])

    assert len(calls) == 2
    assert execution.termination_reason == "passed"


def test_a_foreign_import_collection_error_is_infrastructure(tmp_path, monkeypatch) -> None:
    child = _child(
        test_counts={"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 1},
        foreign_import_errors=[{"nodeid": "tests/test_a.py", "path": "D:/HoH/src/x.py"}],
    )
    _fake_runs(monkeypatch, [(2, child)] * 2)

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "foreign_import"
    assert execution.termination_reason in _INFRASTRUCTURE_OUTCOMES


def test_the_candidates_own_collection_error_is_still_a_test_failure(
    tmp_path, monkeypatch
) -> None:
    child = _child(test_counts={"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 1})
    calls = _fake_runs(monkeypatch, [(2, child)])

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "test_failure"
    assert execution.environment_manifest["classification"] == "candidate_failure"
    assert len(calls) == 1


def test_pytest_exit_codes_two_three_and_four_are_infrastructure(tmp_path, monkeypatch) -> None:
    for code, reason in ((2, "pytest_interrupted"), (3, "pytest_internal_error"),
                         (4, "pytest_usage_error")):
        _fake_runs(monkeypatch, [(code, _child())] * 2)
        assert _run(tmp_path, []).termination_reason == reason
        assert reason in INFRASTRUCTURE_REASONS


def test_n_auto_is_sized_from_free_memory_and_recorded(tmp_path, monkeypatch) -> None:
    low = {"available_physical_bytes": 8 * GB, "available_commit_bytes": int(1.3 * GB)}
    calls = _fake_runs(monkeypatch, [(0, _child())], resources=low)

    execution = _run(tmp_path, ["-q", "-n", "auto"])

    assert calls[0][-2:] == ["-n", "2"]
    assert execution.environment_manifest["xdist_workers"] == {"requested": "auto", "effective": "2"}
    # The frozen command, and so its digest, is what was configured.
    assert execution.environment_manifest["command"][-1] == "auto"


def test_worker_sizing_and_rewrites() -> None:
    assert memory_sized_workers({"available_commit_bytes": 100}, cpu_count=8) == 1
    assert memory_sized_workers({"available_commit_bytes": 64 * GB,
                                 "available_physical_bytes": 64 * GB}, cpu_count=8) == 8
    assert with_xdist_workers(["-nauto", "-q"], 3) == ["-n3", "-q"]
    assert with_xdist_workers(["--numprocesses=auto"], 2) == ["--numprocesses=2"]
    assert xdist_workers(["-q"]) is None
