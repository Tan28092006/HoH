"""Drive one QA rejection through the role MCP servers and keep every tool result."""
from __future__ import annotations

from typing import Any

from hoh_control_plane.mcp_server import create_mcp_server


class Recorder:
    """Call MCP tools as one role and remember each (tool, result) pair."""

    def __init__(self, service, actor, results: list[tuple[str, str, Any]]) -> None:
        self.server = create_mcp_server(service, actor)
        self.role = actor.role.value
        self.results = results

    async def __call__(self, tool: str, **arguments: Any) -> Any:
        _content, result = await self.server.call_tool(tool, arguments)
        self.results.append((self.role, tool, result))
        return result


async def drive_qa_rejection(service, actors, seeded) -> list[tuple[str, str, Any]]:
    """One ticket from claim to a failed verdict and Planner's review of it."""
    results: list[tuple[str, str, Any]] = []
    planner = Recorder(service, actors["planner"], results)
    developer = Recorder(service, actors["developer"], results)
    qa = Recorder(service, actors["qa"], results)
    ticket = seeded["ticket"]
    ticket_id = ticket["ticket_id"]
    project_id = seeded["project"]["project_id"]

    await developer("session.bootstrap", project_id=project_id)
    await developer("agent.register", skills=["hoh-control-plane", "hoh-developer"],
                    model_family="anthropic")
    await developer("work.inbox", project_id=project_id)
    await developer("work.discover", project_id=project_id)
    packet = await developer("ticket.get_context", ticket_ref=f"ticket:{ticket_id}")
    claim = await developer("work.claim", ticket_id=ticket_id,
                            expected_version=packet["ticket"]["version"])
    await developer("experience.record", ticket_id=ticket_id,
                    lesson="Retry tests need the payment stub reset between runs.",
                    file_scope=["src/checkout/**"])
    fresh = await developer("ticket.get_context", ticket_ref=f"ticket:{ticket_id}")
    submitted = await developer(
        "dev.submit_candidate", ticket_id=ticket_id, lease_id=claim["lease"]["lease_id"],
        expected_version=fresh["ticket"]["version"], commit_sha="a" * 40,
        base_commit="b" * 40, branch_ref=f"agent/dev-01/{ticket_id}",
        developer_claims=["Retry creates one order."],
        context_snapshot_id=fresh["context_snapshot_id"],
    )

    await qa("agent.register", skills=["hoh-control-plane", "hoh-qa"], model_family="openai")
    await qa("work.inbox", project_id=project_id)
    qa_claim = await qa("qa.claim_candidate", ticket_id=ticket_id,
                        expected_version=submitted["ticket"]["version"])
    qa_packet = await qa("ticket.get_context", ticket_ref=f"ticket:{ticket_id}")
    await qa(
        "qa.submit_verdict", ticket_id=ticket_id, lease_id=qa_claim["lease"]["lease_id"],
        candidate_id=submitted["candidate"]["candidate_id"],
        expected_version=qa_packet["ticket"]["version"], verdict="failed",
        findings=[
            {
                "criterion": criterion,
                "status": "failed",
                "severity": "high",
                "observation": "A duplicate order is still created.",
                "evidence": [{"type": "test_result", "observation": "duplicate order",
                              "storage_ref": "evidence://qa/duplicate.json"}],
            }
            for criterion in ticket["acceptance_criteria"]
        ],
        context_snapshot_id=qa_packet["context_snapshot_id"],
    )

    await planner("session.bootstrap", project_id=project_id)
    await planner("agent.register", skills=["hoh-control-plane", "hoh-planner"])
    await planner("work.discover", project_id=project_id, detail=True)
    await planner("events.list", project_id=project_id)
    await planner("planner.list_interventions", project_id=project_id)
    await planner("planner.list_repository_reconciliations", project_id=project_id,
                  status="RESOLVED")
    await planner("work.wait_for_event", project_id=project_id)
    await planner("ticket.get_context", ticket_ref=f"ticket:{ticket_id}")
    await planner("feature.get_context", feature_ref=f"feature:{seeded['feature']['feature_id']}")
    [job_id] = {
        value
        for _role, _tool, result in results
        for value in _values(result, "planner_job_id")
    }
    await planner("planner.claim_job", planner_job_id=job_id)
    await developer("work.inbox", project_id=project_id)
    await developer("ticket.get_context", ticket_ref=f"ticket:{ticket_id}")
    return results


def _values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key and isinstance(item, str):
                found.append(item)
            found.extend(_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_values(item, key))
    return found
