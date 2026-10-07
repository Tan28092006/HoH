"""P2: the transport activity log never changes a tool call's outcome.

``record_tool_call`` runs after the call's own transaction. Losing the log row
to contention or a full disk must not turn a committed success into an error the
agent would retry, nor replace a failed call's real error with a logging one.
"""
from __future__ import annotations

import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hoh_control_plane.domain import ConflictError
from hoh_control_plane.mcp.invoke import make_invoke
from hoh_control_plane.store import ContentionError


@pytest.fixture
def failing_log(service, monkeypatch):
    attempts = []

    def record_tool_call(_actor, **fields):
        attempts.append(fields)
        raise ContentionError("database is locked")

    monkeypatch.setattr(service, "record_tool_call", record_tool_call)
    return attempts


def test_a_committed_success_stays_a_success_when_its_log_row_fails(
    service, actors, failing_log, caplog
):
    runs = []
    invoke = make_invoke(service, actors["planner"])

    with caplog.at_level(logging.WARNING, logger="hoh_control_plane.mcp.invoke"):
        result = invoke("work.discover", {"project_id": "project-1"},
                        lambda: runs.append(1) or {"ok": True})

    assert result == {"ok": True}
    # Ran once: the agent is not told to retry work that already committed.
    assert runs == [1]
    assert [entry["outcome"] for entry in failing_log] == ["succeeded"]
    assert "was not recorded: ContentionError" in caplog.text


def test_a_rejected_call_keeps_its_own_error_when_its_log_row_fails(
    service, actors, failing_log
):
    invoke = make_invoke(service, actors["planner"])

    def refused():
        raise ConflictError("Ticket version changed")

    with pytest.raises(ToolError) as raised:
        invoke("ticket.update", {"project_id": "project-1"}, refused)

    assert "Ticket version changed" in str(raised.value)
    assert "locked" not in str(raised.value)
    assert [entry["outcome"] for entry in failing_log] == ["rejected"]


def test_an_internal_failure_is_reraised_unchanged_when_its_log_row_fails(
    service, actors, failing_log
):
    invoke = make_invoke(service, actors["planner"])

    def broken():
        raise RuntimeError("the real fault")

    with pytest.raises(RuntimeError, match="the real fault"):
        invoke("work.discover", {"project_id": "project-1"}, broken)
    assert [entry["outcome"] for entry in failing_log] == ["failed"]
