from __future__ import annotations

import subprocess
from unittest.mock import patch
from pathlib import Path

import pytest

from hoh_control_plane.domain import ConflictError, ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager
from hoh_control_plane import worktree_worker


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
        "hoh_control_plane.git_worktrees.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["git", "status"], 1),
    ):
        with pytest.raises(ValidationError, match="timed out"):
            manager._git(tmp_path, "status", "--porcelain")


def _candidate_ready_for_integration(service, actors, seeded, tmp_path: Path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
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
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert configured["repository_path"] == str(repository.resolve())
    assert prepared["worktree"]["agent_id"] == "dev-01"
    assert worktree.is_dir()
    assert submitted["candidate"]["commit_sha"] == candidate_commit
    assert submitted["candidate"]["changed_paths"] == ["src/checkout/retry.txt"]
    assert context["worktree"]["branch_ref"] == prepared["worktree"]["branch_ref"]
    assert context["candidate"]["changed_paths"] == ["src/checkout/retry.txt"]


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
    )

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
    )

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
    merged = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    assert merged["ticket"]["status"] == "DONE"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]


def test_integrator_returns_candidate_to_rework_when_main_has_drifted(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")
    integration = service.claim_integration(
        actors["integrator"],
        ticket["ticket_id"],
        verified["ticket"]["version"],
        900,
    )

    rework = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

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
