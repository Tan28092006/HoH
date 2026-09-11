"""What a feature already proved travels to the work that follows it (HOH-250)."""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import Role
from hoh_control_plane.service._common import _MAX_INHERITED_CONSTRAINTS


def _ticket(service, planner, ticket_id: str, *, criteria, constraints=None):
    return service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        objective=f"Objective for {ticket_id}",
        required_role=Role.DEVELOPER,
        acceptance_criteria=criteria,
        preservation_constraints=constraints or [],
        file_scope=[f"src/{ticket_id}/**"],
    )


def _mark_done(service, ticket_id: str) -> None:
    """Put a ticket in a state whose criteria count as proven behaviour."""
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET status = 'DONE' WHERE ticket_id = ?", (ticket_id,)
        )


def _inherited(service, actor, ticket_id: str) -> list[dict]:
    return service.get_ticket_context(actor, ticket_id, 60_000)["inherited_constraints"]


def test_a_feature_with_nothing_proven_yet_inherits_nothing(
    service, actors, seeded
) -> None:
    assert _inherited(service, actors["developer"], seeded["ticket"]["ticket_id"]) == []


def test_proven_behaviour_reaches_the_ticket_that_follows(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-301", criteria=["A refund reaches the card within a day"])
    _mark_done(service, "DEV-301")
    later = _ticket(service, planner, "DEV-302", criteria=["Its own criterion"])

    inherited = _inherited(service, actors["developer"], later["ticket_id"])

    assert [entry["behaviour"] for entry in inherited] == [
        "A refund reaches the card within a day"
    ]
    assert inherited[0]["verified_by"] == "DEV-301"


def test_evidence_is_kept_apart_from_judgement(service, actors, seeded) -> None:
    """A reader must be able to tell what was proven from what was decided."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-303", criteria=["A refund reaches the card within a day"])
    _mark_done(service, "DEV-303")
    later = _ticket(
        service, planner, "DEV-304",
        criteria=["Its own criterion"],
        constraints=["A constraint the Planner judged necessary"],
    )

    packet = service.get_ticket_context(actors["developer"], later["ticket_id"], 60_000)

    assert packet["ticket"]["preservation_constraints"] == [
        "A constraint the Planner judged necessary"
    ], "what the Planner wrote is untouched"
    assert [entry["behaviour"] for entry in packet["inherited_constraints"]] == [
        "A refund reaches the card within a day"
    ]


def test_a_behaviour_the_planner_already_stated_is_not_repeated(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    shared = "A refund reaches the card within a day"
    _ticket(service, planner, "DEV-305", criteria=[shared])
    _mark_done(service, "DEV-305")
    later = _ticket(
        service, planner, "DEV-306", criteria=["Its own criterion"], constraints=[shared]
    )

    inherited = _inherited(service, actors["developer"], later["ticket_id"])

    assert inherited == [], "saying it twice does not make it truer"


def test_the_same_behaviour_proven_twice_is_carried_once(service, actors, seeded) -> None:
    planner = actors["planner"]
    shared = "A refund reaches the card within a day"
    for ticket_id in ("DEV-307", "DEV-308"):
        _ticket(service, planner, ticket_id, criteria=[shared])
        _mark_done(service, ticket_id)
    later = _ticket(service, planner, "DEV-309", criteria=["Its own criterion"])

    inherited = _inherited(service, actors["developer"], later["ticket_id"])

    assert len(inherited) == 1


def test_a_long_lived_feature_does_not_swamp_a_new_ticket(service, actors, seeded) -> None:
    planner = actors["planner"]
    for index in range(_MAX_INHERITED_CONSTRAINTS + 5):
        ticket_id = f"DEV-4{index:02d}"
        _ticket(service, planner, ticket_id, criteria=[f"Proven behaviour {index}"])
        _mark_done(service, ticket_id)
    later = _ticket(service, planner, "DEV-499", criteria=["Its own criterion"])

    inherited = _inherited(service, actors["developer"], later["ticket_id"])

    assert len(inherited) == _MAX_INHERITED_CONSTRAINTS


def test_behaviour_proven_after_the_ticket_existed_still_reaches_it(
    service, actors, seeded
) -> None:
    """Otherwise work already in flight is checked against a stale picture."""
    planner = actors["planner"]
    later = _ticket(service, planner, "DEV-310", criteria=["Its own criterion"])
    assert _inherited(service, actors["developer"], later["ticket_id"]) == []

    _ticket(service, planner, "DEV-311", criteria=["Proven only afterwards"])
    _mark_done(service, "DEV-311")

    inherited = _inherited(service, actors["developer"], later["ticket_id"])
    assert [entry["behaviour"] for entry in inherited] == ["Proven only afterwards"]


def test_a_ticket_does_not_inherit_from_itself(service, actors, seeded) -> None:
    planner = actors["planner"]
    own = _ticket(service, planner, "DEV-312", criteria=["Its own criterion"])
    _mark_done(service, "DEV-312")

    inherited = _inherited(service, actors["developer"], own["ticket_id"])

    assert inherited == []


def test_qa_sees_what_the_change_must_not_break(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-313", criteria=["A refund reaches the card within a day"])
    _mark_done(service, "DEV-313")
    later = _ticket(service, planner, "DEV-314", criteria=["Its own criterion"])

    inherited = _inherited(service, actors["qa"], later["ticket_id"])

    assert [entry["behaviour"] for entry in inherited] == [
        "A refund reaches the card within a day"
    ]


def test_unproven_work_is_not_treated_as_proven(service, actors, seeded) -> None:
    """Only what QA actually verified travels; a plan is not evidence."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-315", criteria=["Merely planned, never verified"])
    later = _ticket(service, planner, "DEV-316", criteria=["Its own criterion"])

    assert _inherited(service, actors["developer"], later["ticket_id"]) == []


def test_the_ticket_s_own_constraints_outrank_what_it_inherited(
    service, actors, seeded
) -> None:
    """Under budget pressure, inherited context is what gives way."""
    planner = actors["planner"]
    for index in range(6):
        ticket_id = f"DEV-5{index:02d}"
        _ticket(
            service, planner, ticket_id,
            criteria=[f"Proven behaviour {index} " + "stated at length. " * 20],
        )
        _mark_done(service, ticket_id)
    later = _ticket(
        service, planner, "DEV-599",
        criteria=["Its own criterion"],
        constraints=["A constraint the Planner judged necessary"],
    )

    packet = service.get_ticket_context(actors["developer"], later["ticket_id"], 900)

    assert packet["ticket"]["preservation_constraints"] == [
        "A constraint the Planner judged necessary"
    ]
    assert len(packet.get("inherited_constraints") or []) < 6
