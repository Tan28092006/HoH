"""A worker's failure recorder records any error, however long or empty (audit A1, B3).

Each queued worker catches a domain error and hands its text to a recorder that
marks the job FAILED. The recorders validated that text like user input, so an
error longer than the bound (a long out-of-scope path list, a Git error) or an
empty one raised inside the worker's ``except`` and left the job RUNNING, where
nothing polls it to an end. They now keep a bounded prefix instead.
"""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import ValidationError

LONG = "src/outside/" + "x" * 6000


def _ticket(service) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, "DEV-142")


def _queued_submission(service, actors) -> str:
    claim = service.claim_ticket(actors["developer"], "DEV-142", _ticket(service)["version"], 900)
    queued = service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    return queued["submission"]["submission_id"]


def test_a_long_worker_error_still_ends_the_submission_job(service, actors, seeded, monkeypatch) -> None:
    submission_id = _queued_submission(service, actors)

    def failing_git_inputs(_submission_id):
        raise ValidationError("Candidate changes paths outside ticket write scope: " + LONG)

    monkeypatch.setattr(service, "_candidate_submission_git_inputs", failing_git_inputs)
    job = service.execute_queued_candidate_submission(submission_id)

    assert job["status"] == "FAILED"
    assert job["error_message"].startswith("Candidate changes paths outside ticket write scope")
    assert len(job["error_message"]) <= 4000
    assert job["error_message"].endswith("[truncated]")


@pytest.mark.parametrize("message", ["", "   ", LONG])
def test_the_submission_recorder_accepts_any_text(service, actors, seeded, message) -> None:
    submission_id = _queued_submission(service, actors)
    job = service.fail_queued_candidate_submission(submission_id, message)
    assert job["status"] == "FAILED"
    assert job["error_message"].strip()
    assert len(job["error_message"]) <= 4000
    # Recording twice is a no-op on a terminal job.
    assert service.fail_queued_candidate_submission(submission_id, "later")["error_message"] == job["error_message"]


@pytest.mark.parametrize("message", ["", LONG])
def test_the_provision_recorder_accepts_any_text(service, actors, seeded, message) -> None:
    claim = service.claim_ticket(actors["developer"], "DEV-142", _ticket(service)["version"], 900)
    queued = service.queue_worktree_provision(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"])
    job = service.fail_queued_worktree_provision(queued["provision"]["provision_id"], message)
    assert job["status"] == "FAILED"
    assert job["error_message"].strip()
    assert len(job["error_message"]) <= 4000


def _candidate_id(service, actors) -> str:
    claim = service.claim_ticket(actors["developer"], "DEV-142", _ticket(service)["version"], 900)
    return service.submit_candidate(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )["candidate"]["candidate_id"]


def _plant_job(service, actors, table: str, key: str, job_id: str, extra: dict) -> None:
    """A RUNNING worker job, as the worker leaves it just before recording its error."""
    columns = {key: job_id, "project_id": "project-1", "ticket_id": "DEV-142",
               "candidate_id": _candidate_id(service, actors),
               "lease_id": "lease_x", "status": "RUNNING",
               "created_at": "2026-01-01T00:00:00Z", **extra}
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )


@pytest.mark.parametrize("message", ["", LONG])
def test_the_merge_recorder_accepts_any_text(service, actors, seeded, message) -> None:
    _plant_job(service, actors, "merge_jobs", "merge_job_id", "merge_job_x", {
        "expected_version": 1, "requested_by": "integrator-01"})
    job = service.fail_queued_merge("merge_job_x", message)
    assert job["status"] == "FAILED"
    assert job["error_message"].strip()
    assert len(job["error_message"]) <= 4000


@pytest.mark.parametrize("message", ["", LONG])
def test_the_qa_test_recorder_accepts_any_text(service, actors, seeded, message) -> None:
    _plant_job(service, actors, "qa_test_jobs", "test_run_id", "testrun_x", {
        "test_command": "[]", "command_digest": "d" * 64,
        "timeout_seconds": 60, "worktree_path": "unused", "candidate_commit": "a" * 40,
        "requested_by": "qa-01"})
    job = service.fail_queued_project_test("testrun_x", message)
    assert job["status"] == "FAILED"
    assert job["error_message"].strip()
    assert len(job["error_message"]) <= 2000
