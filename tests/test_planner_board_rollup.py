"""The Planner's board view: complete counts, no ticket rows, nothing finished.

Planner discovery is called at every planning boundary, so its cost is paid on
every cycle for the whole life of a project. It was also the one surface that
listed delivered work: DONE is not in CLOSED_TICKET_STATUSES, so every finished
ticket was carried in a payload whose stated purpose is handing out actionable
work. Counting the board instead of listing it made that visible, because the
counts disagreed with the database.
"""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.domain import Role, ValidationError


def _feature(rollup, feature_id: str) -> dict:
    return next(f for f in rollup["features"] if f["feature_id"] == feature_id)


@pytest.fixture
def board(service, actors, seeded):
    """One feature holding every lifecycle state a board has to distinguish."""
    planner = actors["planner"]
    feature = service.create_feature(
        planner,
        project_id="project-1",
        feature_id="settlement",
        title="Settlement",
        objective="Settle payments once",
    )
    for index, (ticket_id, priority) in enumerate(
        [("SET-1", 90), ("SET-2", 40), ("SET-3", 60)]
    ):
        service.create_ticket(
            planner,
            feature_id=feature["feature_id"],
            ticket_id=ticket_id,
            title=f"Settlement work {index}",
            objective="Bounded outcome",
            required_role=Role.DEVELOPER,
            acceptance_criteria=["It settles once"],
            preservation_constraints=[],
            file_scope=["src/settlement/**"],
            priority=priority,
            ready=ticket_id != "SET-3",
        )
    return {"feature": feature, "seeded_ticket": seeded["ticket"]}


def test_a_planner_asking_the_broad_question_gets_counts_not_rows(
    service, actors, board
) -> None:
    rollup = service.discover_work(actors["planner"], "project-1")

    assert rollup["view"] == "feature_rollup"
    assert rollup["next_cursor"] is None
    settlement = _feature(rollup, "settlement")
    assert "tickets" not in settlement
    assert settlement["ticket_counts"] == {"READY": 2, "BACKLOG": 1}
    assert settlement["open_ticket_count"] == 3
    assert settlement["top_priority"] == 90
    assert settlement["context_ref"].startswith("ctx:feature:settlement:v")
    assert rollup["detail_call"]["tool"] == "work.discover"
    # No ticket body, and no ticket identifier either: the feature reference is
    # what the Planner resolves next.
    assert "Bounded outcome" not in json.dumps(rollup)
    assert "SET-1" not in json.dumps(rollup)


def test_the_rollup_names_the_role_each_feature_is_waiting_on(
    service, actors, board
) -> None:
    planner = actors["planner"]
    claim = service.claim_ticket(actors["developer"], "SET-1", 1, 900)
    service.report_blocker(
        actors["developer"], "SET-1", claim["lease"]["lease_id"],
        claim["ticket"]["version"], "Waiting on the settlement sandbox.",
    )

    rollup = service.discover_work(planner, "project-1")

    settlement = _feature(rollup, "settlement")
    assert settlement["awaiting"]["planner"] == 2, "one BACKLOG plus one BLOCKED"
    assert settlement["awaiting"]["developer"] == 1, "the remaining READY ticket"
    assert settlement["blocked_ticket_count"] == 1


def test_counts_cover_the_whole_board_not_one_page(service, actors, seeded) -> None:
    """A total that inherited the row view's page cap would under-report."""
    planner = actors["planner"]
    for index in range(12):
        service.create_ticket(
            planner,
            feature_id=seeded["feature"]["feature_id"],
            ticket_id=f"PAGE-{index}",
            title=f"Paged {index}",
            objective="Bounded outcome",
            required_role=Role.DEVELOPER,
            acceptance_criteria=["It works"],
            preservation_constraints=[],
            file_scope=["src/**"],
            ready=True,
        )

    rollup = service.discover_work(planner, "project-1", limit=3)
    paged = service.discover_work(planner, "project-1", limit=3, detail=True)

    assert rollup["open_ticket_count"] == 13, "12 paged tickets plus the seeded one"
    assert paged["next_cursor"], "the row view still pages"
    assert sum(len(f["tickets"]) for f in paged["features"]) == 3


