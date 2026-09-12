"""What nothing is pointing at, computed rather than remembered (HOH-252)."""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    Role,
    ValidationError,
)


CRITERION = "Verified behaviour becomes a preservation constraint on later changes."
SOURCE = "HOH_MULTI_AGENT_CONTROL_PLANE.md §25.6"


def _register(service, planner, requirement_id: str = "spec-25.6", statement: str = CRITERION):
    return service.register_requirement(planner, "project-1", requirement_id, statement, SOURCE)


def _ticket(service, planner, ticket_id: str):
    return service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        objective="Serves a requirement",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=[f"src/{ticket_id}/**"],
    )


def _status(service, ticket_id: str, status: str) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET status = ? WHERE ticket_id = ?", (status, ticket_id)
        )


def _bucket(report, requirement_id: str) -> str:
    for name in ("uncovered", "in_flight", "satisfied"):
        if any(entry["requirement_id"] == requirement_id for entry in report[name]):
            return name
    return "absent"


# --- registration -------------------------------------------------------------


def test_a_requirement_records_what_was_asked_and_where_it_came_from(
    service, actors, seeded
) -> None:
    registered = _register(service, actors["planner"])

    assert registered["registered"] is True
    assert registered["statement"] == CRITERION
    assert registered["source_ref"] == SOURCE


def test_registering_the_same_requirement_twice_is_not_a_second_requirement(
    service, actors, seeded
) -> None:
    _register(service, actors["planner"])

    again = _register(service, actors["planner"])

    assert again["registered"] is False
    report = service.requirement_coverage(actors["planner"], "project-1")
    assert report["counts"]["total"] == 1


def test_only_a_planner_registers_requirements(service, actors, seeded) -> None:
    with pytest.raises(AuthorizationError):
        _register(service, actors["developer"])


# --- the gap report -----------------------------------------------------------


def test_a_requirement_nothing_points_at_is_reported_as_uncovered(
    service, actors, seeded
) -> None:
    """This is the whole point: the thing a Planner would otherwise have to recall."""
    _register(service, actors["planner"])

    report = service.requirement_coverage(actors["planner"], "project-1")

    assert _bucket(report, "spec-25.6") == "uncovered"
    assert report["counts"]["uncovered"] == 1


def test_a_requirement_with_unfinished_work_is_in_flight_not_satisfied(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    _register(service, planner)
    ticket = _ticket(service, planner, "DEV-601")
    service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])

    report = service.requirement_coverage(planner, "project-1")

    assert _bucket(report, "spec-25.6") == "in_flight"
    assert report["in_flight"][0]["tickets"] == ["DEV-601"]


def test_a_requirement_is_satisfied_only_when_its_work_is_done(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    _register(service, planner)
    ticket = _ticket(service, planner, "DEV-602")
    service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])
    _status(service, "DEV-602", "DONE")

    report = service.requirement_coverage(planner, "project-1")

    assert _bucket(report, "spec-25.6") == "satisfied"


def test_one_unfinished_ticket_keeps_the_whole_requirement_in_flight(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    _register(service, planner)
    for ticket_id in ("DEV-603", "DEV-604"):
        ticket = _ticket(service, planner, ticket_id)
        service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])
    _status(service, "DEV-603", "DONE")

    report = service.requirement_coverage(planner, "project-1")

    assert _bucket(report, "spec-25.6") == "in_flight"


def test_retiring_the_only_ticket_returns_the_requirement_to_uncovered(
    service, actors, seeded
) -> None:
    """Cancelled work covers nothing, and a gap that reappears must be visible."""
    planner = actors["planner"]
    _register(service, planner)
    ticket = _ticket(service, planner, "DEV-605")
    service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])
    assert _bucket(service.requirement_coverage(planner, "project-1"), "spec-25.6") == "in_flight"

    _status(service, "DEV-605", "CANCELLED")

    assert _bucket(service.requirement_coverage(planner, "project-1"), "spec-25.6") == "uncovered"


def test_satisfaction_cannot_be_declared_only_derived(service, actors, seeded) -> None:
    """No role can mark a requirement done without work the board agrees is done."""
    planner = actors["planner"]
    _register(service, planner)

    assert not hasattr(service, "satisfy_requirement")
    with service.store.connection() as connection:
        columns = {
            row["name"] for row in connection.execute('PRAGMA table_info("requirements")')
        }
    assert "status" not in columns, "there is no field to disagree with the board"
    assert "satisfied" not in columns


# --- linking ------------------------------------------------------------------


def test_a_link_to_an_unknown_requirement_is_refused(service, actors, seeded) -> None:
    """A typo must not invent a requirement and then report it as covered."""
    with pytest.raises(ValidationError) as error:
        service.link_requirement(
            actors["planner"], "spec-does-not-exist", ticket_id=seeded["ticket"]["ticket_id"]
        )

    assert error.value.details["reason"] == "requirement_unknown"


def test_linking_the_same_work_twice_is_harmless(service, actors, seeded) -> None:
    planner = actors["planner"]
    _register(service, planner)
    ticket = _ticket(service, planner, "DEV-606")

    service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])
    service.link_requirement(planner, "spec-25.6", ticket_id=ticket["ticket_id"])

    report = service.requirement_coverage(planner, "project-1")
    assert report["in_flight"][0]["tickets"] == ["DEV-606"]


def test_a_feature_can_carry_a_requirement_too(service, actors, seeded) -> None:
    planner = actors["planner"]
    _register(service, planner)

    linked = service.link_requirement(planner, "spec-25.6", feature_id="payment-retry")

    assert linked["subject_type"] == "feature"


def test_work_and_requirement_must_belong_to_the_same_project(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    admin = Actor("admin-01", Role.ADMIN, frozenset({"project-1", "other"}))
    service.create_project(
        admin, project_id="other", name="Other", description="", default_branch="main"
    )
    service.register_requirement(admin, "other", "other-req", "Something else", SOURCE)

    with pytest.raises(ConflictError):
        service.link_requirement(
            planner, "other-req", ticket_id=seeded["ticket"]["ticket_id"]
        )


def test_exactly_one_subject_is_required(service, actors, seeded) -> None:
    planner = actors["planner"]
    _register(service, planner)

    with pytest.raises(ValidationError):
        service.link_requirement(planner, "spec-25.6")
    with pytest.raises(ValidationError):
        service.link_requirement(
            planner, "spec-25.6", ticket_id="DEV-142", feature_id="payment-retry"
        )


# --- what the report costs ----------------------------------------------------


def test_the_report_is_a_list_of_gaps_not_a_copy_of_the_specification(
    service, actors, seeded
) -> None:
    """It has to be cheaper to consult than the source it points at."""
    planner = actors["planner"]
    for index in range(12):
        _register(service, planner, f"spec-{index}", f"Requirement {index}")

    report = service.requirement_coverage(planner, "project-1")

    assert set(report) == {"project_id", "uncovered", "in_flight", "satisfied", "counts"}
    for entry in report["uncovered"]:
        assert set(entry) == {"requirement_id", "statement", "source_ref", "tickets"}
    assert report["counts"]["uncovered"] == 12


def test_coverage_is_refused_outside_the_actor_s_projects(service, actors, seeded) -> None:
    outsider = Actor("outsider", Role.PLANNER, frozenset({"elsewhere"}))

    with pytest.raises(AuthorizationError):
        service.requirement_coverage(outsider, "project-1")
