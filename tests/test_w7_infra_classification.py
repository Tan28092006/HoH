"""Wave 7A edge cases: the QA runner's infrastructure classification and retry.

Child pytest processes are simulated (``run_managed`` is replaced); memory is a
fake resource snapshot. Nothing here exhausts real memory or runs a suite.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hoh_control_plane import qa_runner
from hoh_control_plane.qa_runner import (
    INFRASTRUCTURE_REASONS,
    WORKER_MEMORY_BYTES,
    ProjectTestRunner,
    memory_sized_workers,
    with_xdist_workers,
    xdist_workers,
)
from hoh_control_plane.service.qa import _INFRASTRUCTURE_OUTCOMES

GB = 1024**3
PLENTY = {"available_physical_bytes": 64 * GB, "available_commit_bytes": 64 * GB}
OOM = 0xC000012D


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
        "test_counts": {"passed": 1, "failed": 0, "skipped": 0, "collection_errors": 0},
        "resource_failures": 0, "foreign_import_errors": [], **overrides,
    }


def _collection_errors(count: int = 1, **overrides) -> dict:
    return _child(
        test_counts={"passed": 0, "failed": 0, "skipped": 0, "collection_errors": count},
        **overrides,
    )


def _fake_runs(monkeypatch, outcomes, resources=PLENTY):
    """Each call answers the next outcome: (returncode, child facts[, stdout, stderr])."""
    calls: list[list[str]] = []

    def run_managed(invocation, *, cwd, env, timeout):
        calls.append(list(invocation))
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        returncode, child = outcome[0], outcome[1]
        stdout = outcome[2] if len(outcome) > 2 else ""
        stderr = outcome[3] if len(outcome) > 3 else ""
        probe_index = next(
            i for i, part in enumerate(invocation) if part.endswith("pytest_probe.py")
        )
        Path(invocation[probe_index + 1]).write_text(json.dumps(child), encoding="utf-8")
        return _Completed(returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(qa_runner, "run_managed", run_managed)
    monkeypatch.setattr(qa_runner, "resource_snapshot", lambda: dict(resources))
    return calls


def _run(tmp_path: Path, arguments: list[str]):
    runner = ProjectTestRunner(tmp_path / "evidence")
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return runner.execute(["python", "-m", "pytest", *arguments], str(worktree), 60)


def _workers(call: list[str]) -> str | None:
    return xdist_workers(call[call.index("pytest") + 1:] if "pytest" in call else call)


# --------------------------------------------------------------- memory


@pytest.mark.parametrize("code", [OOM, OOM - 2**32], ids=["unsigned", "signed"])
def test_commit_limit_exit_code_is_resource_exhausted_in_either_sign(
    tmp_path, monkeypatch, code
) -> None:
    calls = _fake_runs(monkeypatch, [(code, _child())] * 2)

    execution = _run(tmp_path, ["-n", "4"])

    assert execution.termination_reason == "resource_exhausted"
    assert execution.environment_manifest["classification"] == "infrastructure_failure"
    assert len(calls) == 2


@pytest.mark.parametrize("marker", ["MemoryError", "WinError 1455", "paging file is too small"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_memory_markers_in_either_stream_are_resource_exhausted(
    tmp_path, monkeypatch, marker, stream
) -> None:
    text = f"E   {marker}: boom\n"
    out = (text, "") if stream == "stdout" else ("", text)
    _fake_runs(monkeypatch, [(1, _child(), *out)] * 2)

    assert _run(tmp_path, []).termination_reason == "resource_exhausted"


def test_a_memory_marker_in_a_passing_run_is_not_a_failure(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(0, _child(), "test_memoryerror_handling PASSED MemoryError")])

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "passed"
    assert execution.environment_manifest["classification"] == "passed"
    assert len(calls) == 1


def test_a_memory_marker_older_than_the_scanned_tail_is_not_seen(tmp_path, monkeypatch) -> None:
    # Only the last 20,000 characters are scanned: a MemoryError printed early
    # and followed by a long ordinary failure log is the candidate's failure.
    stdout = "MemoryError early\n" + ("x" * 20_001)
    _fake_runs(monkeypatch, [(1, _child(), stdout)])

    assert _run(tmp_path, []).termination_reason == "test_failure"


def test_resource_failures_reported_by_the_probe_are_resource_exhausted(
    tmp_path, monkeypatch
) -> None:
    calls = _fake_runs(monkeypatch, [(1, _child(resource_failures=2))] * 2)

    assert _run(tmp_path, []).termination_reason == "resource_exhausted"
    assert len(calls) == 2


# ------------------------------------------------------------ exit codes


def test_exit_two_without_collection_errors_is_an_interrupted_run(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(2, _child())] * 2)

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "pytest_interrupted"
    assert len(calls) == 2


def test_exit_two_with_the_candidates_collection_errors_is_its_failure(
    tmp_path, monkeypatch
) -> None:
    calls = _fake_runs(monkeypatch, [(2, _collection_errors(3))])

    execution = _run(tmp_path, ["-n", "4"])

    assert execution.termination_reason == "test_failure"
    assert execution.environment_manifest["classification"] == "candidate_failure"
    assert "retry_of" not in execution.environment_manifest
    assert len(calls) == 1


def test_exit_two_with_a_foreign_import_is_infrastructure_even_with_collection_errors(
    tmp_path, monkeypatch
) -> None:
    child = _collection_errors(
        1, foreign_import_errors=[{"nodeid": "tests/test_a.py", "path": "C:/other/x.py"}]
    )
    calls = _fake_runs(monkeypatch, [(2, child)] * 2)

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "foreign_import"
    assert execution.environment_manifest["classification"] == "infrastructure_failure"
    assert execution.environment_manifest["retry_of"]["reason"] == "foreign_import"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("code", "reason"),
    [(3, "pytest_internal_error"), (4, "pytest_usage_error"), (5, "test_configuration_error")],
)
def test_other_pytest_exit_codes_are_infrastructure_and_retried_once(
    tmp_path, monkeypatch, code, reason
) -> None:
    calls = _fake_runs(monkeypatch, [(code, _child())] * 3)

    execution = _run(tmp_path, [])

    assert execution.termination_reason == reason
    assert reason in INFRASTRUCTURE_REASONS
    assert reason in _INFRASTRUCTURE_OUTCOMES
    assert len(calls) == 2, "one retry, never two"


def test_exit_one_is_the_candidates_failure_and_not_retried(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(1, _child())])

    execution = _run(tmp_path, ["-n", "4"])

    assert execution.termination_reason == "test_failure"
    assert len(calls) == 1


def test_a_provenance_mismatch_is_infrastructure_but_not_retried(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(1, _child(provenance_status="mismatch"))])

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "artifact_mismatch"
    assert execution.artifact_mismatch is True
    assert execution.environment_manifest["classification"] == "infrastructure_failure"
    assert len(calls) == 1


def test_memory_takes_precedence_over_a_foreign_import(tmp_path, monkeypatch) -> None:
    child = _collection_errors(1, foreign_import_errors=[{"nodeid": "t", "path": "/x.py"}])
    _fake_runs(monkeypatch, [(OOM, child)] * 2)

    assert _run(tmp_path, []).termination_reason == "resource_exhausted"


# ----------------------------------------------------------------- retry


@pytest.mark.parametrize(
    ("requested", "retried"),
    [(["-n", "5"], "2"), (["-n", "2"], "1"), (["-n", "1"], "1"), (["-n8"], "4"),
     (["--numprocesses=6"], "3"), (["--numprocesses", "3"], "1")],
)
def test_the_retry_halves_the_workers_and_never_goes_below_one(
    tmp_path, monkeypatch, requested, retried
) -> None:
    calls = _fake_runs(monkeypatch, [(3, _child())] * 2)

    execution = _run(tmp_path, ["-q", *requested])

    assert len(calls) == 2
    assert _workers(calls[1]) == retried
    retry = execution.environment_manifest["retry_of"]
    assert retry["reason"] == "pytest_internal_error"
    assert retry["exit_code"] == 3
    assert retry["xdist_workers"] == _workers(calls[0])
    assert execution.environment_manifest["xdist_workers"]["effective"] == retried


def test_a_retry_without_xdist_reruns_the_same_arguments(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(4, _child())] * 2)

    execution = _run(tmp_path, ["-q", "-x"])

    assert calls[0][-2:] == calls[1][-2:] == ["-q", "-x"]
    assert _workers(calls[1]) is None
    assert execution.environment_manifest["retry_of"]["xdist_workers"] is None


def test_a_retry_that_fails_differently_reports_the_second_reason(tmp_path, monkeypatch) -> None:
    calls = _fake_runs(monkeypatch, [(OOM, _child()), (1, _child())])

    execution = _run(tmp_path, ["-n", "4"])

    assert len(calls) == 2
    assert execution.termination_reason == "test_failure"
    assert execution.environment_manifest["retry_of"]["reason"] == "resource_exhausted"


def test_a_retry_that_passes_is_recorded_as_passed_with_its_origin(tmp_path, monkeypatch) -> None:
    _fake_runs(monkeypatch, [(2, _child()), (0, _child())])

    execution = _run(tmp_path, [])

    assert execution.termination_reason == "passed"
    assert execution.environment_manifest["classification"] == "passed"
    assert execution.environment_manifest["retry_of"]["reason"] == "pytest_interrupted"


def test_n_auto_is_sized_then_halved_on_retry(tmp_path, monkeypatch) -> None:
    resources = {"available_physical_bytes": 64 * GB,
                 "available_commit_bytes": 4 * WORKER_MEMORY_BYTES}
    monkeypatch.setattr(qa_runner.os, "cpu_count", lambda: 16)
    calls = _fake_runs(monkeypatch, [(OOM, _child()), (0, _child())], resources=resources)

    execution = _run(tmp_path, ["-n", "auto"])

    assert _workers(calls[0]) == "4"
    assert _workers(calls[1]) == "2"
    assert execution.environment_manifest["command"][-1] == "auto"


def test_the_frozen_command_digest_is_unchanged_by_auto_sizing(tmp_path, monkeypatch) -> None:
    _fake_runs(monkeypatch, [(0, _child())],
               resources={"available_commit_bytes": WORKER_MEMORY_BYTES,
                          "available_physical_bytes": WORKER_MEMORY_BYTES})
    sized = _run(tmp_path, ["-n", "auto"])
    _fake_runs(monkeypatch, [(0, _child())])
    plenty = _run(tmp_path, ["-n", "auto"])

    assert sized.environment_manifest["command_digest"] == plenty.environment_manifest["command_digest"]
    assert sized.environment_manifest["xdist_workers"]["effective"] == "1"


# ----------------------------------------------------------- sizing (pure)


@pytest.mark.parametrize(
    ("free", "expected"),
    [(0, 1), (WORKER_MEMORY_BYTES - 1, 1), (WORKER_MEMORY_BYTES, 1),
     (2 * WORKER_MEMORY_BYTES - 1, 1), (2 * WORKER_MEMORY_BYTES, 2),
     (7 * WORKER_MEMORY_BYTES, 7), (100 * WORKER_MEMORY_BYTES, 8)],
)
def test_memory_sized_workers_boundaries(free, expected) -> None:
    resources = {"available_commit_bytes": free, "available_physical_bytes": 64 * GB}
    assert memory_sized_workers(resources, cpu_count=8) == expected


def test_the_smaller_of_commit_and_physical_memory_wins() -> None:
    resources = {"available_commit_bytes": 64 * GB,
                 "available_physical_bytes": 3 * WORKER_MEMORY_BYTES}
    assert memory_sized_workers(resources, cpu_count=32) == 3


def test_without_memory_facts_the_cpu_count_is_used(monkeypatch) -> None:
    assert memory_sized_workers({}, cpu_count=6) == 6
    # Non-integer readings are not facts.
    assert memory_sized_workers({"available_commit_bytes": 1.5 * GB,
                                 "available_physical_bytes": None}, cpu_count=6) == 6
    monkeypatch.setattr(qa_runner.os, "cpu_count", lambda: None)
    assert memory_sized_workers({}) == 1
    assert memory_sized_workers({"available_commit_bytes": 64 * GB}) == 1


def test_a_single_cpu_caps_at_one() -> None:
    assert memory_sized_workers(PLENTY, cpu_count=1) == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [(["-n", "4"], "4"), (["-n4"], "4"), (["-nauto"], "auto"), (["--numprocesses=3"], "3"),
     (["--numprocesses", "auto"], "auto"), (["-q", "-n"], None), (["--numprocesses"], None),
     ([], None), (["-q", "--no-cov"], None)],
)
def test_xdist_workers_parses_every_spelling(arguments, expected) -> None:
    assert xdist_workers(arguments) == expected


def test_with_xdist_workers_rewrites_only_the_worker_count() -> None:
    assert with_xdist_workers(["-q", "-n", "auto", "-x"], 2) == ["-q", "-n", "2", "-x"]
    assert with_xdist_workers(["--numprocesses", "8"], 1) == ["--numprocesses", "1"]
    # A dangling -n is left alone rather than given a value it never had.
    assert with_xdist_workers(["-q", "-n"], 2) == ["-q", "-n"]
    assert with_xdist_workers(["-q"], 2) == ["-q"]
