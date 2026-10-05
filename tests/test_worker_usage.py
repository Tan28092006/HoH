"""Dispatcher discipline is measured and bounded (wave 6G).

The rule that each ticket goes to a fresh worker was prose only. Execution roles
now report how they worked a ticket (work.report_usage), cycle_metrics reports
the share done by fresh workers and the tokens a ticket cost, and a waiting chat
that has claimed DISPATCHER_TICKET_LIMIT tickets in one session is advised to
start a new one. The advice never refuses anything.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from hoh_control_plane import contracts
from hoh_control_plane.domain import AuthorizationError, Role, ValidationError
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service.usage import DISPATCHER_TICKET_LIMIT


def _ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _claimed(service, actors, ticket_id: str = "DEV-142", developer: str = "developer") -> dict:
    return service.claim_ticket(
        actors[developer], ticket_id, _ticket(service, ticket_id)["version"], 900
    )


def test_a_worker_reports_how_it_worked_a_ticket_it_claimed(service, actors, seeded) -> None:
    _claimed(service, actors)

    report = service.report_usage(
        actors["developer"], "DEV-142", "fresh_subagent", tokens_in=12_000, tokens_out=3_000
    )

    assert report["worker"] == "fresh_subagent"
    assert (report["tokens_in"], report["tokens_out"]) == (12_000, 3_000)
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT agent_id, role, worker FROM ticket_usage_reports WHERE report_id = ?",
            (report["report_id"],),
        ).fetchone()
    assert tuple(row) == ("dev-01", "developer", "fresh_subagent")


def test_only_an_agent_that_claimed_the_ticket_may_report(service, actors, seeded) -> None:
    _claimed(service, actors)

    with pytest.raises(AuthorizationError) as stranger:
        service.report_usage(actors["developer_2"], "DEV-142", "dispatcher")
    assert stranger.value.details["reason"] == "usage_report_without_claim"
    with pytest.raises(AuthorizationError):
        service.report_usage(actors["planner"], "DEV-142", "dispatcher")
    with pytest.raises(ValidationError):
        service.report_usage(actors["developer"], "DEV-142", "subagent")
    with pytest.raises(ValidationError):
        service.report_usage(actors["developer"], "DEV-142", "dispatcher", tokens_in=-1)


@pytest.mark.anyio
async def test_report_usage_is_an_execution_role_tool_with_the_shared_schema(
    service, actors, seeded
) -> None:
    _claimed(service, actors)
    developer = create_mcp_server(service, actors["developer"])
    tools = {tool.name: tool for tool in await developer.list_tools()}
    planner_tools = {tool.name for tool in await create_mcp_server(service, actors["planner"]).list_tools()}

    schema = tools["work.report_usage"].inputSchema
    assert set(schema["properties"]["worker"]["enum"]) == contracts.WORKER_KINDS
    tokens = next(
        option for option in schema["properties"]["tokens_in"]["anyOf"]
        if option.get("type") == "integer"
    )
    assert tokens["maximum"] == contracts.MAX_REPORTED_TOKENS
    assert "work.report_usage" not in planner_tools
    assert "usage:report" in service.capabilities(actors["qa"])

    _, result = await developer.call_tool(
        "work.report_usage",
        {"ticket_id": "DEV-142", "worker": "new_session", "tokens_out": 900},
    )
    assert result["worker"] == "new_session" and result["tokens_in"] is None


def _seed_deliveries_with_usage(service) -> None:
    raw = sqlite3.connect(service.store.path)
    raw.execute("PRAGMA foreign_keys = OFF")
    try:
        for sequence, ticket_id in enumerate(("T-FRESH", "T-DISPATCH", "T-SILENT"), start=1):
            raw.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                     project_id, actor_id, actor_role, payload, occurred_at)
                VALUES (?, 'integration.completed', 'ticket', ?, 1, 'project-1', 'seed',
                        'integrator', '{}', ?)
                """,
                (f"evt_usage_{sequence}", ticket_id, f"2026-09-01T10:0{sequence}:00Z"),
            )
        for report_id, ticket_id, worker, tokens_in, tokens_out in (
            ("u1", "T-FRESH", "fresh_subagent", 1_000, 200),
            ("u2", "T-FRESH", "new_session", 500, 100),
            ("u3", "T-DISPATCH", "dispatcher", 3_000, None),
        ):
            raw.execute(
                """
                INSERT INTO ticket_usage_reports
                    (report_id, project_id, ticket_id, agent_id, role, worker,
                     tokens_in, tokens_out, reported_at)
                VALUES (?, 'project-1', ?, 'dev-01', 'developer', ?, ?, ?, '2026-09-01T10:00:00Z')
                """,
                (report_id, ticket_id, worker, tokens_in, tokens_out),
            )
        raw.commit()
    finally:
        raw.close()


