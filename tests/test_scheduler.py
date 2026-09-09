from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hoh_control_plane.domain import Actor, AuthorizationError, Role, TicketStatus
from hoh_control_plane.scheduler import sweep_once


def _scheduler() -> Actor:
    return Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))


def _expire(store, lease_id: str) -> None:
    with store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease_id),
        )


def test_scheduler_requeues_an_expired_developer_lease(
    service, store, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    _expire(store, claim["lease"]["lease_id"])

    swept = sweep_once(store.path)
    context = service.get_ticket_context(actors["developer_2"], ticket["ticket_id"])

    assert swept["count"] == 1
    assert swept["requeued"][0]["status"] == TicketStatus.READY.value
    assert context["ticket"]["status"] == TicketStatus.READY.value
    assert context["ticket"]["version"] == claim["ticket"]["version"] + 1


def test_scheduler_requeues_an_expired_qa_lease_to_qa_ready(
    service, store, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    development = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
        "a" * 40,
        "b" * 40,
        "hoh/project-1/DEV-142/dev-01",
        ["Candidate is ready for QA."],
    )
    qa = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    _expire(store, qa["lease"]["lease_id"])

    swept = service.sweep_expired_leases(_scheduler())
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert swept["requeued"][0]["status"] == TicketStatus.QA_READY.value
    assert context["ticket"]["status"] == TicketStatus.QA_READY.value


def test_only_the_internal_admin_scheduler_can_sweep_leases(service, actors) -> None:
    try:
        service.sweep_expired_leases(actors["developer"])
    except AuthorizationError:
        pass
    else:
        raise AssertionError("Developer must not sweep every agent lease")
