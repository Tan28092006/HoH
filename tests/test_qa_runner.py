from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hoh_control_plane.domain import ValidationError


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
    (worktree / "retry.txt").write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "retry.txt")
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
        "from pathlib import Path; assert Path('retry.txt').read_text() == 'safe retry\\n'; print('passed')",
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