def test_work_waiting_on_a_dependency_is_counted_apart_never_hidden(
    service, actors, seeded
) -> None:
    """A feature must not look finished because its remainder is blocked."""
    planner = actors["planner"]
    service.create_ticket(
        planner,
        feature_id=seeded["feature"]["feature_id"],
        ticket_id="DEP-2",
        title="Depends on the first",
        objective="Bounded outcome",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It follows"],
        preservation_constraints=[],
        file_scope=["src/**"],
        dependency_ids=[seeded["ticket"]["ticket_id"]],
        ready=True,
    )

    rollup = service.discover_work(planner, "project-1")
    rows = service.discover_work(planner, "project-1", detail=True)

    feature = _feature(rollup, seeded["feature"]["feature_id"])
    assert feature["open_ticket_count"] == 2
    assert feature["dependency_blocked_count"] == 1
    # Not attributed to a role: nobody can claim it, so counting it as work a
    # Developer is sitting on would misroute the Planner.
    assert feature["awaiting"].get("developer") == 1
    listed = {t["ticket_id"] for f in rows["features"] for t in f["tickets"]}
    assert "DEP-2" not in listed, "the row view still only offers claimable work"


# ---------------------------------------------------------------------------
# Delivered work is not somewhere to look.


def _deliver(service, actors, ticket_id: str) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET status = 'DONE', version = version + 1 WHERE ticket_id = ?",
            (ticket_id,),
        )


def test_delivered_tickets_leave_the_discovery_surface(service, actors, board) -> None:
    planner = actors["planner"]
    _deliver(service, actors, "SET-1")

    rollup = service.discover_work(planner, "project-1")
    rows = service.discover_work(planner, "project-1", detail=True)

    settlement = _feature(rollup, "settlement")
    assert "DONE" not in settlement["ticket_counts"]
    assert settlement["open_ticket_count"] == 2
    assert "SET-1" not in {t["ticket_id"] for f in rows["features"] for t in f["tickets"]}


def test_a_delivered_ticket_is_still_reachable_when_asked_for_by_status(
    service, actors, board
) -> None:
    """Excluded from the default surface, never made unreachable."""
    planner = actors["planner"]
    _deliver(service, actors, "SET-1")

    done = service.discover_work(planner, "project-1", status=["DONE"])

    assert done["view"] == "tickets", "a named status is a request for rows"
    assert {t["ticket_id"] for f in done["features"] for t in f["tickets"]} == {"SET-1"}


# ---------------------------------------------------------------------------
# Which view a call gets.


def test_any_narrowing_argument_returns_rows(service, actors, board) -> None:
    planner = actors["planner"]
    narrowing = [
        {"feature_id": "settlement"},
        {"status": ["READY"]},
        {"awaiting_role": "developer"},
        {"priority_min": 50},
        {"blocked_only": True},
        {"unassigned_only": True},
    ]

    for kwargs in narrowing:
        result = service.discover_work(planner, "project-1", **kwargs)
        assert result["view"] == "tickets", kwargs
        assert all("tickets" in feature for feature in result["features"]), kwargs

    assert service.discover_work(planner, "project-1")["view"] == "feature_rollup"
    assert service.discover_work(
        planner, "project-1", detail=True
    )["view"] == "tickets"
    assert service.discover_work(
        planner, "project-1", feature_id="settlement", detail=False
    )["view"] == "feature_rollup", "an explicit detail=false still rolls up"


def test_the_rollup_still_carries_the_planning_queue(service, actors, board) -> None:
    """The Planner's cycle reads its jobs from this call before anything else."""
    claim = service.claim_ticket(actors["developer"], "SET-1", 1, 900)
    service.report_blocker(
        actors["developer"], "SET-1", claim["lease"]["lease_id"],
        claim["ticket"]["version"], "Waiting on the settlement sandbox.",
    )

    rollup = service.discover_work(actors["planner"], "project-1")

    assert [job["job_type"] for job in rollup["planning_jobs"]] == ["developer_blocker"]


def test_execution_roles_always_get_rows(service, actors, board) -> None:
    """Their question is which ticket to claim, which a count cannot answer."""
    for role in ("developer", "qa", "integrator"):
        result = service.discover_work(actors[role], "project-1")
        assert result["view"] == "tickets", role
        assert "detail_call" not in result


def test_detail_must_be_a_boolean(service, actors, board) -> None:
    with pytest.raises(ValidationError, match="detail"):
        service.discover_work(actors["planner"], "project-1", detail="yes")


@pytest.mark.anyio
async def test_the_mcp_tool_exposes_detail_and_returns_the_rollup(
    service, actors, board
) -> None:
    from hoh_control_plane.mcp_server import create_mcp_server

    server = create_mcp_server(service, actors["planner"])
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "detail" in tools["work.discover"].inputSchema["properties"]

    _content, rollup = await server.call_tool(
        "work.discover", {"project_id": "project-1"}
    )
    _content, rows = await server.call_tool(
        "work.discover", {"project_id": "project-1", "detail": True}
    )

    assert rollup["view"] == "feature_rollup"
    assert rows["view"] == "tickets"
    assert len(json.dumps(rollup)) < len(json.dumps(rows))
