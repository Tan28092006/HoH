"""A ticket keeps its write scope reserved while its candidate awaits QA or merge.

The reservation used to end with the Developer lease at submit, so an
overlapping ticket could start on a base the first candidate was about to
change, and its drift was predictable (wave 8E).
"""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import ConflictError, Role, TicketStatus


def _overlapping(service, actors, ticket_id="DEV-143", scope=("src/checkout/serializers/**",)):
    return service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id=ticket_id,
        title="Update checkout retry serializer",
        objective="Make the serializer preserve retry metadata",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Retry metadata survives serializer round trips"],
        preservation_constraints=[], file_scope=list(scope), ready=True,
    )


def _submitted(service, actors, seeded):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    return service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], "a" * 40, "b" * 40, "hoh/project-1/DEV-142/dev-01",
        ["Candidate is ready for QA."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], ticket["ticket_id"])["context_snapshot_id"])


def _claim(service, actors, ticket):
    return service.claim_ticket(actors["developer_2"], ticket["ticket_id"], ticket["version"], 900)


def test_a_submitted_candidate_keeps_its_scope_reserved(service, actors, seeded):
    submitted = _submitted(service, actors, seeded)
    assert submitted["ticket"]["status"] == TicketStatus.QA_READY.value
    overlapping = _overlapping(service, actors)

    with pytest.raises(ConflictError, match="write scope conflicts with active ticket DEV-142"):
        _claim(service, actors, overlapping)

    # Disjoint work is unaffected.
    disjoint = _overlapping(service, actors, "DEV-144", ("src/payments/metrics/**",))
    assert _claim(service, actors, disjoint)["ticket"]["status"] == TicketStatus.DEV_IN_PROGRESS.value


def test_the_reservation_holds_through_qa_and_ends_on_rework(service, actors, seeded):
    submitted = _submitted(service, actors, seeded)
    overlapping = _overlapping(service, actors)
    qa = service.claim_qa(actors["qa"], "DEV-142", submitted["ticket"]["version"], 900)
    assert qa["ticket"]["status"] == TicketStatus.QA_IN_PROGRESS.value

    with pytest.raises(ConflictError, match="write scope"):
        _claim(service, actors, overlapping)

    service.submit_qa_verdict(
        actors["qa"], "DEV-142", qa["lease"]["lease_id"],
        submitted["candidate"]["candidate_id"], qa["ticket"]["version"], "failed",
        [{"criterion": criterion, "status": "failed", "severity": "high",
          "observation": "Retry is not idempotent.",
          "evidence": [{"type": "test_result", "observation": "failed",
                        "storage_ref": f"evidence://qa/{index}.json"}]}
         for index, criterion in enumerate(seeded["ticket"]["acceptance_criteria"], start=1)],
        context_snapshot_id=service.get_ticket_context(actors["qa"], "DEV-142")["context_snapshot_id"],
    )

    # REWORK has no candidate awaiting anything; the next claim decides.
    assert _claim(service, actors, overlapping)["ticket"]["status"] == TicketStatus.DEV_IN_PROGRESS.value


@pytest.mark.parametrize("status,reserved", [
    (TicketStatus.QA_READY, True),
    (TicketStatus.QA_IN_PROGRESS, True),
    (TicketStatus.VERIFIED, True),
    (TicketStatus.MERGE_READY, True),
    (TicketStatus.REWORK, False),
    (TicketStatus.BLOCKED, False),
    (TicketStatus.DONE, False),
    (TicketStatus.CANCELLED, False),
])
def test_which_statuses_reserve_the_scope(service, actors, seeded, status, reserved):
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE tickets SET status = ? WHERE ticket_id = 'DEV-142'",
                           (status.value,))
    overlapping = _overlapping(service, actors)

    if reserved:
        with pytest.raises(ConflictError, match="write scope"):
            _claim(service, actors, overlapping)
    else:
        assert _claim(service, actors, overlapping)["lease"]["lease_id"]
