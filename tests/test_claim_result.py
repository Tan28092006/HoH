"""A claim hands the dispatcher {lease, worker_brief} and the ticket's identity only (wave 8D, A5).

The claim used to return the whole tickets row. The dispatcher only passes the
brief on, and the worker reads the body from its own packet, so carrying the
body here put every ticket into the long-lived dispatcher chat. The result is now
identity plus the hand-off, bounded in size, and the brief names the project so
the worker can call work.report_failure without asking.
"""
from __future__ import annotations

import pytest
from aci_helpers import drive_qa_rejection

from hoh_control_plane.domain import Role
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service import leases
from hoh_control_plane.service._common import agent_facing, delivered_json

_RESULT_KEYS = {
    "ticket", "lease", "context_snapshot_id", "current_context_snapshot_id", "worker_brief",
    "family_check",
}
_IDENTITY_KEYS = {
    "project_id", "ticket_id", "feature_id", "title", "status", "version", "required_role",
    "candidate_commit",
}


def _claims(results) -> dict[str, dict]:
    return {
        tool: result for _role, tool, result in results
        if tool in {"work.claim", "qa.claim_candidate"}
    }


@pytest.mark.anyio
async def test_a_claim_carries_identity_lease_and_brief_and_no_ticket_body(
    service, actors, seeded
) -> None:
    claims = _claims(await drive_qa_rejection(service, actors, seeded))

    for tool, claim in claims.items():
        assert set(claim) <= _RESULT_KEYS, tool
        assert set(claim["ticket"]) <= _IDENTITY_KEYS, tool
        assert claim["ticket"]["project_id"] == "project-1"
        assert claim["worker_brief"]["lease_id"] == claim["lease"]["lease_id"]
        assert "acceptance_criteria" not in delivered_json(claim)
        assert "file_scope" not in delivered_json(claim)
    # QA's identity names the frozen artifact it is about to judge.
    assert claims["qa.claim_candidate"]["ticket"]["candidate_commit"]


@pytest.mark.anyio
async def test_the_brief_names_the_project_a_failure_report_needs(
    service, actors, seeded
) -> None:
    server = create_mcp_server(service, actors["developer"])
    ticket = seeded["ticket"]
    _content, claim = await server.call_tool(
        "work.claim", {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]}
    )
    brief = claim["worker_brief"]

    assert brief["project_id"] == "project-1"
    # The brief alone is enough to report a durable failure on this ticket.
    _content, reported = await server.call_tool("work.report_failure", {
        "project_id": brief["project_id"], "ticket_id": brief["ticket_id"],
        "failure_class": "infrastructure", "summary": "The test runner cannot start",
    })
    assert reported


def _long_ticket(service, actors, seeded) -> dict:
    return service.create_ticket(
        actors["planner"], feature_id=seeded["feature"]["feature_id"], ticket_id="D" * 100,
        title="T" * 240, objective="O" * 8000, required_role=Role.DEVELOPER,
        acceptance_criteria=["A" * 2000] * 30, preservation_constraints=["P" * 2000] * 30,
        file_scope=["src/other/**"], ready=True,
    )


def test_a_claim_on_the_largest_ticket_stays_under_the_ceiling(service, actors, seeded) -> None:
    ticket = _long_ticket(service, actors, seeded)

    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    assert len(delivered_json(agent_facing(claim))) <= leases.MAX_CLAIM_RESULT_CHARS
    assert claim["worker_brief"] is not None
    assert len(claim["ticket"]["title"]) <= 120
    assert claim["ticket"]["title"].endswith("...")


def test_past_the_ceiling_the_claim_keeps_lease_and_identity_and_points_at_the_packet(
    service, actors, seeded, monkeypatch
) -> None:
    monkeypatch.setattr(leases, "MAX_CLAIM_RESULT_CHARS", 200)
    ticket = seeded["ticket"]

    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    assert claim["truncated"] is True
    assert claim["worker_brief"] is None
    assert claim["lease"]["ticket_id"] == ticket["ticket_id"]
    assert claim["ticket"]["ticket_id"] == ticket["ticket_id"]
    assert "ticket.get_context" in claim["next_action"]
    # The claim itself committed: the lease is held.
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM leases WHERE lease_id = ?", (claim["lease"]["lease_id"],)
        ).fetchone()
