"""Wave 8A, R1: only a run the runner classified as 'passed' is evidence.

A run that ended for an infrastructure reason keeps its exit code (an output
cap leaves exit 0), so neither run reuse nor the merge gate may read exit 0 as
passing.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from hoh_control_plane.domain import ReleasePolicyError
from hoh_control_plane.qa_runner import TestExecution as Execution
from helpers.gate import _PASSING_REGRESSION, _gate_ready_candidate
from helpers.qa import candidate_waiting_for_qa_run as _candidate_waiting_for_qa


def _truncated(*_arguments) -> Execution:
    return Execution(command_digest="a" * 64, exit_code=0, timed_out=False, duration_ms=1,
                     stdout="x" * 10, stderr="", termination_reason="output_truncated",
                     stdout_truncated=True)


def test_a_truncated_run_with_exit_zero_is_recorded_with_its_reason_and_not_reused(
    service, actors, seeded, tmp_path, monkeypatch
):
    ticket, candidate, claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"])
    with monkeypatch.context() as patch:
        patch.setattr(service.project_tests, "execute", _truncated)
        queued = service.queue_project_tests(actors["qa"], ticket["ticket_id"], claim["lease"]["lease_id"])
        service.execute_queued_project_test(queued["test_run"]["test_run_id"])
    polled = service.get_project_test_run(actors["qa"], ticket["ticket_id"],
        claim["lease"]["lease_id"], queued["test_run"]["test_run_id"])
    assert polled["test_run"]["exit_code"] == 0
    assert polled["test_run"]["termination_reason"] == "output_truncated"
    assert polled["test_run"]["provenance_status"] == "unverified"
    assert polled["ticket"]["status"] == "BLOCKED"

    # The review's scenario: Planner recovers the candidate, QA claims it again.
    readiness = service.append_planner_context(actors["planner"], ticket_id=ticket["ticket_id"],
        entry_type="planner_decision", content="Output cap raised; runner verified.",
        source_ref="evidence://operations/readiness.json")
    recovered = service.recover_candidate_for_qa(actors["planner"], ticket["ticket_id"],
        polled["ticket"]["version"], queued["test_run"]["test_run_id"], readiness["entry_id"])
    again = service.claim_qa(actors["qa"], ticket["ticket_id"], recovered["ticket"]["version"], 900)
    requeued = service.queue_project_tests(actors["qa"], ticket["ticket_id"], again["lease"]["lease_id"])
    assert not requeued.get("reused")
    assert requeued["test_run"]["status"] == "QUEUED"

    # A genuinely passing run is reused afterwards.
    service.execute_queued_project_test(requeued["test_run"]["test_run_id"])
    reused = service.queue_project_tests(actors["qa"], ticket["ticket_id"], again["lease"]["lease_id"])
    assert reused["reused"] is True
    assert reused["test_run"]["termination_reason"] == "passed"
    assert service._reusable_qa_run(
        candidate["candidate"]["candidate_id"], [sys.executable, "-c", "print('passed')"], 120
    ) == requeued["test_run"]["test_run_id"]


def _seed_run(service, ticket_id, *, reason, exit_code=0, report=None, checksum=None):
    with service.store.connection(immediate=True) as connection:
        ticket = service._ticket(connection, ticket_id)
        candidate = connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
            (ticket_id, ticket["candidate_commit"])).fetchone()["candidate_id"]
        command = json.loads(connection.execute(
            "SELECT test_command FROM candidate_test_profiles WHERE candidate_id = ?",
            (candidate,)).fetchone()["test_command"])
        connection.execute(
            """INSERT INTO qa_test_runs
                (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                 duration_ms, storage_ref, report_path, storage_checksum, executed_by,
                 created_at, termination_reason)
               VALUES (?, ?, ?, ?, ?, 0, 10, 'evidence://local/run.json', ?, ?, 'qa-01', ?, ?)""",
            (f"testrun_seed_{reason}", candidate, ticket_id,
             hashlib.sha256("\0".join(command).encode()).hexdigest(), exit_code,
             str(report or "missing.json"), checksum, "2026-01-01T00:00:00Z", reason))
    return candidate


def _queue(service, actors, ticket, integration):
    return service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"], integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"])["context_snapshot_id"])


@pytest.mark.parametrize("reason", ["output_truncated", "capture_failure", "unclassified"])
def test_the_gate_refuses_a_candidate_whose_only_exit_zero_run_did_not_pass(
    service, actors, seeded, tmp_path, reason
):
    _repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    _seed_run(service, ticket["ticket_id"], reason=reason)
    with pytest.raises(ReleasePolicyError) as refused:
        _queue(service, actors, ticket, integration)
    assert refused.value.details["reason"] == "regression_run_failed"


def test_a_legacy_row_counts_only_when_its_checksummed_report_says_passed(
    service, actors, seeded, tmp_path
):
    _repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    report = tmp_path / "legacy-report.json"
    body = json.dumps({"termination_reason": "output_truncated", "exit_code": 0}).encode()
    report.write_bytes(body)
    # A row written before the column: NULL reason, report on disk.
    _seed_run(service, ticket["ticket_id"], reason=None, report=report,
              checksum=hashlib.sha256(body).hexdigest())
    with pytest.raises(ReleasePolicyError):
        _queue(service, actors, ticket, integration)

    passing = json.dumps({"termination_reason": "passed", "exit_code": 0}).encode()
    run = {"exit_code": 0, "timed_out": 0, "termination_reason": None,
           "report_path": str(report), "storage_checksum": hashlib.sha256(passing).hexdigest()}
    report.write_bytes(passing)
    assert service._run_passed(run) is True
    report.write_bytes(passing + b" ")  # edited after the fact
    assert service._run_passed(run) is False
    assert service._run_passed({**run, "storage_checksum": None}) is False
    assert service._run_passed({**run, "termination_reason": "passed", "exit_code": 1}) is False


def test_a_gate_execution_that_exits_zero_without_passing_does_not_land(
    service, actors, seeded, tmp_path, monkeypatch
):
    from helpers.merge import merge_through_queue
    import subprocess

    repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = subprocess.run(["git", "-C", str(repository), "rev-parse", "main"],
                            capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setattr(service.project_tests, "execute", lambda *a: Execution(
        command_digest="a" * 64, exit_code=0, timed_out=False, duration_ms=1,
        stdout="", stderr="", termination_reason="no_tests_passed"))
    outcome = merge_through_queue(service, actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"])
    assert outcome["ticket"]["status"] == "REWORK"
    after = subprocess.run(["git", "-C", str(repository), "rev-parse", "main"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert after == before
    with service.store.connection() as connection:
        gate = connection.execute(
            "SELECT termination_reason FROM qa_test_runs WHERE ticket_id = ? "
            "AND test_run_id NOT LIKE 'testrun_seed_%' ORDER BY created_at DESC LIMIT 1",
            (ticket["ticket_id"],)).fetchone()
    assert gate["termination_reason"] == "no_tests_passed"


def test_queued_gate_proof_requires_a_passed_run(service, actors, seeded, tmp_path):
    _repository, ticket, _candidate, _integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    candidate_id = _seed_run(service, ticket["ticket_id"], reason="output_truncated")
    job = {"merge_job_id": "merge_job_x", "candidate_id": candidate_id}
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "INSERT INTO audit_events (event_id, project_id, event_type, aggregate_type, aggregate_id, "
            "aggregate_version, actor_id, actor_role, payload, occurred_at) "
            "VALUES ('evt_x', 'project-1', 'integration.gate_passed', 'merge_job', 'merge_job_x', 1, "
            "'integrator-01', 'integrator', ?, '2026-01-01T00:00:00Z')",
            (json.dumps({"candidate_id": candidate_id, "test_run_id": "testrun_seed_output_truncated"}),))
        assert service._queued_gate_is_recorded(connection, job) is False
        connection.execute("UPDATE qa_test_runs SET termination_reason = 'passed' "
                           "WHERE test_run_id = 'testrun_seed_output_truncated'")
        assert service._queued_gate_is_recorded(connection, job) is True
