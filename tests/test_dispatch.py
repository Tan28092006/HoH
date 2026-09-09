from __future__ import annotations

import pytest

from hoh_control_plane.domain import Actor, ConflictError, Role, TicketStatus
from hoh_control_plane.service import ControlPlaneService


def _submit_candidate(
    service: ControlPlaneService, actors: dict[str, Actor], seeded: dict[str, object]
) -> dict[str, object]:
    ticket = seeded["ticket"]
    claimed = service.claim_ticket(
        actors["developer"], ticket_id="DEV-142", expected_version=ticket["version"], lease_seconds=900
    )
    return service.submit_candidate(
        actors["developer"],
        ticket_id="DEV-142",
        lease_id=claimed["lease"]["lease_id"],
        expected_version=claimed["ticket"]["version"],
        commit_sha="a" * 40,
        base_commit="b" * 40,
        branch_ref="agent/dev-01/DEV-142",
        developer_claims=["Candidate is ready for independent QA"],
    )


def test_candidate_is_offered_to_online_qas_but_first_claim_wins(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    submitted = _submit_candidate(service, actors, seeded)
    qa_two = Actor("qa-02", Role.QA, frozenset({"project-1"}))
    admin = Actor("scheduler", Role.ADMIN, frozenset({"*"}))

    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=1, machine_id="laptop-a")
    service.register_agent(qa_two, skills=["hoh-qa"], capacity=1, machine_id="laptop-b")
    dispatch = service.dispatch_pending_work(admin, offer_seconds=90)

    assert dispatch["offered_count"] == 2
    assert service.get_agent_inbox(actors["qa"], "project-1")["offers"][0]["ticket"]["ticket_id"] == "DEV-142"
    assert service.get_agent_inbox(qa_two, "project-1")["offers"][0]["ticket"]["ticket_id"] == "DEV-142"

    qa_claim = service.claim_qa(
        actors["qa"], "DEV-142", submitted["ticket"]["version"], 900
    )
    assert qa_claim["ticket"]["status"] == TicketStatus.QA_IN_PROGRESS.value
    with pytest.raises(ConflictError):
        service.claim_qa(qa_two, "DEV-142", qa_claim["ticket"]["version"], 900)

    overview = service.dispatch_overview(actors["planner"], "project-1")
    queue = next(queue for queue in overview["queues"] if queue["target_role"] == Role.QA.value)
    assert queue["status"] == "claimed"
    assert queue["claimed_by"] == "qa-01"
    assert queue["open_offer_count"] == 0


def test_offline_qa_work_stays_durable_until_an_agent_checks_in(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    _submit_candidate(service, actors, seeded)
    admin = Actor("scheduler", Role.ADMIN, frozenset({"*"}))

    first_dispatch = service.dispatch_pending_work(admin, offer_seconds=90)
    assert first_dispatch["offered_count"] == 0
    assert first_dispatch["pending_count"] == 1

    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=1, machine_id="late-qa")
    inbox = service.get_agent_inbox(actors["qa"], "project-1")

    assert inbox["offers"][0]["target_role"] == Role.QA.value
    assert inbox["offers"][0]["ticket"]["status"] == TicketStatus.QA_READY.value


def test_later_qa_check_in_receives_an_offer_even_when_another_offer_is_open(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    _submit_candidate(service, actors, seeded)
    qa_two = Actor("qa-02", Role.QA, frozenset({"project-1"}))

    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=1)
    first_inbox = service.get_agent_inbox(actors["qa"], "project-1")
    service.register_agent(qa_two, skills=["hoh-qa"], capacity=1)
    later_inbox = service.get_agent_inbox(qa_two, "project-1")

    assert first_inbox["offers"][0]["ticket"]["ticket_id"] == "DEV-142"
    assert later_inbox["offers"][0]["ticket"]["ticket_id"] == "DEV-142"


def test_failed_qa_requeues_ticket_for_an_available_developer(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    submitted = _submit_candidate(service, actors, seeded)
    qa_claim = service.claim_qa(
        actors["qa"], "DEV-142", submitted["ticket"]["version"], 900
    )
    service.submit_qa_verdict(
        actors["qa"],
        ticket_id="DEV-142",
        lease_id=qa_claim["lease"]["lease_id"],
        candidate_id=submitted["candidate"]["candidate_id"],
        expected_version=qa_claim["ticket"]["version"],
        verdict="failed",
        findings=[
            {
                "criterion": "A successful retry creates exactly one order",
                "status": "failed",
                "severity": "high",
                "observation": "A duplicate order is still created.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "duplicate-order regression reproduced",
                        "storage_ref": "evidence://qa/duplicate-order.json",
                    }
                ],
            }
        ],
    )

    service.register_agent(
        actors["developer_2"], skills=["hoh-developer"], capacity=1, machine_id="dev-b"
    )
    inbox = service.get_agent_inbox(actors["developer_2"], "project-1")

    assert inbox["offers"][0]["target_role"] == Role.DEVELOPER.value
    assert inbox["offers"][0]["ticket"]["status"] == TicketStatus.REWORK.value
