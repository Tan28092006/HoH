"""Reconciliation decisions are a typed MCP payload, not a free-form dict (wave 8D, A1).

A wrong key or disposition used to cost a validation_error round trip while the
whole board was frozen. The schema now names every key and the six values, and
refuses anything else before the service runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from helpers.repository import _tracked_repository

from hoh_control_plane import contracts
from hoh_control_plane.domain import ValidationError
from hoh_control_plane.mcp_server import create_mcp_server

TOOL = "planner.resolve_repository_reconciliation"


@pytest.fixture
def pending(service, actors, seeded, tmp_path: Path) -> dict:
    """A project-wide reconciliation opened by the first observation of main."""
    planner = actors["planner"]
    repository, _remote = _tracked_repository(tmp_path)
    service.configure_repository(planner, "project-1", str(repository))
    service.sync_repository(planner, "project-1")
    [reconciliation] = [
        item
        for item in service.list_repository_reconciliations(planner, "project-1")[
            "reconciliations"
        ]
        if item["status"] == "PENDING"
    ]
    return reconciliation


async def _tool(service, actors):
    tools = await create_mcp_server(service, actors["planner"]).list_tools()
    [tool] = [tool for tool in tools if tool.name == TOOL]
    return tool


def _arguments(pending: dict, decisions: object) -> dict:
    return {
        "project_id": "project-1",
        "reconciliation_id": pending["reconciliation_id"],
        "ticket_decisions": decisions,
        "decision_summary": "The observed commit does not change the plan",
    }


def _keep_all(pending: dict) -> list[dict]:
    return [
        {"ticket_id": ticket_id, "disposition": "keep", "reason": "Docs-only commit"}
        for ticket_id in pending["ticket_versions"]
    ]


def _status(service, reconciliation_id: str) -> str:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT status FROM repository_reconciliations WHERE reconciliation_id = ?",
            (reconciliation_id,),
        ).fetchone()["status"]


@pytest.mark.anyio
async def test_the_schema_names_every_decision_key_and_the_six_dispositions(
    service, actors, seeded
) -> None:
    schema = (await _tool(service, actors)).inputSchema
    decisions = schema["properties"]["ticket_decisions"]
    item = decisions["items"]
    if "$ref" in item:
        item = schema["$defs"][item["$ref"].rsplit("/", 1)[-1]]

    assert set(item["properties"]) == {
        "ticket_id", "disposition", "reason", "replacement_ticket_id"
    }
    assert set(item["required"]) == {"ticket_id", "disposition", "reason"}
    assert item["additionalProperties"] is False
    assert set(item["properties"]["disposition"]["enum"]) == contracts.DISPOSITIONS
    assert len(contracts.DISPOSITIONS) == 6
    assert item["properties"]["reason"]["maxLength"] == contracts.DECISION_REASON_CHARS
    assert decisions["maxItems"] == contracts.MAX_TICKET_DECISIONS


@pytest.mark.anyio
async def test_the_tool_description_carries_a_call_example(service, actors, seeded) -> None:
    description = (await _tool(service, actors)).description
    assert "Example:" in description
    assert '"disposition": "supersede"' in description
    assert '"replacement_ticket_id"' in description


@pytest.mark.anyio
async def test_typed_decisions_resolve_the_barrier(service, actors, pending) -> None:
    _content, result = await create_mcp_server(service, actors["planner"]).call_tool(
        TOOL, _arguments(pending, _keep_all(pending))
    )

    assert result["reconciliation"]["status"] == "RESOLVED"
    manifest = result["reconciliation"]["decision_manifest"]
    # Only the keys the caller set reach the service: no null replacement is stored.
    assert all("replacement_ticket_id" not in item for item in manifest["ticket_decisions"])


@pytest.mark.parametrize(
    "bad_decision, field",
    [
        ({"disposition": "keep", "reason": "r", "evidence": "x"}, "evidence"),
        ({"disposition": "kept", "reason": "r"}, "disposition"),
        ({"disposition": "KEEP", "reason": "r"}, "disposition"),
        ({"disposition": "keep"}, "reason"),
        ({"disposition": "keep", "reason": ""}, "reason"),
        ({"disposition": "keep", "reason": "r" * (contracts.DECISION_REASON_CHARS + 1)}, "reason"),
        ({"disposition": "keep", "reason": "r", "ticket_id": ""}, "ticket_id"),
    ],
    ids=["unknown-key", "unknown-value", "wrong-case", "missing-reason", "blank-reason",
         "long-reason", "blank-ticket"],
)
@pytest.mark.anyio
async def test_a_malformed_decision_is_refused_before_the_service_runs(
    service, actors, pending, bad_decision, field
) -> None:
    decisions = _keep_all(pending)
    decisions[0] = {"ticket_id": decisions[0]["ticket_id"], **bad_decision}

    with pytest.raises(ToolError) as refused:
        await create_mcp_server(service, actors["planner"]).call_tool(
            TOOL, _arguments(pending, decisions)
        )

    assert field in str(refused.value)
    assert _status(service, pending["reconciliation_id"]) == "PENDING"


@pytest.mark.anyio
async def test_a_single_object_instead_of_a_list_is_refused(service, actors, pending) -> None:
    with pytest.raises(ToolError):
        await create_mcp_server(service, actors["planner"]).call_tool(
            TOOL, _arguments(pending, _keep_all(pending)[0])
        )
    assert _status(service, pending["reconciliation_id"]) == "PENDING"


def test_the_service_refuses_a_value_outside_the_shared_set(service, actors, pending) -> None:
    """REST and direct callers meet the same constant the schema enum is built from."""
    decisions = _keep_all(pending)
    decisions[0]["disposition"] = "archive"

    with pytest.raises(ValidationError, match="disposition"):
        service.resolve_repository_reconciliation(
            actors["planner"], "project-1", pending["reconciliation_id"], decisions, "summary"
        )
    assert "archive" not in contracts.DISPOSITIONS
