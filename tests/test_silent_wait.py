"""A waiting role holds one long wait and says nothing until there is news.

Observed on the live marketing project: role chats passed timeout_seconds of
about a minute, so every wait came back empty, the chat narrated it ("still
waiting"), and called again. The narration grew the context until the chat
ended its turn, and a chat that has ended its turn is not waiting at all: the
role went offline. The server now holds an MCP wait regardless of the value a
model picks, and an empty return tells the model, in the result itself, to go
straight back to waiting without a message.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hoh_control_plane import event_wait


def test_an_mcp_wait_is_held_for_the_server_maximum_whatever_the_model_passes(
    service, actors, monkeypatch
) -> None:
    from hoh_control_plane.mcp_server import create_mcp_server

    captured: dict = {}

    async def fake_wait(service_, actor, project_id, **arguments):
        captured.update(arguments)
        return {"status": "timeout", "event_id": None, "event": None}

    monkeypatch.setattr(event_wait, "wait_for_event", fake_wait)
    server = create_mcp_server(service, actors["developer"])

    for requested in (1, 60, 180, 3599):
        captured.clear()
        asyncio.run(server.call_tool(
            "work.wait_for_event", {"project_id": "project-1", "timeout_seconds": requested}
        ))
        assert captured["timeout_seconds"] == event_wait.MAX_WAIT_SECONDS, requested


def test_the_hold_never_lowers_a_request_or_masks_an_invalid_one(
    service, actors, monkeypatch
) -> None:
    from hoh_control_plane.mcp_server import ToolError, create_mcp_server

    captured: dict = {}

    async def fake_wait(service_, actor, project_id, **arguments):
        captured.update(arguments)
        return {"status": "timeout", "event_id": None, "event": None}

    monkeypatch.setattr(event_wait, "wait_for_event", fake_wait)
    server = create_mcp_server(service, actors["developer"])

    asyncio.run(server.call_tool("work.wait_for_event", {"project_id": "project-1"}))
    assert captured["timeout_seconds"] == event_wait.MAX_WAIT_SECONDS

    # A value the server would reject is passed through to be rejected, not
    # quietly turned into a valid one.
    monkeypatch.undo()
    with pytest.raises(ToolError, match="timeout_seconds"):
        asyncio.run(server.call_tool(
            "work.wait_for_event", {"project_id": "project-1", "timeout_seconds": -5}
        ))


def test_an_empty_return_tells_the_model_to_resume_silently(service, actors, seeded) -> None:
    # The seeded ticket is READY, so a Developer would be handed it; the
    # Integrator has nothing to do, which is the case this rule is about.
    service.register_agent(actors["integrator"], skills=["hoh-integrator"], capacity=1)

    result = asyncio.run(event_wait.wait_for_event(
        service, actors["integrator"], "project-1", timeout_seconds=0.05
    ))

    assert result["status"] == "timeout"
    assert result["next_action"] == event_wait.SILENT_RESUME
    assert "write nothing" in result["next_action"]
    assert "again now" in result["next_action"]
