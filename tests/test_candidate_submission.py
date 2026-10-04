from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane import mcp_server
from hoh_control_plane.mcp import workers as mcp_workers
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.mcp_server import create_mcp_server


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _prepared_candidate(service, actors, seeded, tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial commit")
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"], claim["ticket"]["version"]
    )["worktree"]
    worktree = Path(prepared["worktree_path"])
    change = worktree / "src" / "checkout" / "retry.txt"
    change.parent.mkdir(parents=True)
    change.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement retry")
    return ticket, claim, prepared, _git(worktree, "rev-parse", "HEAD")


def test_candidate_submission_queues_without_running_git_validation(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)

    monkeypatch.setattr(
        service.git_worktrees,
        "verify_candidate",
        lambda *_args, **_kwargs: pytest.fail("Git validation must run in the detached worker"),
    )

    queued = service.queue_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], commit, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )

    assert queued["submission"]["status"] == "QUEUED"
    assert queued["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_candidate_submission_worker_freezes_one_candidate_then_poll_returns_it(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)
    queued = service.queue_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], commit, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )

    completed = service.execute_queued_candidate_submission(queued["submission"]["submission_id"])
    polled = service.get_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"], queued["submission"]["submission_id"]
    )

    assert completed["status"] == "COMPLETED"
    assert polled["ticket"]["status"] == "QA_READY"
    assert polled["submission"]["candidate"]["commit_sha"] == commit


def test_duplicate_submission_reuses_one_job_and_one_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)
    arguments = (
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], commit, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )

    first = service.queue_candidate_submission(*arguments)
    retry = service.queue_candidate_submission(*arguments)
    completed = service.execute_queued_candidate_submission(first["submission"]["submission_id"])
    replay = service.execute_queued_candidate_submission(first["submission"]["submission_id"])

    assert retry["submission"]["submission_id"] == first["submission"]["submission_id"]
    assert replay["candidate_id"] == completed["candidate_id"]
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates WHERE ticket_id = ?", (ticket["ticket_id"],)).fetchone()[0] == 1


def test_candidate_submission_worker_persists_validation_failure_for_polling(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)
    queued = service.queue_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], commit, prepared["base_commit"], prepared["branch_ref"], ["Ready."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    monkeypatch.setattr(
        service.git_worktrees, "verify_candidate", lambda **_kwargs: (_ for _ in ()).throw(ValidationError("bad commit"))
    )

    failed = service.execute_queued_candidate_submission(queued["submission"]["submission_id"])
    polled = service.get_candidate_submission(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"], queued["submission"]["submission_id"]
    )

    assert failed["status"] == "FAILED"
    assert polled["submission"]["error_message"] == "bad commit"
    assert polled["ticket"]["status"] == "DEV_IN_PROGRESS"


@pytest.mark.anyio
async def test_developer_mcp_acknowledges_candidate_submission_before_worker_validation(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, claim, prepared, commit = _prepared_candidate(service, actors, seeded, tmp_path)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mcp_workers,
        "_start_candidate_submission_worker",
        lambda database_path, submission_id: launched.append((database_path, submission_id)),
    )

    _, result = await create_mcp_server(service, actors["developer"]).call_tool(
        "dev.submit_candidate",
        {
            "ticket_id": ticket["ticket_id"],
            "lease_id": claim["lease"]["lease_id"],
            "expected_version": claim["ticket"]["version"],
            "commit_sha": commit,
            "base_commit": prepared["base_commit"],
            "branch_ref": prepared["branch_ref"],
            "developer_claims": ["Ready."],
            "context_snapshot_id": service.get_ticket_context(
                actors["developer"], ticket["ticket_id"]
            )["context_snapshot_id"],
        },
    )

    assert result["submission"]["status"] == "QUEUED"
    assert launched == [(service.store.path, result["submission"]["submission_id"])]
