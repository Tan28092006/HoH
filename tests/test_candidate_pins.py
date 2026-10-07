"""Frozen candidates are pinned at refs/hoh/candidates/<id> (wave 8E).

Without a ref of its own, a candidate's commit lives only on a developer branch:
a force-push, fetch --prune and gc can drop the commit a BLOCKED ticket's
evidence names. The pin is created at submission and removed only once the
ticket has been DONE for the retention period.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.git_worktrees import CANDIDATE_REF_PREFIX, GitWorktreeManager
from hoh_control_plane.service._common import CANDIDATE_PIN_RETENTION_SECONDS

from test_candidate_submission import _prepared_candidate
from test_integration_async import (
    _candidate_ready_for_integration,
    _claimed_for_integration,
)

SCHEDULER = Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "pins"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    _git(repository, "checkout", "-q", "-b", "feature")
    (repository / "feature.txt").write_text("candidate\n", encoding="utf-8")
    _git(repository, "add", "feature.txt")
    _git(repository, "commit", "-m", "candidate")
    candidate = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    return repository, candidate


def _pin(repository: Path, candidate_id: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", "-q",
                             CANDIDATE_REF_PREFIX + candidate_id],
                            capture_output=True, text=True)
    return result.stdout.strip() or None


def test_a_pin_keeps_the_commit_through_force_push_prune_and_gc(tmp_path):
    repository, candidate = _repository(tmp_path)
    manager = GitWorktreeManager(tmp_path / "worktrees")

    assert manager.pin_candidate(repository_path=str(repository), candidate_id="cand_1",
                                 commit=candidate) is True
    # The developer branch is force-pushed away and everything unreferenced is collected.
    _git(repository, "branch", "-D", "feature")
    _git(repository, "reflog", "expire", "--expire=now", "--all")
    _git(repository, "gc", "-q", "--prune=now")

    assert _git(repository, "cat-file", "-t", candidate) == "commit"
    assert manager.candidate_pins(repository_path=str(repository)) == {"cand_1": candidate}


def test_a_pin_is_create_only_and_never_moves(tmp_path):
    repository, candidate = _repository(tmp_path)
    base = _git(repository, "rev-parse", "main")
    manager = GitWorktreeManager(tmp_path / "worktrees")
    manager.pin_candidate(repository_path=str(repository), candidate_id="cand_1", commit=candidate)

    # A retry at the same commit is a no-op.
    assert manager.pin_candidate(repository_path=str(repository), candidate_id="cand_1",
                                 commit=candidate) is False
    with pytest.raises(ValidationError) as refused:
        manager.pin_candidate(repository_path=str(repository), candidate_id="cand_1", commit=base)
    assert refused.value.details["reason"] == "candidate_pin_conflict"
    assert _pin(repository, "cand_1") == candidate


def test_unpin_is_compare_and_set_on_the_pinned_commit(tmp_path):
    repository, candidate = _repository(tmp_path)
    base = _git(repository, "rev-parse", "main")
    manager = GitWorktreeManager(tmp_path / "worktrees")
    manager.pin_candidate(repository_path=str(repository), candidate_id="cand_1", commit=candidate)

    assert manager.unpin_candidate(repository_path=str(repository), candidate_id="cand_1",
                                   commit=base) is False
    assert _pin(repository, "cand_1") == candidate
    assert manager.unpin_candidate(repository_path=str(repository), candidate_id="cand_1",
                                   commit=candidate) is True
    assert _pin(repository, "cand_1") is None


@pytest.mark.parametrize("candidate_id", ["", "../main", "a/b", "-x", "cand 1"])
def test_an_unsafe_candidate_id_never_becomes_a_ref(tmp_path, candidate_id):
    repository, candidate = _repository(tmp_path)
    with pytest.raises(ValidationError):
        GitWorktreeManager(tmp_path / "worktrees").pin_candidate(
            repository_path=str(repository), candidate_id=candidate_id, commit=candidate)


def test_a_queued_submission_pins_its_candidate(service, actors, seeded, tmp_path: Path):
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)
    queued = service.queue_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], commit, prepared["base_commit"], prepared["branch_ref"],
        ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], ticket["ticket_id"])["context_snapshot_id"])

    completed = service.execute_queued_candidate_submission(queued["submission"]["submission_id"])

    assert _pin(tmp_path / "repository", completed["candidate_id"]) == commit


def _age_ticket(service, ticket_id: str, seconds: int) -> None:
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE tickets SET updated_at = ? WHERE ticket_id = ?",
                           (stamp, ticket_id))


def test_a_pin_outlives_done_until_retention_passes(service, actors, seeded, tmp_path: Path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    commit = candidate["candidate"]["commit_sha"]
    # Pinned at submission (the synchronous path).
    assert _pin(repository, candidate_id) == commit

    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"], integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"])["context_snapshot_id"])
    assert service.execute_queued_merge(queued["merge_job"]["merge_job_id"])["merge_status"] == "merged"

    # DONE alone does not unpin.
    assert service.sync_candidate_pins(SCHEDULER, "project-1")["unpinned"] == []
    assert _pin(repository, candidate_id) == commit

    _age_ticket(service, ticket["ticket_id"], CANDIDATE_PIN_RETENTION_SECONDS + 60)
    assert service.sync_candidate_pins(SCHEDULER, "project-1")["unpinned"] == [candidate_id]
    assert _pin(repository, candidate_id) is None
    # Once gone it is not recreated.
    assert service.sync_candidate_pins(SCHEDULER, "project-1")["pinned"] == []


def test_the_sweep_repairs_a_missed_pin_and_keeps_pins_of_unfinished_tickets(
    service, actors, seeded, tmp_path: Path
):
    repository, ticket, candidate, _verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    commit = candidate["candidate"]["commit_sha"]
    # A crash between the freeze and the pin.
    _git(repository, "update-ref", "-d", CANDIDATE_REF_PREFIX + candidate_id)
    # However old, a ticket that is not DONE keeps its candidate pinned.
    _age_ticket(service, ticket["ticket_id"], CANDIDATE_PIN_RETENTION_SECONDS * 2)

    swept = service.sync_candidate_pins(SCHEDULER, "project-1")

    assert swept["pinned"] == [candidate_id]
    assert swept["unpinned"] == []
    assert _pin(repository, candidate_id) == commit


def test_a_candidate_whose_commit_is_gone_is_reported_not_failed(
    service, actors, seeded, tmp_path: Path
):
    repository, _ticket, candidate, _verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    candidate_id = candidate["candidate"]["candidate_id"]
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE candidates SET commit_sha = ? WHERE candidate_id = ?",
                           ("f" * 40, candidate_id))
    _git(repository, "update-ref", "-d", CANDIDATE_REF_PREFIX + candidate_id)

    swept = service.sync_candidate_pins(SCHEDULER, "project-1")

    assert swept["missing_commits"] == [candidate_id]
    assert _pin(repository, candidate_id) is None
