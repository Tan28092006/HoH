from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane import mcp_server
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.mcp_server import create_mcp_server


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "integration-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial commit")
    return repository


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
    change = worktree / "src" / "checkout" / "retry.txt"
    change.parent.mkdir(parents=True)
    change.write_text("safe retry\n", encoding="utf-8")
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


def _claimed_for_integration(service, actors, ticket, verified):
    return service.claim_integration(
        actors["integrator"], ticket["ticket_id"], verified["ticket"]["version"], 900
    )


def test_merge_candidate_queues_without_running_git(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    monkeypatch.setattr(
        service.git_worktrees,
        "fast_forward_default_branch",
        lambda *_args, **_kwargs: pytest.fail("Git must run in the detached merge worker"),
    )

    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )

    assert queued["merge_job"]["status"] == "QUEUED"
    assert queued["ticket"]["status"] == "MERGE_READY"


def test_merge_worker_fast_forwards_then_poll_reports_completed(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert completed["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE lease_id = ?", (integration["lease"]["lease_id"],)
        ).fetchone()[0] == 0
        final_ticket = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "DONE"


def test_merge_worker_requeues_rework_when_mainline_has_drifted(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert completed["merge_status"] == "base_drift"
    with service.store.connection() as connection:
        final_ticket = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "REWORK"


def test_duplicate_merge_request_reuses_one_job_and_merges_once(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    arguments = (
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )

    first = service.queue_merge_candidate(*arguments)
    retry = service.queue_merge_candidate(*arguments)
    completed = service.execute_queued_merge(first["merge_job"]["merge_job_id"])
    replay = service.execute_queued_merge(first["merge_job"]["merge_job_id"])

    assert retry["merge_job"]["merge_job_id"] == first["merge_job"]["merge_job_id"]
    assert replay["status"] == completed["status"] == "COMPLETED"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM merge_jobs WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0] == 1
        final_ticket = connection.execute(
            "SELECT status, version FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "DONE"
        assert final_ticket["version"] == integration["ticket"]["version"] + 1


def test_merge_worker_persists_git_failure_for_polling(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )
    monkeypatch.setattr(
        service.git_worktrees,
        "fast_forward_default_branch",
        lambda **_kwargs: (_ for _ in ()).throw(ValidationError("Git operation timed out")),
    )

    failed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])
    polled = service.get_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], queued["merge_job"]["merge_job_id"],
    )

    assert failed["status"] == "FAILED"
    assert polled["merge_job"]["error_message"] == "Git operation timed out"
    assert polled["ticket"]["status"] == "MERGE_READY"


def test_poll_before_worker_runs_renews_the_integrator_lease(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )

    polled = service.get_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], queued["merge_job"]["merge_job_id"],
        lease_seconds=1200,
    )

    assert polled["merge_job"]["status"] == "QUEUED"
    assert polled["lease"] is not None
    assert polled["lease"]["expires_at"] > integration["lease"]["expires_at"]


@pytest.mark.anyio
async def test_integrator_mcp_acknowledges_merge_before_worker_runs(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mcp_server,
        "_start_merge_worker",
        lambda database_path, merge_job_id: launched.append((database_path, merge_job_id)),
    )

    _, result = await create_mcp_server(service, actors["integrator"]).call_tool(
        "integrator.merge_candidate",
        {
            "ticket_id": ticket["ticket_id"],
            "lease_id": integration["lease"]["lease_id"],
            "expected_version": integration["ticket"]["version"],
        },
    )

    assert result["merge_job"]["status"] == "QUEUED"
    assert launched == [(service.store.path, result["merge_job"]["merge_job_id"])]