def test_cycle_metrics_report_fresh_worker_share_and_tokens_per_ticket(
    service, actors, seeded
) -> None:
    _seed_deliveries_with_usage(service)

    report = service.cycle_metrics(actors["planner"], "project-1")

    usage = report["worker_usage"]
    assert (usage["reported_tickets"], usage["unreported_tickets"]) == (2, 1)
    assert usage["fresh_worker_share"] == 0.5
    # T-FRESH cost 1,800 tokens over two fresh rounds; T-DISPATCH 3,000.
    assert usage["tokens_per_ticket"] == {"count": 2, "p50": 2400.0, "p90": 2880.0}
    by_id = {ticket["ticket_id"]: ticket["usage"] for ticket in report["tickets"]}
    assert by_id["T-FRESH"]["by_worker"] == {"fresh_subagent": 1, "new_session": 1}
    assert by_id["T-DISPATCH"]["fresh_workers"] is False
    assert by_id["T-SILENT"] == {
        "reports": 0, "fresh_workers": None, "tokens_in": None, "tokens_out": None,
    }


def _open(service, actors, index: int) -> str:
    ticket_id = f"DEV-S{index}"
    service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id=ticket_id,
        title=f"Work {index}", objective="Small independent change",
        required_role=Role.DEVELOPER, acceptance_criteria=["It works"],
        preservation_constraints=["Checkout stays valid"],
        file_scope=[f"src/area{index}/**"], ready=True,
    )
    return ticket_id


def test_a_long_dispatcher_session_is_advised_to_restart(service, actors, seeded) -> None:
    from hoh_control_plane.event_wait import wait_for_event

    developer = actors["developer"]
    service.register_agent(developer, skills=["hoh-developer"], capacity=1)
    for index in range(DISPATCHER_TICKET_LIMIT - 1):
        _claimed(service, actors, _open(service, actors, index))
    assert service.dispatcher_advice(developer) is None

    _claimed(service, actors, _open(service, actors, DISPATCHER_TICKET_LIMIT))

    advice = service.dispatcher_advice(developer)
    assert advice["advice"] == "start_new_session"
    assert advice["session_tickets"] == DISPATCHER_TICKET_LIMIT
    waited = asyncio.run(wait_for_event(service, developer, "project-1", timeout_seconds=0.15))
    assert waited["status"] == "timeout"
    assert waited["advice"] == "start_new_session"
    assert waited["ticket_limit"] == DISPATCHER_TICKET_LIMIT
    # Advisory: the server still hands out and accepts work.
    _claimed(service, actors, _open(service, actors, DISPATCHER_TICKET_LIMIT + 1))
    # A new chat registers again, which starts a new session count.
    service.register_agent(developer, skills=["hoh-developer"], capacity=1)
    assert service.dispatcher_advice(developer) is None


def test_a_wait_on_the_agents_own_job_carries_no_advice(service, actors) -> None:
    from hoh_control_plane.event_wait import _with_dispatcher_advice

    class Advising:
        def dispatcher_advice(self, _actor):
            return {"advice": "start_new_session"}

    timeout = {"status": "timeout", "event_id": None, "event": None}
    assert _with_dispatcher_advice(Advising(), actors["qa"], timeout, "qa") == timeout
    assert _with_dispatcher_advice(
        Advising(), actors["qa"], {"status": "superseded"}, None
    ) == {"status": "superseded"}
    assert _with_dispatcher_advice(Advising(), actors["qa"], timeout, None)["advice"] == (
        "start_new_session"
    )
    assert json.dumps(timeout)  # untouched input
