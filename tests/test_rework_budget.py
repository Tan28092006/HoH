"""Dev and QA cannot loop forever: the third rejection goes to Planner.

A maker that keeps missing and a checker that keeps rejecting is a planning
problem, not an execution one. The service counts failed QA verdicts per ticket
and, on the third, blocks the ticket and opens a Planner job instead of sending
it back for another round. Only a recorded Planner decision starts a new count.
"""
from __future__ import annotations

import pytest
from barrier_helpers import keep_open_tickets

from hoh_control_plane.domain import ConflictError

from hoh_control_plane.service._common import MAX_QA_REJECTIONS


def _ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _events(service, ticket_id: str, event_type: str) -> list:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT payload FROM audit_events WHERE aggregate_id = ? AND event_type = ?",
            (ticket_id, event_type),
        ).fetchall()


def _reject_once(service, actors, ticket_id: str, attempt: int) -> dict:
    developer, qa = actors["developer"], actors["qa"]
    claim = service.claim_ticket(developer, ticket_id, _ticket(service, ticket_id)["version"], 900)
    submitted = service.submit_candidate(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
        f"{attempt:x}" * 40, "b" * 40, "agent/dev-01/DEV-142", [f"Attempt {attempt}."],
        context_snapshot_id=service.get_ticket_context(developer, ticket_id)["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(qa, ticket_id, submitted["ticket"]["version"], 900)
    criterion = _ticket(service, ticket_id)["acceptance_criteria"][0]
    verdict = service.submit_qa_verdict(
        qa, ticket_id, qa_claim["lease"]["lease_id"], submitted["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"], "failed",
        [{
            "criterion": criterion, "status": "failed", "severity": "high",
            "observation": "A duplicate order is still created.",
            "evidence": [{"type": "test_result", "observation": "duplicate order",
                          "storage_ref": "evidence://qa/duplicate.json"}],
        }],
        context_snapshot_id=qa_claim["current_context_snapshot_id"],
    )
    keep_open_tickets(service, actors["planner"])
    return verdict


def test_the_third_qa_rejection_blocks_the_ticket_for_planner(service, actors, seeded) -> None:
    ticket_id = seeded["ticket"]["ticket_id"]
    assert MAX_QA_REJECTIONS == 3

    first = _reject_once(service, actors, ticket_id, 1)
    second = _reject_once(service, actors, ticket_id, 2)
    assert (first["qa_rejections"], second["qa_rejections"]) == (1, 2)
    assert _ticket(service, ticket_id)["status"] == "REWORK"

    third = _reject_once(service, actors, ticket_id, 3)

    ticket = _ticket(service, ticket_id)
    assert third["rework_budget_exhausted"] is True and third["qa_rejections"] == 3
    assert ticket["status"] == "BLOCKED"
    assert ticket["blocked_reason"] == "rework_budget_exhausted"
    assert third["planner_job"]["summary"].startswith("rework_budget_exhausted:")
    assert third["planner_job"]["status"] == "READY"
    assert len(_events(service, ticket_id, "ticket.rework_budget_exhausted")) == 1
    # No Developer can pick the ticket up again until Planner decides.
    with pytest.raises(ConflictError, match="cannot be claimed from BLOCKED"):
        service.claim_ticket(actors["developer"], ticket_id, ticket["version"], 900)


def test_planner_resuming_the_ticket_records_the_reset(service, actors, seeded) -> None:
    ticket_id = seeded["ticket"]["ticket_id"]
    for attempt in range(1, MAX_QA_REJECTIONS + 1):
        exhausted = _reject_once(service, actors, ticket_id, attempt)
    planner = actors["planner"]
    job = service.claim_planner_job(planner, exhausted["planner_job"]["planner_job_id"])

    resumed = service.resume_ticket_after_failure(
        planner, job["planner_job"]["planner_job_id"], job["lease"]["lease_id"],
        _ticket(service, ticket_id)["version"], "developer",
        "Contract revised: retry is idempotent per order id; one more attempt.",
    )

    assert resumed["rework_budget_reset"] is True
    assert resumed["ticket"]["status"] == "REWORK"
    assert resumed["ticket"]["blocked_reason"] is None
    resets = _events(service, ticket_id, "ticket.rework_budget_reset")
    assert len(resets) == 1 and '"qa_rejections": 3' in resets[0]["payload"]
    decision = service.resolve_context_reference(planner, resumed["context_ref"])
    assert "reset the exhausted QA rework budget" in decision["entry"]["content"]

    # The count starts again only after that recorded decision.
    again = _reject_once(service, actors, ticket_id, 4)
    assert again["qa_rejections"] == 1
    assert _ticket(service, ticket_id)["status"] == "REWORK"
