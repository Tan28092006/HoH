from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hoh_control_plane import qa_worker
from hoh_control_plane.domain import ConflictError, ValidationError


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _candidate_waiting_for_qa(service, actors, seeded, tmp_path: Path, test_command: list[str]):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(actors["planner"], "project-1", test_command, 120)
    ticket = seeded["ticket"]
    development = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    retry_path = worktree / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
        _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Retry implementation is ready for QA."],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    return ticket, candidate, qa_claim


def test_qa_runs_the_planner_registered_command_and_persists_evidence(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/checkout/retry.txt').read_text() == 'safe retry\\n'; print('passed')",
    ]
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    result = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert candidate["candidate"]["candidate_id"] == result["test_run"]["candidate_id"]
    assert result["test_run"]["exit_code"] == 0
    assert Path(result["test_run"]["report_path"]).is_file()
    assert context["test_runs"][0]["storage_ref"].startswith("evidence://local/")


def test_qa_queues_project_tests_then_polls_a_completed_durable_run(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The MCP-facing request must finish before the isolated test command runs."""
    test_command = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/checkout/retry.txt').is_file(); print('passed')",
    ]
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )

    assert queued["test_run"]["status"] == "QUEUED"
    assert queued["test_run"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert service.get_ticket_context(actors["qa"], ticket["ticket_id"])["test_runs"] == []

    qa_worker.main(
        [
            "--database",
            str(service.store.path),
            "--test-run-id",
            queued["test_run"]["test_run_id"],
        ]
    )
    polled = service.get_project_test_run(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        queued["test_run"]["test_run_id"],
    )

    assert polled["test_run"]["status"] == "COMPLETED"
    assert polled["test_run"]["exit_code"] == 0
    assert Path(polled["test_run"]["report_path"]).is_file()


def test_qa_cannot_queue_two_active_runs_for_one_frozen_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    service.queue_project_tests(actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"])

    with pytest.raises(ConflictError, match="active"):
        service.queue_project_tests(
            actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
        )


def test_qa_records_a_failing_registered_test_without_accepting_the_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [sys.executable, "-c", "raise SystemExit(7)"]
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    result = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert result["test_run"]["exit_code"] == 7
    assert context["ticket"]["status"] == "QA_IN_PROGRESS"
    assert context["test_runs"][0]["exit_code"] == 7


def test_qa_refuses_to_run_when_the_candidate_worktree_becomes_dirty(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [sys.executable, "-c", "print('should not execute')"]
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])
    worktree = Path(context["worktree"]["worktree_path"])
    (worktree / "untracked.txt").write_text("not frozen\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="clean"):
        service.run_project_tests(
            actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
        )


def test_test_command_requires_a_verified_project_repository(service, actors, seeded) -> None:
    with pytest.raises(ValidationError, match="repository"):
        service.configure_test_command(
            actors["planner"], "project-1", ["python", "-m", "pytest"], 120
        )
