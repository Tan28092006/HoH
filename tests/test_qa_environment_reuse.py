"""Shared execution identity comes from trusted runner inputs, never the reviewer."""
import json
import sys

import pytest

from helpers.qa import candidate_waiting_for_qa_run
from hoh_control_plane.domain import Actor, AuthorizationError, ConflictError, ReleasePolicyError, Role
from hoh_control_plane.qa_runner import ProjectTestRunner

pytestmark = pytest.mark.git


def test_stable_identity_ignores_ephemeral_paths_and_secret_values(tmp_path, monkeypatch):
    runner = ProjectTestRunner(tmp_path / "evidence")
    command = [sys.executable, "-c", "print('passed')"]
    a = runner._build_environment(str(tmp_path / "one"))
    b = runner._build_environment(str(tmp_path / "two"))
    b["HOH_API_TOKEN"] = "must-not-be-retained"
    b["UNRELATED_SECRET"] = "must-not-be-retained"
    digest = runner.execution_environment_digest(command, 120, environment=a)
    assert runner.execution_environment_digest(command, 120, environment=b) == digest
    assert len(digest) == 64 and "must-not-be-retained" not in digest
    b["OMP_NUM_THREADS"] = "9"
    assert runner.execution_environment_digest(command, 120, environment=b) != digest
    assert runner.execution_environment_digest(command, 121, environment=a) != digest


def test_passing_execution_reuses_only_in_matching_environment(service, actors, seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-retained")
    t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path,
        [sys.executable, "-c", "print('passed')"])
    args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
    first = service.queue_project_tests(*args)["test_run"]
    stranger = Actor("stranger", Role.QA, frozenset({"project-1"}))
    with pytest.raises(AuthorizationError):
        service.queue_project_tests(stranger, t["ticket_id"], owner["lease"]["lease_id"])
    with pytest.raises(AuthorizationError):
        service.get_project_test_run(stranger, t["ticket_id"], owner["lease"]["lease_id"], first["test_run_id"])
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id=?", ("e" * 40, t["ticket_id"]))
    with pytest.raises(ConflictError):
        service.queue_project_tests(*args)
    with pytest.raises(ConflictError):
        service.get_project_test_run(*args, first["test_run_id"])
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id=?", (first["candidate_commit"], t["ticket_id"]))
        db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (owner["lease"]["lease_id"],))
    with pytest.raises(ConflictError, match="expired"):
        service.queue_project_tests(*args)
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (owner["lease"]["expires_at"], owner["lease"]["lease_id"]))
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs WHERE candidate_id=?", (first["candidate_id"],)).fetchone()[0] == 1
    completed = service.execute_queued_project_test(first["test_run_id"])
    assert completed["status"] == "COMPLETED" and completed["termination_reason"] == "passed"
    reused = service.queue_project_tests(*args)
    assert reused["reused"] is True and reused["test_run"]["test_run_id"] == first["test_run_id"]
    command, timeout = first["test_command"], first["timeout_seconds"]
    candidate_id = submitted["candidate"]["candidate_id"]
    assert service._reusable_qa_run(candidate_id, command, timeout) == first["test_run_id"]
    assert "must-not-be-retained" not in json.dumps(completed)
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    assert service._reusable_qa_run(candidate_id, command, timeout) is None
    fresh = service.queue_project_tests(*args)
    assert not fresh.get("reused") and fresh["test_run"]["test_run_id"] != first["test_run_id"]
    assert service.queue_project_tests(*args)["test_run"]["test_run_id"] == fresh["test_run"]["test_run_id"]
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    with pytest.raises(ConflictError) as denied:
        service.queue_project_tests(*args)
    assert denied.value.details["reason"] == "execution_environment_changed"
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    with service.store.connection() as db:
        state = service._candidate_suite_state(db, candidate_id, command, timeout)
    with pytest.raises(ReleasePolicyError) as pending:
        service._require_regression_run(state)
    assert pending.value.details["reason"] == "regression_run_pending"
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE qa_test_jobs SET status='FAILED' WHERE test_run_id=?", (fresh["test_run"]["test_run_id"],))
    assert service._reusable_qa_run(candidate_id, command, timeout) is None
    with service.store.connection() as db:
        state = service._candidate_suite_state(db, candidate_id, command, timeout)
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs WHERE candidate_id=?", (candidate_id,)).fetchone()[0] == 2
    with pytest.raises(ReleasePolicyError) as failed:
        service._require_regression_run(state)
    assert failed.value.details["reason"] == "regression_run_failed"


def test_unproved_completed_environment_cannot_be_reused(service, actors, seeded, tmp_path):
    t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path,
        [sys.executable, "-c", "print('passed')"])
    args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
    first = service.queue_project_tests(*args)["test_run"]
    service.execute_queued_project_test(first["test_run_id"])
    with service.store.connection(immediate=True) as db:
        db.execute("ALTER TABLE qa_test_jobs DROP COLUMN execution_environment_digest")
        db.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")
    service.store.initialize()
    service.store.initialize()
    with service.store.connection() as db:
        historical = service._qa_test_job(db, first["test_run_id"])
        assert historical["status"] == "COMPLETED" and historical["exit_code"] == 0
        assert historical["execution_environment_digest"] is None
    assert service._reusable_qa_run(submitted["candidate"]["candidate_id"], first["test_command"], first["timeout_seconds"]) is None
    assert service.queue_project_tests(*args)["test_run"]["test_run_id"] != first["test_run_id"]
