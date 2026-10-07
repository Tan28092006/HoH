"""Review A6: a barrier never hides urgent Planner work, and no offer is unclaimable.

A pending reconciliation used to be the only thing a Planner wait returned, so a
high-severity blocker waited behind it for as long as the barrier stood. And a
READY ticket whose dependencies were unfinished was still offered: twenty such
tickets at high priority filled the inbox window, and the claimable low-priority
work behind them never reached a Developer.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from hoh_control_plane import event_wait
from hoh_control_plane.domain import Actor, InterventionSeverity, PlannerJobType, Role
from hoh_control_plane.event_wait import _pending, wait_for_event


@pytest.fixture(autouse=True)
def _no_repository_observation(monkeypatch):
    monkeypatch.setitem(event_wait._REPOSITORY_OBSERVED_AT, "project-1", time.monotonic())


def _open_barrier(service):
    opened = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO repository_reconciliations
                (reconciliation_id, project_id, observation_ref, observed_commits,
                 ticket_versions, status, created_at)
            VALUES ('recon_a6', 'project-1', 'git:observed:a6', ?, '{}', 'PENDING', ?)
            """,
            (json.dumps([{"commit": "c" * 40}]), opened),
        )


def _ticket(service, actors, ticket_id, *, ready=True, priority=None, depends=None):
    created = service.create_ticket(
        actors["planner"], "payment-retry", ticket_id, "Subject", "Objective",
        Role.DEVELOPER, ["Done"], [], [f"src/{ticket_id.lower()}/**"],
        ready=ready, dependency_ids=depends or [],
    )
    if priority is not None:
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE tickets SET priority = ? WHERE ticket_id = ?",
                               (priority, ticket_id))
            connection.execute("UPDATE work_queue SET priority = ? WHERE ticket_id = ?",
                               (priority, ticket_id))
    return created


def _job(service, actors, ticket, severity):
    with service.store.connection(immediate=True) as connection:
        return service._open_planner_job(
            connection, actors["planner"], "project-1", ticket,
            job_type=PlannerJobType.DEVELOPER_BLOCKER, summary="Blocked",
            evidence_refs=[f"ticket:{ticket['ticket_id']}"], severity=severity,
        )


# --- a barrier and urgent Planner work -------------------------------------


def test_a_barrier_carries_high_severity_jobs_but_not_normal_ones(service, actors, seeded):
    service.register_agent(actors["planner"], skills=["hoh-planner"], capacity=1)
    urgent = _job(service, actors, _ticket(service, actors, "HOT-1"), InterventionSeverity.HIGH)
    _job(service, actors, _ticket(service, actors, "COLD-1"), InterventionSeverity.NORMAL)
    _open_barrier(service)

    events = _pending(service, actors["planner"], "project-1")

    assert [payload["kind"] for _key, payload in events] == ["repository_changed", "planner_work"]
    assert events[1][1]["planner_job_id"] == urgent["planner_job_id"]


def test_the_urgent_job_arrives_once_the_barrier_event_is_acknowledged(service, actors, seeded):
    service.register_agent(actors["planner"], skills=["hoh-planner"], capacity=1)
    urgent = _job(service, actors, _ticket(service, actors, "HOT-1"), InterventionSeverity.HIGH)
    _open_barrier(service)

    async def scenario():
        barrier = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1)
        following = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1,
                                         ack_event_id=barrier["event_id"])
        return barrier, following

    barrier, following = asyncio.run(scenario())
    assert barrier["event"]["kind"] == "repository_changed"
    assert following["event"]["kind"] == "planner_work"
    assert following["event"]["planner_job_id"] == urgent["planner_job_id"]


def test_a_planner_at_capacity_is_offered_only_the_barrier(service, actors, seeded):
    service.register_agent(actors["planner"], skills=["hoh-planner"], capacity=1)
    held = _job(service, actors, _ticket(service, actors, "HELD-1"), InterventionSeverity.NORMAL)
    service.claim_planner_job(actors["planner"], held["planner_job_id"])
    _job(service, actors, _ticket(service, actors, "HOT-1"), InterventionSeverity.HIGH)
    _open_barrier(service)

    events = _pending(service, actors["planner"], "project-1")

    assert [payload["kind"] for _key, payload in events] == ["repository_changed"]


def test_an_execution_role_still_sees_only_the_barrier(service, actors, seeded):
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)
    _job(service, actors, _ticket(service, actors, "HOT-1"), InterventionSeverity.HIGH)
    _open_barrier(service)

    events = _pending(service, actors["developer"], "project-1")

    assert [payload["kind"] for _key, payload in events] == ["repository_reconciliation_required"]


# --- offers and unfinished dependencies -------------------------------------


def test_blocked_high_priority_tickets_do_not_crowd_out_claimable_work(service, actors, seeded):
    _ticket(service, actors, "BASE-1", ready=False)
    for index in range(3):
        _ticket(service, actors, f"WAIT-{index}", priority=90, depends=["BASE-1"])
    _ticket(service, actors, "FREE-1", priority=1)
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)

    inbox = service.get_agent_inbox(actors["developer"], "project-1", limit=2)

    offered = {offer["ticket"]["ticket_id"] for offer in inbox["offers"]}
    assert "FREE-1" in offered
    assert not any(ticket_id.startswith("WAIT-") for ticket_id in offered)


def test_a_ticket_is_offered_once_its_dependency_is_done(service, actors, seeded):
    _ticket(service, actors, "BASE-1", ready=False)
    _ticket(service, actors, "WAIT-1", depends=["BASE-1"])
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)
    before = service.get_agent_inbox(actors["developer"], "project-1")
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE tickets SET status = 'DONE' WHERE ticket_id = 'BASE-1'")

    after = service.get_agent_inbox(actors["developer"], "project-1")

    assert "WAIT-1" not in {offer["ticket"]["ticket_id"] for offer in before["offers"]}
    assert "WAIT-1" in {offer["ticket"]["ticket_id"] for offer in after["offers"]}


def test_an_offer_made_before_a_dependency_was_added_is_withheld(service, actors, seeded):
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)
    first = service.get_agent_inbox(actors["developer"], "project-1")
    assert [offer["ticket"]["ticket_id"] for offer in first["offers"]] == ["DEV-142"]
    _ticket(service, actors, "BASE-1", ready=False)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id) VALUES ('DEV-142', 'BASE-1')"
        )

    again = service.get_agent_inbox(actors["developer"], "project-1")

    assert again["offers"] == []


def test_dispatch_fanout_skips_a_ticket_with_unfinished_dependencies(service, actors, seeded):
    _ticket(service, actors, "BASE-1", ready=False)
    _ticket(service, actors, "WAIT-1", depends=["BASE-1"])
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=3)
    admin = Actor("scheduler", Role.ADMIN, frozenset({"*"}))

    service.dispatch_pending_work(admin, offer_seconds=90)

    with service.store.connection() as connection:
        offered = {
            row["ticket_id"] for row in connection.execute(
                "SELECT q.ticket_id FROM work_offers o JOIN work_queue q ON q.queue_id = o.queue_id "
                "WHERE o.status = 'offered'"
            )
        }
    assert "DEV-142" in offered and "WAIT-1" not in offered
