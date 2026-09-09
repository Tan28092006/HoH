from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


def test_developer_worktree_binds_a_candidate_to_its_ticket_and_branch(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    configured = service.configure_repository(
        actors["planner"], "project-1", str(repository)
    )
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    retry_path = worktree / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")
    candidate_commit = _git(worktree, "rev-parse", "HEAD")

    submitted = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        candidate_commit,
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Retry implementation is committed in the assigned worktree."],
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert configured["repository_path"] == str(repository.resolve())
    assert prepared["worktree"]["agent_id"] == "dev-01"
    assert worktree.is_dir()
    assert submitted["candidate"]["commit_sha"] == candidate_commit
    assert submitted["candidate"]["changed_paths"] == ["src/checkout/retry.txt"]
    assert context["worktree"]["branch_ref"] == prepared["worktree"]["branch_ref"]
    assert context["candidate"]["changed_paths"] == ["src/checkout/retry.txt"]


def test_candidate_is_rejected_when_actual_diff_exceeds_declared_write_scope(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "README.md").write_text("# Changed outside ticket scope\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "Change unrelated documentation")

    with pytest.raises(ValidationError, match="outside ticket write scope"):
        service.submit_candidate(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
            _git(worktree, "rev-parse", "HEAD"),
            prepared["worktree"]["base_commit"],
            prepared["worktree"]["branch_ref"],
            ["This deliberately changes an unrelated file."],
        )


def test_configured_repository_rejects_candidate_from_another_branch(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "retry.txt").write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")

    with pytest.raises(ValidationError, match="branch"):
        service.submit_candidate(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
            _git(worktree, "rev-parse", "HEAD"),
            prepared["worktree"]["base_commit"],
            "hoh/project-1/DEV-142/someone-else",
            ["This must be rejected."],
        )


def test_planner_cannot_configure_a_non_git_directory(service, actors, tmp_path: Path) -> None:
    non_repository = tmp_path / "not-a-repository"
    non_repository.mkdir()

    with pytest.raises(ValidationError, match="Git repository"):
        service.configure_repository(actors["planner"], "project-1", str(non_repository))


def test_planner_cannot_change_repository_after_a_ticket_has_a_worktree(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    replacement_repository = _repository(tmp_path / "replacement")
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )

    with pytest.raises(ConflictError, match="cannot change"):
        service.configure_repository(
            actors["planner"], "project-1", str(replacement_repository)
        )
