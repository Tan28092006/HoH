import sys

import pytest

from hoh_control_plane.domain import ConflictError, ValidationError
from hoh_control_plane.qa_runner import TestExecution as Execution
from test_qa_runner import _candidate_waiting_for_qa


def test_infrastructure_execution_parks_same_candidate_and_remains_pollable(service, actors, seeded, tmp_path, monkeypatch):
    ticket, candidate, claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"])
    monkeypatch.setattr(service.project_tests, "execute", lambda *a: Execution(
        command_digest="a" * 64, exit_code=None, timed_out=False, duration_ms=1,
        stdout="", stderr="resource_unready", termination_reason="resource_unready"))
    queued = service.queue_project_tests(actors["qa"], ticket["ticket_id"], claim["lease"]["lease_id"])
    service.execute_queued_project_test(queued["test_run"]["test_run_id"])
    polled = service.get_project_test_run(actors["qa"], ticket["ticket_id"],
        claim["lease"]["lease_id"], queued["test_run"]["test_run_id"])
    assert polled["ticket"]["status"] == "BLOCKED"
    assert polled["lease"] is None
    assert polled["ticket"]["blocked_reason"].startswith("qa.infrastructure/")
    with pytest.raises(ConflictError):
        service.claim_ticket(actors["developer"], ticket["ticket_id"], polled["ticket"]["version"], 900)
    packet = service.get_ticket_context(actors["planner"], ticket["ticket_id"])
    assert packet["candidate"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    with pytest.raises(ValidationError, match="readiness"):
        service.recover_candidate_for_qa(actors["planner"], ticket["ticket_id"],
            polled["ticket"]["version"], queued["test_run"]["test_run_id"])
    readiness = service.append_planner_context(actors["planner"], ticket_id=ticket["ticket_id"],
        entry_type="planner_decision", content="Resource headroom restored and runner preflight verified.",
        source_ref="evidence://operations/readiness.json")
    recovered = service.recover_candidate_for_qa(actors["planner"], ticket["ticket_id"],
        polled["ticket"]["version"], queued["test_run"]["test_run_id"], readiness["entry_id"])
    assert recovered["ticket"]["status"] == "QA_READY"
    assert recovered["candidate"]["candidate_id"] == candidate["candidate"]["candidate_id"]


@pytest.mark.parametrize("finding_status,expected", [("insufficient_evidence", "BLOCKED"), ("failed", "REWORK")])
def test_evidence_gap_does_not_dispatch_developer_but_assertion_failure_does(
    service, actors, seeded, tmp_path, finding_status, expected,
):
    ticket, candidate, claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"])
    result = service.submit_qa_verdict(actors["qa"], ticket["ticket_id"], claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"], claim["ticket"]["version"], "failed", [{
            "criterion": ticket["acceptance_criteria"][0], "status": finding_status,
            "observation": "runner evidence missing" if finding_status == "insufficient_evidence" else "assertion failed",
            "evidence": [{"type": "test_result", "observation": "observed", "storage_ref": "evidence://inline"}],
        }])
    assert result["ticket"]["status"] == expected
