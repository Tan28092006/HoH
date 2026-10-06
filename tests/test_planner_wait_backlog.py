"""Review R3: a backlog of old Planner jobs never hides a new one.

The Planner wait used to read the 50 oldest READY jobs and skip the ones it had
already delivered, so 50 reports read but never resolved starved every later
job. Delivery reports also never closed. Both are fixed, and discovery is
bounded with a total count beside it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from hoh_control_plane import event_wait
from hoh_control_plane.domain import InterventionSeverity, PlannerJobType, Role, ValidationError
from hoh_control_plane.event_wait import wait_for_event


@pytest.fixture(autouse=True)
def _no_repository_observation(monkeypatch):
    monkeypatch.setitem(event_wait._REPOSITORY_OBSERVED_AT, "project-1", time.monotonic())


def _tickets(service, actors, count, prefix="JOB"):
    tickets = []
    for index in range(count):
        tickets.append(service.create_ticket(
            actors["planner"], feature_id="payment-retry", ticket_id=f"{prefix}-{index:03d}",
            title="Planning subject", objective="Something a Planner decides",
            required_role=Role.DEVELOPER, acceptance_criteria=["Decided"],
            preservation_constraints=[], file_scope=[f"src/{prefix.lower()}{index}/**"],
        ))
    return tickets


def _open_job(service, actors, ticket, *, job_type=PlannerJobType.QA_FAILURE,
              severity=InterventionSeverity.NORMAL, summary="Needs a decision"):
    with service.store.connection(immediate=True) as connection:
        return service._open_planner_job(
            connection, actors["planner"], "project-1", ticket, job_type=job_type,
            summary=summary, evidence_refs=[f"ticket:{ticket['ticket_id']}"], severity=severity,
        )


def _delivery_report(service, actors, ticket, mainline):
    with service.store.connection(immediate=True) as connection:
        return service._report_delivery_completed(
            connection, actors["planner"], "project-1", ticket,
            candidate_id=None, mainline_commit=mainline,
        )


def _mark_acknowledged(service, agent_id, jobs):
    with service.store.connection(immediate=True) as connection:
        for index, job in enumerate(jobs):
            current = connection.execute(
                "SELECT updated_at FROM planner_jobs WHERE planner_job_id = ?",
                (job["planner_job_id"],),
            ).fetchone()
            connection.execute(
                "INSERT INTO agent_event_deliveries VALUES (?,?,?,?,?,'acked',?,?)",
                (f"delivery_seed_{index}", agent_id, "project-1",
                 f"planner:{job['planner_job_id']}:{current['updated_at']}", "{}",
                 "2026-10-06T00:00:00Z", "2026-10-06T00:00:01Z"),
            )


def _register(service, actor):
    service.register_agent(actor, skills=[actor.role.value], capacity=1)


def test_fifty_acknowledged_jobs_do_not_hide_a_new_blocker(service, actors, seeded):
    _register(service, actors["planner"])
    old = [_open_job(service, actors, ticket) for ticket in _tickets(service, actors, 50)]
    _mark_acknowledged(service, actors["planner"].agent_id, old)
    newest = _open_job(service, actors, _tickets(service, actors, 1, prefix="NEW")[0],
                       job_type=PlannerJobType.DEVELOPER_BLOCKER)

    result = asyncio.run(wait_for_event(service, actors["planner"], "project-1", timeout_seconds=2))

    assert result["status"] == "event"
    assert result["event"]["planner_job_id"] == newest["planner_job_id"]


def test_a_job_with_new_evidence_is_offered_again_after_an_ack(service, actors, seeded):
    """The key carries updated_at: an acked job that changes is new again."""
    _register(service, actors["planner"])
    ticket = _tickets(service, actors, 1)[0]
    job = _open_job(service, actors, ticket)
    _mark_acknowledged(service, actors["planner"].agent_id, [job])
    time.sleep(0.01)
    _open_job(service, actors, ticket, summary="More evidence arrived")

    result = asyncio.run(wait_for_event(service, actors["planner"], "project-1", timeout_seconds=2))

    assert result["event"]["planner_job_id"] == job["planner_job_id"]


def test_an_unacknowledged_delivery_is_replayed_not_dropped(service, actors, seeded):
    _register(service, actors["planner"])
    job = _open_job(service, actors, _tickets(service, actors, 1)[0])

    async def scenario():
        first = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1)
        again = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1)
        return first, again

    first, again = asyncio.run(scenario())
    assert first["event"]["planner_job_id"] == job["planner_job_id"]
    assert again["event_id"] == first["event_id"] and again["replayed"] is True


def test_high_severity_jobs_lead_the_window(service, actors, seeded, monkeypatch):
    _register(service, actors["planner"])
    monkeypatch.setattr(event_wait, "PLANNER_JOB_EVENT_LIMIT", 3)
    for ticket in _tickets(service, actors, 5):
        _open_job(service, actors, ticket)
    urgent = _open_job(service, actors, _tickets(service, actors, 1, prefix="HOT")[0],
                       severity=InterventionSeverity.HIGH)

    with service.store.connection() as connection:
        events = event_wait._planner_job_events(connection, actors["planner"], "project-1")

    assert len(events) == 3
    assert events[0][1]["planner_job_id"] == urgent["planner_job_id"]


def test_acknowledging_a_delivery_report_closes_it(service, actors, seeded):
    _register(service, actors["planner"])
    report = _delivery_report(service, actors, seeded["ticket"], "c" * 40)
    assert report["status"] == "READY"

    async def scenario():
        delivered = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1)
        after = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=0.2,
                                     ack_event_id=delivered["event_id"])
        return delivered, after

    delivered, after = asyncio.run(scenario())
    assert delivered["event"]["planner_job_id"] == report["planner_job_id"]
    assert after["status"] == "timeout"
    closed = service.discover_planner_jobs(actors["planner"], "project-1", include_resolved=True)
    assert closed[0]["status"] == "RESOLVED"
    assert "acknowledged" in closed[0]["resolution"]


def test_acknowledging_a_decision_job_leaves_it_open(service, actors, seeded):
    _register(service, actors["planner"])
    job = _open_job(service, actors, _tickets(service, actors, 1)[0])

    async def scenario():
        delivered = await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=1)
        await wait_for_event(service, actors["planner"], "project-1", timeout_seconds=0.2,
                             ack_event_id=delivered["event_id"])

    asyncio.run(scenario())
    assert service.discover_planner_jobs(actors["planner"], "project-1")[0]["planner_job_id"] == (
        job["planner_job_id"]
    )


def test_intervention_ack_closes_a_report_but_not_a_claimed_one(service, actors, seeded):
    first = _delivery_report(service, actors, seeded["ticket"], "c" * 40)
    other = _tickets(service, actors, 1)[0]
    second = _delivery_report(service, actors, other, "d" * 40)
    service.claim_planner_job(actors["planner"], second["planner_job_id"])

    for intervention in service.list_planner_interventions(actors["planner"], "project-1"):
        service.acknowledge_planner_intervention(actors["planner"], intervention["intervention_id"])

    open_ids = {job["planner_job_id"] for job in service.discover_planner_jobs(actors["planner"], "project-1")}
    assert first["planner_job_id"] not in open_ids
    assert second["planner_job_id"] in open_ids


def test_discovery_is_capped_with_a_total_and_urgent_work_first(service, actors, seeded):
    for ticket in _tickets(service, actors, 12):
        _open_job(service, actors, ticket)
    urgent = _open_job(service, actors, _tickets(service, actors, 1, prefix="HOT")[0],
                       severity=InterventionSeverity.HIGH)

    jobs = service.discover_planner_jobs(actors["planner"], "project-1")
    view = service.discover_work(actors["planner"], "project-1")
    detail = service.discover_work(actors["planner"], "project-1", detail=True)

    assert len(jobs) == 10
    assert jobs[0]["planner_job_id"] == urgent["planner_job_id"]
    assert view["planning_job_count"] == 13 and len(view["planning_jobs"]) == 10
    assert detail["planning_job_count"] == 13
    assert len(service.discover_planner_jobs(actors["planner"], "project-1", limit=50)) == 13
    with pytest.raises(ValidationError):
        service.discover_planner_jobs(actors["planner"], "project-1", limit=0)
