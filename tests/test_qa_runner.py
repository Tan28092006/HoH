from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh_control_plane import qa_worker
from hoh_control_plane.domain import Actor, ConflictError, Role, ValidationError


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


def test_qa_queues_without_running_git_checks_in_the_mcp_request(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("QA queueing must not wait for a Git verification")

    monkeypatch.setattr(service.git_worktrees, "verify_frozen_candidate", fail_if_called)

    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )

    assert queued["test_run"]["status"] == "QUEUED"


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


def test_expiring_a_qa_lease_keeps_its_durable_test_job_but_requeues_the_ticket(
    service, store, actors, seeded, tmp_path: Path
) -> None:
    """A QA test job retains its audit lease id after the live lease is reclaimed."""
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    with store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                qa_claim["lease"]["lease_id"],
            ),
        )

    swept = service.sweep_expired_leases(
        Actor("test-scheduler", Role.ADMIN, frozenset({"*"}))
    )

    assert swept["count"] == 1
    with store.connection() as connection:
        retained_job = connection.execute(
            "SELECT lease_id, status FROM qa_test_jobs WHERE test_run_id = ?",
            (queued["test_run"]["test_run_id"],),
        ).fetchone()
    assert retained_job is not None
    assert retained_job["lease_id"] == qa_claim["lease"]["lease_id"]
    assert retained_job["status"] == "QUEUED"


def test_store_migrates_legacy_qa_test_job_lease_foreign_key_without_losing_job(
    service, store, actors, seeded, tmp_path: Path
) -> None:
    """Existing databases must release lease rows while keeping QA audit history."""
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]
    job_columns = (
        "test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command, "
        "command_digest, timeout_seconds, worktree_path, candidate_commit, requested_by, "
        "status, error_message, created_at, started_at, completed_at"
    )
    with store.connection(immediate=True) as connection:
        connection.execute("DROP TABLE qa_test_jobs")
        connection.execute(
            """
            CREATE TABLE qa_test_jobs (
                test_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                test_command TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                worktree_path TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO qa_test_jobs ({job_columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                json.dumps(queued[column]) if column == "test_command" else queued[column]
                for column in job_columns.split(", ")
            ),
        )

    store.initialize()

    with store.connection() as connection:
        lease_foreign_keys = connection.execute("PRAGMA foreign_key_list(qa_test_jobs)").fetchall()
        migrated_job = connection.execute(
            "SELECT test_run_id, lease_id, status FROM qa_test_jobs WHERE test_run_id = ?",
            (queued["test_run_id"],),
        ).fetchone()
    assert all(foreign_key["table"] != "leases" for foreign_key in lease_foreign_keys)
    assert migrated_job is not None
    assert migrated_job["lease_id"] == qa_claim["lease"]["lease_id"]
    assert migrated_job["status"] == "QUEUED"


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
