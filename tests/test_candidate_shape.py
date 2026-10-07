"""A candidate is exactly one ordinary commit on its frozen base (review F2).

AGENTS.md states the invariant; ``_require_single_commit_iteration`` enforces it.
Until now no test submitted the two shapes it exists to refuse: a range of two
commits, which hides an iteration boundary from Planner, and a merge commit,
which imports a side history nobody reviewed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from helpers.barrier import keep_open_tickets
from helpers.integration import _git, _repository

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager

ONE_COMMIT = "exactly one non-merge commit"


def _commit(tree: Path, relative: str, text: str, message: str) -> str:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(tree, "add", relative)
    _git(tree, "commit", "-m", message)
    return _git(tree, "rev-parse", "HEAD")


def _assigned_worktree(service, actors, tmp_path: Path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    keep_open_tickets(service, actors["planner"])
    with service.store.connection() as connection:
        ticket = service._ticket(connection, "DEV-142")
    claim = service.claim_ticket(actors["developer"], "DEV-142", ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"]
    )["worktree"]
    return claim, prepared, Path(prepared["worktree_path"])


def _two_commits(worktree: Path) -> str:
    _commit(worktree, "src/checkout/retry.txt", "first\n", "First step")
    return _commit(worktree, "src/checkout/retry.txt", "second\n", "Second step")


def _merge_commit(worktree: Path, base: str) -> str:
    branch = _git(worktree, "branch", "--show-current")
    _git(worktree, "checkout", "-q", "-b", "side", base)
    _commit(worktree, "src/checkout/side.txt", "side\n", "Side history")
    _git(worktree, "checkout", "-q", branch)
    _commit(worktree, "src/checkout/retry.txt", "retry\n", "Main line")
    _git(worktree, "merge", "-q", "--no-ff", "--no-edit", "side")
    merge = _git(worktree, "rev-parse", "HEAD")
    assert len(_git(worktree, "rev-list", "--parents", "-n", "1", merge).split()) == 3
    return merge


def _state(service) -> tuple[str, int]:
    with service.store.connection() as connection:
        status = service._ticket(connection, "DEV-142")["status"]
        candidates = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    return status, candidates


@pytest.mark.parametrize("shape", ["two_commits", "merge_commit"])
def test_submit_refuses_a_candidate_that_is_not_one_ordinary_commit(
    service, actors, seeded, tmp_path, shape
) -> None:
    claim, prepared, worktree = _assigned_worktree(service, actors, tmp_path)
    head = _two_commits(worktree) if shape == "two_commits" else _merge_commit(
        worktree, prepared["base_commit"])

    with pytest.raises(ValidationError, match=ONE_COMMIT):
        service.submit_candidate(
            actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
            head, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
            context_snapshot_id=service.get_ticket_context(
                actors["developer"], "DEV-142")["context_snapshot_id"],
        )

    assert _state(service) == ("DEV_IN_PROGRESS", 0)


@pytest.mark.parametrize("shape", ["two_commits", "merge_commit"])
def test_the_submission_worker_fails_the_job_for_either_shape(
    service, actors, seeded, tmp_path, shape
) -> None:
    claim, prepared, worktree = _assigned_worktree(service, actors, tmp_path)
    head = _two_commits(worktree) if shape == "two_commits" else _merge_commit(
        worktree, prepared["base_commit"])
    queued = service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        head, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )

    job = service.execute_queued_candidate_submission(queued["submission"]["submission_id"])

    assert job["status"] == "FAILED"
    assert ONE_COMMIT in job["error_message"]
    assert _state(service) == ("DEV_IN_PROGRESS", 0)


# --- the agent-workspace path, straight against the repository -----------------

def _pushed(tmp_path: Path):
    repository = _repository(tmp_path)
    return GitWorktreeManager(tmp_path / "worktrees"), repository, _git(repository, "rev-parse", "HEAD")


def test_a_pushed_two_commit_range_is_refused(tmp_path) -> None:
    manager, repository, base = _pushed(tmp_path)
    head = _two_commits(repository)
    with pytest.raises(ValidationError, match=ONE_COMMIT):
        manager.verify_pushed_candidate(
            repository_path=str(repository), candidate_commit=head, submitted_base_commit=base)


def test_a_pushed_merge_commit_is_refused_even_when_one_parent_is_the_base(tmp_path) -> None:
    manager, repository, base = _pushed(tmp_path)
    _git(repository, "checkout", "-q", "-b", "side")
    _commit(repository, "src/side.txt", "side\n", "Side history")
    _git(repository, "checkout", "-q", "main")
    _git(repository, "merge", "-q", "--no-ff", "--no-edit", "side")
    merge = _git(repository, "rev-parse", "HEAD")
    assert base in _git(repository, "rev-list", "--parents", "-n", "1", merge).split()[1:]

    with pytest.raises(ValidationError, match=ONE_COMMIT):
        manager.verify_pushed_candidate(
            repository_path=str(repository), candidate_commit=merge, submitted_base_commit=base)


def test_a_rework_iteration_may_continue_the_assessed_candidate(tmp_path) -> None:
    manager, repository, base = _pushed(tmp_path)
    assessed = _commit(repository, "src/retry.txt", "first\n", "Assessed iteration")
    rework = _commit(repository, "src/retry.txt", "second\n", "Rework iteration")

    accepted = manager.verify_pushed_candidate(
        repository_path=str(repository), candidate_commit=rework, submitted_base_commit=base,
        expected_iteration_parent=assessed)

    assert accepted == (rework, base)
    # Without the assessed candidate as an allowed parent it is a two-commit range.
    with pytest.raises(ValidationError, match=ONE_COMMIT):
        manager.verify_pushed_candidate(
            repository_path=str(repository), candidate_commit=rework, submitted_base_commit=base)


def test_a_candidate_that_skips_the_assessed_iteration_is_refused(tmp_path) -> None:
    manager, repository, base = _pushed(tmp_path)
    assessed = _commit(repository, "src/retry.txt", "first\n", "Assessed iteration")
    _commit(repository, "src/retry.txt", "second\n", "Unassessed step")
    third = _commit(repository, "src/retry.txt", "third\n", "Rework iteration")

    with pytest.raises(ValidationError, match=ONE_COMMIT):
        manager.verify_pushed_candidate(
            repository_path=str(repository), candidate_commit=third, submitted_base_commit=base,
            expected_iteration_parent=assessed)
