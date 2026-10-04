"""The packet budget is measured on the text MCP actually delivers."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service._common import delivered_json


@pytest.mark.anyio
async def test_mcp_text_is_compact_json_of_the_structured_result(
    service, actors, seeded
) -> None:
    server = create_mcp_server(service, actors["developer"])
    content, structured = await server.call_tool(
        "ticket.get_context", {"ticket_ref": seeded["ticket"]["ticket_id"]}
    )

    assert len(content) == 1
    assert "\n" not in content[0].text
    assert json.loads(content[0].text) == structured
    assert content[0].text == delivered_json(structured)


@pytest.mark.anyio
@pytest.mark.parametrize("budget", [300, 1000, 4000])
async def test_estimated_tokens_measure_the_delivered_text(
    service, actors, seeded, budget
) -> None:
    for index in range(10):
        service.append_planner_context(
            actors["planner"],
            entry_type="risk",
            content=f"risk {index}: " + "a long explanation " * 30,
            ticket_id=seeded["ticket"]["ticket_id"],
        )
    server = create_mcp_server(service, actors["developer"])
    content, structured = await server.call_tool(
        "ticket.get_context",
        {"ticket_ref": seeded["ticket"]["ticket_id"], "token_budget": budget},
    )

    delivered_tokens = len(content[0].text) // 4
    # Only the placeholder-to-real snapshot id and the estimate itself differ.
    assert abs(delivered_tokens - structured["estimated_tokens"]) <= 8


def test_discovery_does_not_resend_what_bootstrap_returned(service, actors, seeded) -> None:
    for role in ("developer", "planner"):
        discovered = service.discover_work(actors[role], seeded["project"]["project_id"])
        assert discovered["project"] == {
            "project_id": seeded["project"]["project_id"],
            "name": seeded["project"]["name"],
        }
        assert "capabilities" not in discovered["agent"]
