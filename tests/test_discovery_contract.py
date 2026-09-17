"""work.discover must hand out bounded metadata, never ticket bodies (HOH-241)."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.domain import Actor, Role, ValidationError

# Everything a worker must read before acting. Discovery may never inline these:
# they are reachable only through the typed ticket.get_context reference.
BODY_FIELDS = (
    "objective",
    "acceptance_criteria",
    "preservation_constraints",
    "file_scope",
    "blocked_reason",
    "base_commit",
    "candidate_commit",
)


def _populate(service, actors, seeded, count: int = 12) -> None:
    """Add enough tickets across two features to exercise grouping and paging."""
    planner = actors["planner"]
    service.create_feature(
        planner,
        project_id="project-1",
        feature_id="settlement",
        title="Settlement",
        objective="Settle captured payments",
    )
    for index in range(count):
        feature_id = "payment-retry" if index % 2 else "settlement"
        service.create_ticket(
            planner,
            feature_id=feature_id,
            ticket_id=f"GEN-{index:03d}",
            title=f"Generated ticket {index}",
            objective="An objective long enough that inlining it would be obvious " * 8,
            required_role=Role.DEVELOPER,
            acceptance_criteria=["A criterion that discovery must not inline"],
            preservation_constraints=["A constraint that discovery must not inline"],
            file_scope=[f"src/generated/module_{index}.py"],
            priority=50 + index,
        )


def test_discovery_returns_metadata_and_refs_not_ticket_bodies(
    service, actors, seeded
) -> None:
    """The Planner payload is the worst case: unnarrowed by required_role."""
    _populate(service, actors, seeded)

    discovered = service.discover_work(actors["planner"], "project-1")

    payload = json.dumps(discovered)
    for field in BODY_FIELDS:
        assert f'"{field}"' not in payload, f"discovery inlined {field}"
    assert "An objective long enough" not in payload
    for feature in discovered["features"]:
        assert feature["pending_ticket_count"] == len(feature["tickets"])
        assert isinstance(feature["blocked_ticket_count"], int)
        for ticket in feature["tickets"]:
            assert ticket["ticket_ref"] == f"ticket:{ticket['ticket_id']}"
            # A Planner row carries identity and routing only: the one call it
            # could recommend is ticket.get_context on the ticket_ref beside it.
            assert set(ticket) == {
                "ticket_id", "ticket_ref", "feature_id", "title", "ticket_type",
                "status", "priority", "required_role", "version", "context_ref",
            }


def test_discovery_stays_small_next_to_the_full_ticket_context(
    service, actors, seeded
) -> None:
    """Discovery must cost far less than resolving the tickets it points at."""
    _populate(service, actors, seeded)
    planner = actors["planner"]

    discovered = service.discover_work(planner, "project-1")

    refs = [t["ticket_ref"] for f in discovered["features"] for t in f["tickets"]]
    one_packet = service.get_ticket_context(planner, refs[0])
    assert len(json.dumps(discovered)) < len(json.dumps(one_packet)) * len(refs)


def test_status_and_feature_and_priority_filters_narrow_the_result(
    service, actors, seeded
) -> None:
    _populate(service, actors, seeded)
    planner = actors["planner"]

    backlog = service.discover_work(planner, "project-1", status=["BACKLOG"])
    scoped = service.discover_work(planner, "project-1", feature_id="settlement")
    important = service.discover_work(planner, "project-1", priority_min=58)

    assert {t["status"] for f in backlog["features"] for t in f["tickets"]} == {"BACKLOG"}
    assert {f["feature_id"] for f in scoped["features"]} == {"settlement"}
    assert all(
        t["priority"] >= 58 for f in important["features"] for t in f["tickets"]
    )


def test_awaiting_role_selects_the_states_that_role_must_act_on(
    service, actors, seeded
) -> None:
    _populate(service, actors, seeded)
    planner = actors["planner"]
    ticket = service.discover_work(planner, "project-1", status=["BACKLOG"])
    first = ticket["features"][0]["tickets"][0]
    service.set_ticket_ready(planner, first["ticket_id"], first["version"])

    awaiting = service.discover_work(planner, "project-1", awaiting_role="developer")

    states = {t["status"] for f in awaiting["features"] for t in f["tickets"]}
    assert states <= {"READY", "REWORK"}
    assert first["ticket_id"] in {
        t["ticket_id"] for f in awaiting["features"] for t in f["tickets"]
    }


def test_unassigned_only_hides_a_ticket_under_an_active_lease(
    service, actors, seeded
) -> None:
    planner, developer = actors["planner"], actors["developer"]
    ticket = seeded["ticket"]
    claimed = service.claim_ticket(
        developer, ticket["ticket_id"], ticket["version"], 600
    )
    assert claimed["lease"]["lease_id"]

    free = service.discover_work(planner, "project-1", unassigned_only=True)

    assert ticket["ticket_id"] not in {
        t["ticket_id"] for f in free["features"] for t in f["tickets"]
    }


def test_cursor_pages_every_ticket_exactly_once_while_rows_are_inserted(
    service, actors, seeded
) -> None:
    """Keyset paging: a ticket created mid-walk must not hide an unseen row."""
    _populate(service, actors, seeded)
    planner = actors["planner"]
    seen: list[str] = []
    cursor = None
    pages = 0

    while True:
        page = service.discover_work(planner, "project-1", limit=3, cursor=cursor)
        seen.extend(t["ticket_id"] for f in page["features"] for t in f["tickets"])
        cursor = page["next_cursor"]
        pages += 1
        if pages == 1:
            service.create_ticket(
                planner,
                feature_id="settlement",
                ticket_id="GEN-LATE",
                title="Inserted between pages",
                objective="Arrives after paging started",
                required_role=Role.DEVELOPER,
                acceptance_criteria=["Paging stays stable"],
                preservation_constraints=[],
                file_scope=["src/generated/late.py"],
                priority=1,
            )
        if cursor is None:
            break

    assert len(seen) == len(set(seen)), "cursor paging returned a duplicate"
    every = service.discover_work(planner, "project-1", limit=100)
    original = {t["ticket_id"] for f in every["features"] for t in f["tickets"]}
    assert original - {"GEN-LATE"} <= set(seen)


def test_role_scoping_and_blocked_only_are_unchanged(service, actors, seeded) -> None:
    planner, developer = actors["planner"], actors["developer"]
    _populate(service, actors, seeded)
    ticket = seeded["ticket"]

    for_dev = service.discover_work(developer, "project-1")
    assert {t["status"] for f in for_dev["features"] for t in f["tickets"]} <= {
        "READY", "REWORK"
    }
    assert all(
        t["required_role"] == "developer"
        for f in for_dev["features"]
        for t in f["tickets"]
    )

    claimed = service.claim_ticket(
        developer, ticket["ticket_id"], ticket["version"], 600
    )
    service.report_blocker(
        developer,
        ticket["ticket_id"],
        claimed["lease"]["lease_id"],
        claimed["ticket"]["version"],
        "Waiting on an upstream contract decision",
    )
    blocked = service.discover_work(planner, "project-1", blocked_only=True)
    assert {t["status"] for f in blocked["features"] for t in f["tickets"]} == {"BLOCKED"}
    assert sum(f["blocked_ticket_count"] for f in blocked["features"]) == 1


def test_qa_and_integrator_keep_their_own_status_windows(service, actors, seeded) -> None:
    for role, allowed in (
        ("qa", {"QA_READY"}),
        ("integrator", {"VERIFIED", "MERGE_READY"}),
    ):
        discovered = service.discover_work(actors[role], "project-1")
        states = {t["status"] for f in discovered["features"] for t in f["tickets"]}
        assert states <= allowed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": ["NOT_A_STATUS"]},
        {"status": []},
        {"awaiting_role": "planner"},
        {"awaiting_role": "not-a-role"},
        {"cursor": "not-a-cursor"},
    ],
)
def test_invalid_discovery_filters_are_refused(service, actors, seeded, kwargs) -> None:
    with pytest.raises(ValidationError):
        service.discover_work(actors["planner"], "project-1", **kwargs)


def test_discovery_refuses_a_project_outside_the_actor_scope(service, actors) -> None:
    outsider = Actor("dev-99", Role.DEVELOPER, frozenset({"other-project"}))
    with pytest.raises(Exception):
        service.discover_work(outsider, "project-1")
