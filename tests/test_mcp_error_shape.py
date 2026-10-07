"""Every MCP failure has one shape, one audit row and no leaked internals (wave 8D, A4).

Before, work.wait_for_event dropped details and retry_hint, a storage error was
audited as database_error but reported as state_conflict, an argument the schema
refused had no [code] and no audit row, and an unexpected exception reached the
client as raw text. Now mcp.errors maps every failure to an isError result whose
structured content is {code, message, details, retry_hint}.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from hoh_control_plane import event_wait
from hoh_control_plane.domain import ConflictError, NotFoundError, ValidationError
from hoh_control_plane.mcp.errors import HohToolError
from hoh_control_plane.mcp_server import create_mcp_server

_KEYS = {"code", "message", "details", "retry_hint"}


def _audit(service, tool_name: str) -> list[tuple[str, str | None]]:
    with service.store.connection() as connection:
        return [
            (row["outcome"], row["error_code"])
            for row in connection.execute(
                "SELECT outcome, error_code FROM agent_tool_calls WHERE tool_name = ? "
                "ORDER BY occurred_at",
                (tool_name,),
            )
        ]


async def _transport_error(service, actor, tool: str, arguments: dict[str, Any]):
    """Call through a real MCP client session and return the error result."""
    server = create_mcp_server(service, actor)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(tool, arguments)
    assert result.isError, result
    return result


@pytest.mark.anyio
async def test_a_domain_refusal_reaches_a_client_as_structured_is_error(
    service, actors, seeded
) -> None:
    result = await _transport_error(
        service, actors["developer"], "ticket.get_context", {"ticket_ref": "ticket:MISSING-1"}
    )

    payload = result.structuredContent
    assert set(payload) == _KEYS
    assert payload["code"] == NotFoundError.code
    assert result.content[0].text.startswith(f"[{NotFoundError.code}] ")
    assert _audit(service, "ticket.get_context") == [("rejected", NotFoundError.code)]


@pytest.mark.anyio
async def test_a_schema_refusal_has_a_code_names_the_field_and_is_audited(
    service, actors, seeded
) -> None:
    result = await _transport_error(service, actors["developer"], "work.report_failure", {
        "project_id": "project-1", "ticket_id": seeded["ticket"]["ticket_id"],
        "failure_class": "secret-value-xyz", "summary": "It broke",
    })

    payload = result.structuredContent
    assert payload["code"] == ValidationError.code
    assert [issue["field"] for issue in payload["details"]["errors"]] == ["failure_class"]
    assert "input schema" in payload["retry_hint"]
    # The refused value is not echoed back into the transcript.
    assert "secret-value-xyz" not in result.content[0].text
    assert _audit(service, "work.report_failure") == [("rejected", ValidationError.code)]


@pytest.mark.anyio
async def test_a_missing_argument_is_refused_with_the_same_shape(service, actors, seeded) -> None:
    result = await _transport_error(service, actors["developer"], "work.claim", {})

    assert result.structuredContent["code"] == ValidationError.code
    fields = {issue["field"] for issue in result.structuredContent["details"]["errors"]}
    assert "ticket_id" in fields


@pytest.mark.anyio
async def test_an_unexpected_exception_leaks_no_text_and_is_audited_as_internal(
    service, actors, seeded, monkeypatch
) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("secret-token-abc123 from deep inside")

    monkeypatch.setattr(service, "claim_ticket", explode)
    ticket = seeded["ticket"]
    result = await _transport_error(service, actors["developer"], "work.claim", {
        "ticket_id": ticket["ticket_id"], "expected_version": ticket["version"],
    })

    assert result.structuredContent["code"] == "internal_error"
    assert "secret-token-abc123" not in result.content[0].text
    assert "secret-token-abc123" not in str(result.structuredContent)
    assert "RuntimeError" not in result.content[0].text
    assert _audit(service, "work.claim") == [("failed", "internal_error")]


@pytest.mark.anyio
async def test_a_storage_error_is_audited_under_the_code_the_client_reads(
    service, actors, seeded, monkeypatch
) -> None:
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table: leases_v9")

    monkeypatch.setattr(service, "claim_ticket", locked)
    ticket = seeded["ticket"]
    result = await _transport_error(service, actors["developer"], "work.claim", {
        "ticket_id": ticket["ticket_id"], "expected_version": ticket["version"],
    })

    payload = result.structuredContent
    assert payload["code"] == ConflictError.code
    assert payload["details"] == {"reason": "database_conflict"}
    assert payload["retry_hint"]
    assert "leases_v9" not in result.content[0].text
    assert _audit(service, "work.claim") == [("rejected", ConflictError.code)]


@pytest.mark.anyio
async def test_wait_for_event_carries_details_and_a_retry_hint(
    service, actors, seeded, monkeypatch
) -> None:
    async def refuse(*_args, **_kwargs):
        raise ConflictError(
            "The board is frozen", {"reason": "repository_reconciliation_required"}
        )

    monkeypatch.setattr(event_wait, "wait_for_event", refuse)
    result = await _transport_error(
        service, actors["developer"], "work.wait_for_event", {"project_id": "project-1"}
    )

    payload = result.structuredContent
    assert payload["code"] == ConflictError.code
    assert payload["details"] == {"reason": "repository_reconciliation_required"}
    assert "work.wait_for_event" in payload["retry_hint"]
    assert _audit(service, "work.wait_for_event") == [("rejected", ConflictError.code)]


@pytest.mark.anyio
async def test_a_lost_wait_connection_maps_to_connection_lost(
    service, actors, seeded, monkeypatch
) -> None:
    async def drop(*_args, **_kwargs):
        raise ConnectionError("socket reset by peer 10.0.0.7")

    monkeypatch.setattr(event_wait, "wait_for_event", drop)
    result = await _transport_error(
        service, actors["developer"], "work.wait_for_event", {"project_id": "project-1"}
    )

    assert result.structuredContent["code"] == "connection_lost"
    assert "session.bootstrap" in result.structuredContent["retry_hint"]
    assert "10.0.0.7" not in result.content[0].text
    assert _audit(service, "work.wait_for_event") == [("failed", "connection_lost")]


@pytest.mark.anyio
async def test_a_check_made_before_invoke_is_audited_exactly_once(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    server = create_mcp_server(service, actors["developer"])
    with pytest.raises(HohToolError) as raised:
        await server.call_tool("dev.submit_candidate", {
            "ticket_id": ticket["ticket_id"], "lease_id": "lease_1",
            "expected_version": ticket["version"], "commit_sha": "a" * 40,
            "base_commit": "b" * 40, "branch_ref": "agent/x", "developer_claims": ["c"],
            "context_snapshot_id": " ",
        })

    assert raised.value.payload["code"] == ValidationError.code
    assert _audit(service, "dev.submit_candidate") == [("rejected", ValidationError.code)]


@pytest.mark.anyio
async def test_an_in_process_caller_still_catches_a_tool_error(service, actors, seeded) -> None:
    server = create_mcp_server(service, actors["developer"])
    with pytest.raises(ToolError) as raised:
        await server.call_tool("ticket.get_context", {"ticket_ref": "ticket:MISSING-2"})

    assert str(raised.value).startswith(f"[{NotFoundError.code}] ")
    # No FastMCP framing in front of the code.
    assert "Error executing tool" not in str(raised.value)


@pytest.mark.anyio
async def test_an_unknown_tool_is_a_validation_error_not_an_internal_one(
    service, actors, seeded
) -> None:
    result = await _transport_error(service, actors["developer"], "planner.create_ticket", {})

    assert result.structuredContent["code"] == ValidationError.code
    assert "planner.create_ticket" in result.structuredContent["message"]
