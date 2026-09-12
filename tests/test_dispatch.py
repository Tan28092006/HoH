from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hoh_control_plane.domain import Actor, ConflictError, Role, TicketStatus
from hoh_control_plane.service import ControlPlaneService


def _expire_lease(store, lease_id: str) -> None:
    with store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease_id),
        )


def test_small_inbox_does_not_materialize_offers_for_the_whole_backlog(service, actors, seeded):
    for index in range(12):
        service.create_ticket(actors["planner"], "payment-retry", f"BOUND-{index}", "Bounded", "Backlog",
                              Role.DEVELOPER, ["Visible"], [], [f"part-{index}/**"], ready=True)
    service.register_agent(actors["developer"], skills=["hoh-developer"])
    first = service.get_agent_inbox(actors["developer"], "project-1", limit=3)
    second = service.get_agent_inbox(actors["developer"], "project-1", limit=3)
    assert first["offered_now"] == 3
    assert second["offered_now"] == 0
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_offers WHERE agent_id = ? AND status = 'offered'",
                                  (actors["developer"].agent_id,)).fetchone()[0] == 3


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


def test_inbox_returns_the_callers_active_lease_before_new_offers(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    submitted = _submit_candidate(service, actors, seeded)
    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=1)
    claimed = service.claim_qa(
        actors["qa"], "DEV-142", submitted["ticket"]["version"], 900
    )

    inbox = service.get_agent_inbox(actors["qa"], "project-1")

    assert inbox["active_leases"][0]["ticket"]["ticket_id"] == "DEV-142"
    assert inbox["active_leases"][0]["lease_id"] == claimed["lease"]["lease_id"]


def test_inbox_recovers_an_expired_qa_lease_before_offering_work(
    service: ControlPlaneService,
    store,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    submitted = _submit_candidate(service, actors, seeded)
    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=1)
    claimed = service.claim_qa(
        actors["qa"], "DEV-142", submitted["ticket"]["version"], 900
    )
    _expire_lease(store, claimed["lease"]["lease_id"])

    inbox = service.get_agent_inbox(actors["qa"], "project-1")

    assert inbox["active_leases"] == []
    assert inbox["offers"][0]["ticket"]["ticket_id"] == "DEV-142"
    assert inbox["offers"][0]["ticket"]["status"] == TicketStatus.QA_READY.value


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


def test_cancelled_work_is_never_offered_again(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    """A retired ticket must leave every dispatch surface, including a live offer."""
    ticket = seeded["ticket"]
    admin = Actor("scheduler", Role.ADMIN, frozenset({"*"}))
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)
    before = service.get_agent_inbox(actors["developer"], "project-1")

    service.cancel_ticket(
        actors["planner"], ticket["ticket_id"], ticket["version"], "Approach abandoned."
    )
    after = service.get_agent_inbox(actors["developer"], "project-1")
    dispatch = service.dispatch_pending_work(admin, offer_seconds=90)

    assert [offer["ticket"]["ticket_id"] for offer in before["offers"]] == [ticket["ticket_id"]]
    assert after["offers"] == []
    assert after["offered_now"] == 0
    assert dispatch["offered_count"] == 0
    assert dispatch["pending_count"] == 0


def test_superseded_work_leaves_dispatch_but_its_replacement_is_offered(
    service: ControlPlaneService,
    actors: dict[str, Actor],
    seeded: dict[str, object],
) -> None:
    ticket = seeded["ticket"]
    replacement = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-950",
        title="Narrower retry fix",
        objective="Replaces the broad retry ticket",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["The narrower behavior is demonstrable"],
        preservation_constraints=[],
        file_scope=["src/checkout/retry/**"],
        ready=True,
    )
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)

    service.supersede_ticket(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        replacement["ticket_id"],
        "Replaced by a narrower ticket.",
    )
    inbox = service.get_agent_inbox(actors["developer"], "project-1")

    assert [offer["ticket"]["ticket_id"] for offer in inbox["offers"]] == ["DEV-950"]
