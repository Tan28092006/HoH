from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path

import pytest
from barrier_helpers import keep_open_tickets
from merge_helpers import merge_through_queue, seed_release_regression_run

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    DomainError,
    Role,
    ValidationError,
)
from hoh_control_plane.git_worktrees import GitWorktreeManager
from hoh_control_plane import worktree_worker


_SCHEDULER = Actor("scheduler-01", Role.ADMIN, frozenset({"*"}))


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


def test_git_command_timeout_becomes_a_domain_error(tmp_path: Path) -> None:
    manager = GitWorktreeManager(tmp_path / "worktrees")

    with patch(
        "hoh_control_plane.git_worktrees.run_managed",
        side_effect=subprocess.TimeoutExpired(["git", "status"], 1),
    ):
        with pytest.raises(ValidationError, match="timed out"):
            manager._git(tmp_path, "status", "--porcelain")


def _candidate_ready_for_integration(service, actors, seeded, tmp_path: Path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    # The release policy requires a registered regression command.
    service.configure_test_command(actors["planner"], "project-1", [sys.executable, "-c", "pass"], 60)
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
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    verified = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Verified against the frozen candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    return repository, ticket, candidate, verified


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
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert configured["repository_path"] == str(repository.resolve())
    assert prepared["worktree"]["agent_id"] == "dev-01"
    assert worktree.is_dir()
    assert submitted["candidate"]["commit_sha"] == candidate_commit
    assert submitted["candidate"]["changed_paths"] == ["src/checkout/retry.txt"]
    assert context["worktree"]["branch_ref"] == prepared["worktree"]["branch_ref"]
    # In-scope paths are summarised in the packet and resolvable by reference.
    assert "changed_paths" not in context["candidate"]
    assert context["candidate"]["changed_paths_summary"] == {
        "count": 1, "within_file_scope": True,
    }
    resolved = service.resolve_context_reference(
        actors["qa"], f"ctx:candidate:{submitted['candidate']['candidate_id']}:changed_paths"
    )
    assert resolved["changed_paths"] == ["src/checkout/retry.txt"]


def test_worktree_provision_is_queued_then_completed_by_a_detached_worker(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """The Developer MCP path queues Git work rather than waiting on it."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("worktree provisioning must not run in the queueing request")

    monkeypatch.setattr(service.git_worktrees, "prepare", fail_if_called)
    queued = service.queue_worktree_provision(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )

    assert queued["provision"]["status"] == "QUEUED"
    assert queued["provision"]["ticket_id"] == ticket["ticket_id"]

    monkeypatch.undo()
    worktree_worker.main(
        [
            "--database",
            str(service.store.path),
            "--provision-id",
            queued["provision"]["provision_id"],
        ]
    )
    polled = service.get_worktree_provision(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        queued["provision"]["provision_id"],
    )

    assert polled["provision"]["status"] == "COMPLETED"
    assert polled["worktree"]["agent_id"] == actors["developer"].agent_id
    assert Path(polled["worktree"]["worktree_path"]).is_dir()


def test_rework_handoffs_to_a_new_developer_without_touching_the_prior_worktree(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A rejected candidate can be continued by a different Developer safely."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    first_claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    first_worktree = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        first_claim["lease"]["lease_id"],
        first_claim["ticket"]["version"],
    )["worktree"]
    old_path = Path(first_worktree["worktree_path"])
    retry_path = old_path / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("first candidate\n", encoding="utf-8")
    _git(old_path, "add", "src/checkout/retry.txt")
    _git(old_path, "commit", "-m", "First candidate")
    first_candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        first_claim["lease"]["lease_id"],
        first_claim["ticket"]["version"],
        _git(old_path, "rev-parse", "HEAD"),
        first_worktree["base_commit"],
        first_worktree["branch_ref"],
        ["First candidate is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], first_candidate["ticket"]["version"], 900
    )
    rework = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        first_candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "failed",
        [
            {
                "criterion": ticket["acceptance_criteria"][0],
                "status": "failed",
                "severity": "high",
                "observation": "A follow-up Developer must repair this candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "candidate rejected",
                        "storage_ref": "evidence://qa/rejected.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    # QA rejection opens a Planner reconciliation atomically with the verdict.
    keep_open_tickets(service, actors["planner"])

    handoff_claim = service.claim_ticket(
        actors["developer_2"], ticket["ticket_id"], rework["ticket"]["version"], 900
    )
    handed_off = service.prepare_worktree(
        actors["developer_2"],
        ticket["ticket_id"],
        handoff_claim["lease"]["lease_id"],
        handoff_claim["ticket"]["version"],
    )["worktree"]
    handoff_path = Path(handed_off["worktree_path"])

    assert handed_off["agent_id"] == "dev-02"
    assert handoff_path != old_path
    assert _git(handoff_path, "rev-parse", "HEAD") == first_candidate["candidate"]["commit_sha"]
    assert handed_off["base_commit"] == first_worktree["base_commit"]
    assert old_path.is_dir()
    assert _git(old_path, "rev-parse", "HEAD") == first_candidate["candidate"]["commit_sha"]

    follow_up = handoff_path / "src" / "checkout" / "handoff.txt"
    follow_up.write_text("second developer\n", encoding="utf-8")
    _git(handoff_path, "add", "src/checkout/handoff.txt")
    _git(handoff_path, "commit", "-m", "Continue rejected candidate")
    second_candidate = service.submit_candidate(
        actors["developer_2"],
        ticket["ticket_id"],
        handoff_claim["lease"]["lease_id"],
        handoff_claim["ticket"]["version"],
        _git(handoff_path, "rev-parse", "HEAD"),
        handed_off["base_commit"],
        handed_off["branch_ref"],
        ["The rejected candidate was continued in a separate worktree."],
        context_snapshot_id=service.get_ticket_context(actors["developer_2"], ticket["ticket_id"])["context_snapshot_id"],
    )

    assert second_candidate["candidate"]["base_commit"] == first_worktree["base_commit"]
    assert second_candidate["candidate"]["changed_paths"] == [
        "src/checkout/handoff.txt",
        "src/checkout/retry.txt",
    ]


def test_rework_handoff_uses_current_main_when_the_rejected_candidate_has_base_drift(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A handoff must never implicitly merge a stale rejected candidate into main."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    first_claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    first_worktree = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        first_claim["lease"]["lease_id"],
        first_claim["ticket"]["version"],
    )["worktree"]
    old_path = Path(first_worktree["worktree_path"])
    retry_path = old_path / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("rejected candidate\n", encoding="utf-8")
    _git(old_path, "add", "src/checkout/retry.txt")
    _git(old_path, "commit", "-m", "Rejected candidate")
    first_candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        first_claim["lease"]["lease_id"],
        first_claim["ticket"]["version"],
        _git(old_path, "rev-parse", "HEAD"),
        first_worktree["base_commit"],
        first_worktree["branch_ref"],
        ["The first candidate is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    (repository / "mainline.txt").write_text("mainline moved\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")
    current_main = _git(repository, "rev-parse", "main")
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], first_candidate["ticket"]["version"], 900
    )
    rework = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        first_candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "failed",
        [
            {
                "criterion": ticket["acceptance_criteria"][0],
                "status": "failed",
                "severity": "high",
                "observation": "The candidate needs a rebase-aware repair.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "candidate rejected after mainline advanced",
                        "storage_ref": "evidence://qa/base-drift.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    # QA rejection opens a Planner reconciliation atomically with the verdict.
    keep_open_tickets(service, actors["planner"])

    handoff_claim = service.claim_ticket(
        actors["developer_2"], ticket["ticket_id"], rework["ticket"]["version"], 900
    )
    handed_off = service.prepare_worktree(
        actors["developer_2"],
        ticket["ticket_id"],
        handoff_claim["lease"]["lease_id"],
        handoff_claim["ticket"]["version"],
    )["worktree"]
    handoff_path = Path(handed_off["worktree_path"])

    assert _git(handoff_path, "rev-parse", "HEAD") == current_main
    assert handed_off["base_commit"] == current_main
    assert not (handoff_path / "src" / "checkout" / "retry.txt").exists()
    assert _git(old_path, "rev-parse", "HEAD") == first_candidate["candidate"]["commit_sha"]


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
            context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
        )


def test_integrator_fast_forwards_a_verified_candidate_to_main(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )

    integration = service.claim_integration(
        actors["integrator"],
        ticket["ticket_id"],
        verified["ticket"]["version"],
        900,
    )
    merged = merge_through_queue(
        service,
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    assert merged["ticket"]["status"] == "DONE"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    reports = service.discover_planner_jobs(actors["planner"], "project-1")
    assert len(reports) == 1
    assert reports[0]["job_type"] == "delivery_completed"
    assert reports[0]["candidate_id"] == candidate["candidate"]["candidate_id"]


def test_integrator_returns_candidate_to_rework_when_main_has_drifted(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = service.claim_integration(
        actors["integrator"],
        ticket["ticket_id"],
        verified["ticket"]["version"],
        900,
    )
    seed_release_regression_run(service, ticket["ticket_id"])
    queued = service.queue_merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )
    # Mainline moves after the merge was accepted but before the worker runs.
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")

    rework = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])
    rework["ticket"] = service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]

    assert rework["ticket"]["status"] == "REWORK"
    assert rework["merge_status"] == "base_drift"


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
            context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
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


def _abandoned_first_developer(service, actors, seeded, tmp_path: Path):
    """Give the first Developer a worktree they will lose before freezing a candidate."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    worktree = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )["worktree"]
    return repository, ticket, claim, worktree


def _expire_lease(service, lease_id: str) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            ("2000-01-01T00:00:00+00:00", lease_id),
        )


def _take_over(service, actors, ticket_id: str, agent: str = "developer_2"):
    """Recover the expired lease the way the scheduler does, then claim as a replacement."""
    sweep = service.sweep_expired_leases(_SCHEDULER)
    version = next(
        entry["version"] for entry in sweep["requeued"] if entry["ticket_id"] == ticket_id
    )
    claim = service.claim_ticket(actors[agent], ticket_id, version, 900)
    prepared = service.prepare_worktree(
        actors[agent], ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"]
    )
    return claim, prepared


def _full_entry(service, actor, entry: dict) -> str:
    reference = f"ctx:entry:{entry['entry_id']}"
    return service.resolve_context_reference(actor, reference)["entry"]["content"]


def _context_entries(service, actor, ticket_id: str, entry_type: str) -> list[dict]:
    entries = service.get_ticket_context(actor, f"ticket:{ticket_id}")["context_entries"]
    return [entry for entry in entries if entry["entry_type"] == entry_type]


def test_git_provision_does_not_hold_the_database_write_lock(service, actors, seeded, tmp_path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    original = service.git_worktrees.prepare

    def prepare(**kwargs):
        import sqlite3
        with sqlite3.connect(service.store.path, timeout=0.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
        return original(**kwargs)

    with patch.object(service.git_worktrees, "prepare", side_effect=prepare):
        prepared = service.prepare_worktree(actors["developer"], ticket["ticket_id"],
                                           claim["lease"]["lease_id"], claim["ticket"]["version"])
    assert Path(prepared["worktree"]["worktree_path"]).is_dir()


def test_expired_provision_job_does_not_block_a_replacement(service, actors, seeded, tmp_path):
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    claim = service.claim_ticket(actors["developer"], "DEV-142", seeded["ticket"]["version"], 900)
    old = service.queue_worktree_provision(actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"])
    _expire_lease(service, claim["lease"]["lease_id"])
    sweep = service.sweep_expired_leases(_SCHEDULER)
    replacement = service.claim_ticket(actors["developer_2"], "DEV-142", sweep["requeued"][0]["version"], 900)
    queued = service.queue_worktree_provision(actors["developer_2"], "DEV-142", replacement["lease"]["lease_id"], replacement["ticket"]["version"])
    assert queued["provision"]["status"] == "QUEUED"
    assert service.execute_queued_worktree_provision(old["provision"]["provision_id"])["status"] == "FAILED"


def test_a_returning_developer_gets_a_new_assignment_without_deleting_old_work(
    service, actors, seeded, tmp_path
):
    _, ticket, first, old = _abandoned_first_developer(service, actors, seeded, tmp_path)
    old_path = Path(old["worktree_path"])
    (old_path / "unfinished.txt").write_text("preserve me", encoding="utf-8")
    _expire_lease(service, first["lease"]["lease_id"])
    second, _ = _take_over(service, actors, ticket["ticket_id"])
    _expire_lease(service, second["lease"]["lease_id"])
    _, returned = _take_over(service, actors, ticket["ticket_id"], "developer")
    assert returned["worktree"]["worktree_path"] != old["worktree_path"]
    assert (old_path / "unfinished.txt").read_text(encoding="utf-8") == "preserve me"


def test_expired_lease_takeover_seeds_the_replacement_from_a_clean_checkpoint(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Committed work must survive a lost lease even with no frozen candidate."""
    repository, ticket, first_claim, first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    old_path = Path(first_worktree["worktree_path"])
    retry_path = old_path / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("checkpointed work\n", encoding="utf-8")
    _git(old_path, "add", "src/checkout/retry.txt")
    _git(old_path, "commit", "-m", "Checkpoint before the lease expired")
    checkpoint = _git(old_path, "rev-parse", "HEAD")
    _expire_lease(service, first_claim["lease"]["lease_id"])

    _claim, prepared = _take_over(service, actors, ticket["ticket_id"])
    replacement = prepared["worktree"]
    new_path = Path(replacement["worktree_path"])

    assert replacement["agent_id"] == "dev-02"
    assert new_path != old_path
    assert replacement["branch_ref"] != first_worktree["branch_ref"]
    assert _git(new_path, "rev-parse", "HEAD") == checkpoint
    assert (new_path / "src" / "checkout" / "retry.txt").read_text(
        encoding="utf-8"
    ) == "checkpointed work\n"
    assert _git(old_path, "rev-parse", "HEAD") == checkpoint

    handoff = _context_entries(
        service, actors["developer_2"], ticket["ticket_id"], "worktree_handoff"
    )
    assert len(handoff) == 1
    assert handoff[0]["artifact_commit"] == checkpoint
    content = handoff[0]["content"]
    assert "dev-01" in content
    assert first_worktree["branch_ref"] in content
    assert first_worktree["base_commit"] in content


def test_takeover_fails_closed_and_records_recovery_evidence_for_a_dirty_worktree(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Uncommitted work is never trusted, never silently lost and never logged raw."""
    repository, ticket, first_claim, first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    old_path = Path(first_worktree["worktree_path"])
    (old_path / "README.md").write_text(
        "# Checkout\nuncommitted-secret-body\n", encoding="utf-8"
    )
    stray = old_path / "src" / "checkout" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("untracked-secret-body\n", encoding="utf-8")
    current_main = _git(repository, "rev-parse", "main")
    _expire_lease(service, first_claim["lease"]["lease_id"])

    _claim, prepared = _take_over(service, actors, ticket["ticket_id"])
    new_path = Path(prepared["worktree"]["worktree_path"])

    assert _git(new_path, "rev-parse", "HEAD") == current_main
    assert prepared["worktree"]["base_commit"] == current_main
    assert not (new_path / "src" / "checkout" / "stray.txt").exists()
    assert "uncommitted-secret-body" not in (new_path / "README.md").read_text(
        encoding="utf-8"
    )

    recovery = _context_entries(
        service, actors["developer_2"], ticket["ticket_id"], "worktree_recovery"
    )
    assert len(recovery) == 1
    # The packet carries a checkpoint; the full account resolves by reference.
    content = _full_entry(service, actors["developer_2"], recovery[0])
    assert "uncommitted-secret-body" not in content
    assert "untracked-secret-body" not in content
    assert "dev-01" in content
    assert first_worktree["branch_ref"] in content
    assert "src/checkout/stray.txt" in content

    artifact = Path(recovery[0]["source_ref"])
    assert artifact.is_file()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() in content
    assert not artifact.is_relative_to(service.git_worktrees.worktree_root)


def test_takeover_fails_closed_when_the_prior_worktree_is_missing(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A vanished worktree gives the replacement current main, not a silent success."""
    repository, ticket, first_claim, first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    current_main = _git(repository, "rev-parse", "main")
    shutil.rmtree(Path(first_worktree["worktree_path"]))
    _expire_lease(service, first_claim["lease"]["lease_id"])

    _claim, prepared = _take_over(service, actors, ticket["ticket_id"])

    assert _git(Path(prepared["worktree"]["worktree_path"]), "rev-parse", "HEAD") == current_main
    recovery = _context_entries(
        service, actors["developer_2"], ticket["ticket_id"], "worktree_recovery"
    )
    assert len(recovery) == 1
    assert recovery[0]["source_ref"] is None
    assert "missing" in _full_entry(service, actors["developer_2"], recovery[0])


def test_takeover_appends_assignment_history_without_overwriting_the_prior_record(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Who held which branch, and when, stays auditable across a takeover."""
    _repository_path, ticket, first_claim, first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    _expire_lease(service, first_claim["lease"]["lease_id"])
    _claim, prepared = _take_over(service, actors, ticket["ticket_id"])

    with service.store.connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM worktree_assignments WHERE ticket_id = ? ORDER BY assigned_at ASC",
                (ticket["ticket_id"],),
            ).fetchall()
        ]

    assert [row["agent_id"] for row in rows] == ["dev-01", "dev-02"]
    assert rows[0]["branch_ref"] == first_worktree["branch_ref"]
    assert rows[0]["worktree_path"] == first_worktree["worktree_path"]
    assert rows[0]["released_at"] is not None
    assert rows[1]["branch_ref"] == prepared["worktree"]["branch_ref"]
    assert rows[1]["released_at"] is None


def test_the_prior_branch_is_never_accepted_for_a_candidate_after_takeover(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Neither the replaced Developer nor their branch can freeze a candidate."""
    _repository_path, ticket, first_claim, first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    old_path = Path(first_worktree["worktree_path"])
    retry_path = old_path / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("checkpointed work\n", encoding="utf-8")
    _git(old_path, "add", "src/checkout/retry.txt")
    _git(old_path, "commit", "-m", "Checkpoint before the lease expired")
    checkpoint = _git(old_path, "rev-parse", "HEAD")
    _expire_lease(service, first_claim["lease"]["lease_id"])
    replacement_claim, prepared = _take_over(service, actors, ticket["ticket_id"])

    with pytest.raises(DomainError):
        service.submit_candidate(
            actors["developer"],
            ticket["ticket_id"],
            first_claim["lease"]["lease_id"],
            prepared["ticket"]["version"],
            checkpoint,
            first_worktree["base_commit"],
            first_worktree["branch_ref"],
            ["Late submission from the replaced Developer."],
            context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
        )

    with pytest.raises(ValidationError, match="branch"):
        service.submit_candidate(
            actors["developer_2"],
            ticket["ticket_id"],
            replacement_claim["lease"]["lease_id"],
            prepared["ticket"]["version"],
            checkpoint,
            first_worktree["base_commit"],
            first_worktree["branch_ref"],
            ["Submitting from the prior Developer's branch."],
            context_snapshot_id=service.get_ticket_context(actors["developer_2"], ticket["ticket_id"])["context_snapshot_id"],
        )

    context = service.get_ticket_context(
        actors["developer_2"], f"ticket:{ticket['ticket_id']}"
    )
    assert context["candidate"] is None


def test_worktree_preparation_rejects_an_expired_developer_lease(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    _expire_lease(service, claim["lease"]["lease_id"])

    with pytest.raises(ConflictError, match="expired"):
        service.prepare_worktree(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
        )


def test_takeover_rejects_a_developer_outside_the_project(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository_path, ticket, first_claim, _first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    _expire_lease(service, first_claim["lease"]["lease_id"])
    replacement_claim, _prepared = _take_over(service, actors, ticket["ticket_id"])
    outsider = Actor("dev-99", Role.DEVELOPER, frozenset({"project-2"}))

    with pytest.raises(AuthorizationError):
        service.prepare_worktree(
            outsider,
            ticket["ticket_id"],
            replacement_claim["lease"]["lease_id"],
            replacement_claim["ticket"]["version"],
        )


def test_replacement_candidate_is_still_bound_to_the_ticket_write_scope(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository_path, ticket, first_claim, _first_worktree = _abandoned_first_developer(
        service, actors, seeded, tmp_path
    )
    _expire_lease(service, first_claim["lease"]["lease_id"])
    replacement_claim, prepared = _take_over(service, actors, ticket["ticket_id"])
    new_path = Path(prepared["worktree"]["worktree_path"])
    (new_path / "unscoped.txt").write_text("outside the write scope\n", encoding="utf-8")
    _git(new_path, "add", "unscoped.txt")
    _git(new_path, "commit", "-m", "Change a path outside the ticket scope")

    with pytest.raises(ValidationError, match="write scope"):
        service.submit_candidate(
            actors["developer_2"],
            ticket["ticket_id"],
            replacement_claim["lease"]["lease_id"],
            prepared["ticket"]["version"],
            _git(new_path, "rev-parse", "HEAD"),
            prepared["worktree"]["base_commit"],
            prepared["worktree"]["branch_ref"],
            ["Touches a path outside the declared write scope."],
            context_snapshot_id=service.get_ticket_context(actors["developer_2"], ticket["ticket_id"])["context_snapshot_id"],
        )
