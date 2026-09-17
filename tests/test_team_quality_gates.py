"""Four guarantees that make each iteration land right the first time.

Each one closes a gap where the control plane stated a rule the runtime did not
enforce, so an agent that skipped it got no signal and the whole iteration was
paid for twice: a write decided from stale context, a barrier nobody was told
was ageing, a contract that never fit a packet, and two installations that
would name the same branch.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh_control_plane.domain import Role, ValidationError
from hoh_control_plane.mcp_server import ToolError, create_mcp_server


def _snapshot(service, actor, ticket_id: str) -> str:
    return service.get_ticket_context(actor, ticket_id)["context_snapshot_id"]


# ---------------------------------------------------------------------------
# 1. A consequential write must name the packet it was decided from.


@pytest.mark.anyio
async def test_submission_without_a_context_snapshot_is_refused(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    arguments = {
        "ticket_id": ticket["ticket_id"],
        "lease_id": claim["lease"]["lease_id"],
        "expected_version": claim["ticket"]["version"],
        "commit_sha": "a" * 40,
        "base_commit": "b" * 40,
        "branch_ref": "agent/dev-01/DEV-142",
        "developer_claims": ["Ready."],
    }
    server = create_mcp_server(service, actors["developer"])

    with pytest.raises(ToolError, match="context_snapshot_id"):
        await server.call_tool("dev.submit_candidate", arguments)
    with pytest.raises(ToolError, match="context_snapshot_id"):
        await server.call_tool(
            "dev.submit_candidate", {**arguments, "context_snapshot_id": "   "}
        )

    _content, result = await server.call_tool(
        "dev.submit_candidate",
        {
            **arguments,
            "context_snapshot_id": _snapshot(
                service, actors["developer"], ticket["ticket_id"]
            ),
        },
    )
    assert result["ticket"]["ticket_id"] == ticket["ticket_id"]


@pytest.mark.anyio
async def test_a_snapshot_taken_before_a_planner_decision_no_longer_authorizes_a_write(
    service, actors, seeded
) -> None:
    """The point of requiring one: the check now actually runs on every submit."""
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    stale = _snapshot(service, actors["developer"], ticket["ticket_id"])
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content="Scope narrowed: only the retry path.",
        ticket_id=ticket["ticket_id"],
    )

    with pytest.raises(ToolError, match="[Rr]efresh"):
        await create_mcp_server(service, actors["developer"]).call_tool(
            "dev.submit_candidate",
            {
                "ticket_id": ticket["ticket_id"],
                "lease_id": claim["lease"]["lease_id"],
                "expected_version": claim["ticket"]["version"],
                "commit_sha": "a" * 40,
                "base_commit": "b" * 40,
                "branch_ref": "agent/dev-01/DEV-142",
                "developer_claims": ["Ready."],
                "context_snapshot_id": stale,
            },
        )


@pytest.mark.anyio
async def test_verdict_and_merge_tools_declare_the_snapshot_as_required(
    service, actors
) -> None:
    """Required in the schema, not merely validated, so a client can see it."""
    qa_tools = {tool.name: tool for tool in await create_mcp_server(service, actors["qa"]).list_tools()}
    integrator_tools = {
        tool.name: tool
        for tool in await create_mcp_server(service, actors["integrator"]).list_tools()
    }
    developer_tools = {
        tool.name: tool
        for tool in await create_mcp_server(service, actors["developer"]).list_tools()
    }

    for tools, name in (
        (qa_tools, "qa.submit_verdict"),
        (integrator_tools, "integrator.merge_candidate"),
        (developer_tools, "dev.submit_candidate"),
    ):
        required = tools[name].inputSchema.get("required") or []
        assert "context_snapshot_id" in required, name


# ---------------------------------------------------------------------------
# 2. A barrier that nobody resolves must get louder, not quieter.


def _open_barrier(service, project_id: str, *, age_seconds: int = 0) -> str:
    opened = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat().replace(
        "+00:00", "Z"
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO repository_reconciliations
                (reconciliation_id, project_id, observation_ref, observed_commits,
                 ticket_versions, status, created_at)
            VALUES ('recon_9001', ?, 'git:observed:abc', ?, '{}', 'PENDING', ?)
            """,
            (project_id, json.dumps([{"commit": "c" * 40}]), opened),
        )
    return "recon_9001"


def test_a_fresh_barrier_tells_an_execution_role_to_hold(service, actors, seeded) -> None:
    from hoh_control_plane.event_wait import _pending

    _open_barrier(service, "project-1")

    events = _pending(service, actors["developer"], "project-1")

    assert len(events) == 1
    _key, payload = events[0]
    assert payload["kind"] == "repository_reconciliation_required"
    assert payload["escalation_level"] == 0
    assert payload["age_seconds"] < 60
    assert payload["opened_at"]


def test_an_ageing_barrier_escalates_and_says_how_long_work_has_been_blocked(
    service, actors, seeded
) -> None:
    from hoh_control_plane.event_wait import _pending

    _open_barrier(service, "project-1", age_seconds=9000)

    developer_events = _pending(service, actors["developer"], "project-1")
    planner_events = _pending(service, actors["planner"], "project-1")

    key, payload = developer_events[0]
    assert payload["kind"] == "planning_barrier_pending"
    assert payload["escalation_level"] == 2, "30 minutes and 2 hours both crossed"
    assert payload["age_seconds"] >= 9000
    assert "150 minutes" in payload["next_action"]
    assert "operator" in payload["next_action"]
    # A new key per level, so an escalation is delivered even though the earlier
    # notification for the same barrier was already sent to this agent.
    assert key.endswith(":stale2")

    # Planner keeps its own event kind: it is the role that resolves the barrier,
    # not a role waiting on somebody else to.
    _planner_key, planner_payload = planner_events[0]
    assert planner_payload["kind"] == "repository_changed"
    assert planner_payload["escalation_level"] == 2
    assert "operator" not in planner_payload["next_action"]


def test_the_inbox_reports_barrier_age_and_the_packet_stays_snapshot_stable(
    service, actors, seeded
) -> None:
    _open_barrier(service, "project-1", age_seconds=4000)
    service.register_agent(
        actors["developer"], skills=["hoh-control-plane"], capacity=1
    )

    inbox = service.get_agent_inbox(actors["developer"], "project-1")
    first = service.get_ticket_context(actors["planner"], seeded["ticket"]["ticket_id"])
    second = service.get_ticket_context(actors["planner"], seeded["ticket"]["ticket_id"])

    assert inbox["planning_barrier"]["oldest_age_seconds"] >= 4000
    assert inbox["planning_barrier"]["pending"][0]["age_seconds"] >= 4000
    # The packet carries opened_at, never a clock reading: an age inside a
    # snapshotted packet would make every read a different snapshot.
    pending = first["planning_barrier"]["pending"][0]
    assert pending["opened_at"]
    assert "age_seconds" not in pending
    assert first["context_snapshot_id"] == second["context_snapshot_id"]


# ---------------------------------------------------------------------------
# 3. A contract must fit the packet it will live in for the ticket's whole life.


def _contract(service, actors, criteria, expected="Observed on the candidate."):
    return [
        {
            "criterion": criterion,
            "scenario_ref": f"s{index}",
            "method": "black_box",
            "expected_observation": expected,
            "required_evidence_types": ["test_result"],
            "required_capabilities": ["build_execution"],
        }
        for index, criterion in enumerate(criteria)
    ]


def test_an_oversized_contract_entry_is_refused_at_write_time(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    criteria = ticket["acceptance_criteria"]
    contract = _contract(service, actors, criteria, expected="x" * 601)

    with pytest.raises(ValidationError, match="expected_observation"):
        service.configure_validation_contract(
            actors["planner"], ticket["ticket_id"], ticket["version"], contract,
            "Bind each criterion.",
        )


def test_a_contract_that_would_not_fit_a_packet_is_refused_with_its_size(
    service, actors, seeded
) -> None:
    """The ticket section is protected, so an unbounded contract is permanent cost."""
    planner = actors["planner"]
    criteria = [f"Criterion {index}: the behaviour holds." for index in range(20)]
    feature = seeded["feature"]
    ticket = service.create_ticket(
        planner,
        feature_id=feature["feature_id"],
        ticket_id="DEV-777",
        title="Wide ticket",
        objective="Too many scenarios for one packet",
        required_role=Role.DEVELOPER,
        acceptance_criteria=criteria,
        preservation_constraints=[],
        file_scope=["src/**"],
    )
    contract = _contract(service, actors, criteria, expected="y" * 590)

    with pytest.raises(ValidationError) as error:
        service.configure_validation_contract(
            planner, ticket["ticket_id"], ticket["version"], contract, "Bind them.",
        )

    assert error.value.details["contract_estimated_tokens"] > error.value.details["budget"]
    assert "split" in str(error.value)


def test_a_contract_that_fits_reports_what_it_costs_the_planner(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    contract = _contract(service, actors, ticket["acceptance_criteria"])

    result = service.configure_validation_contract(
        actors["planner"], ticket["ticket_id"], ticket["version"], contract,
        "Bind each criterion to an observation.",
    )

    assert 0 < result["contract_estimated_tokens"] <= 1_200
    packet = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    assert len(packet["ticket"]["validation_contract"]) == len(contract)


# ---------------------------------------------------------------------------
# 4. Two installations must never name the same branch.


def test_profile_identity_is_unique_per_installation(tmp_path: Path) -> None:
    from hoh_control_plane.local_agent import profile_actor

    unsalted = profile_actor("shop", "developer", "primary")
    first = profile_actor("shop", "developer", "primary", salt="installation-a")
    second = profile_actor("shop", "developer", "primary", salt="installation-b")

    assert first.agent_id != second.agent_id
    assert first.agent_id != unsalted.agent_id
    assert first.agent_id == profile_actor(
        "shop", "developer", "primary", salt="installation-a"
    ).agent_id, "stable for one installation"
    assert first.role is Role.DEVELOPER and first.project_ids == frozenset({"shop"})


def test_an_existing_profile_keeps_its_identity_across_the_salt_change(
    service, tmp_path: Path
) -> None:
    """Re-deriving an installed agent's id would orphan its leases and worktrees."""
    from hoh_control_plane.local_agent import installed_profile_actor, profile_actor

    root = tmp_path / "install"
    (root / "config").mkdir(parents=True)
    config = {
        "installation_id": "installation-a",
        "database_path": str(service.store.path),
    }
    legacy = profile_actor("project-1", "developer", "primary")
    (root / "config/agent-credentials.json").write_text(
        json.dumps({"t" * 48: {
            "agent_id": legacy.agent_id, "role": "developer", "project_ids": ["project-1"],
        }}),
        encoding="utf-8",
    )

    resolved = installed_profile_actor(root, config, "project-1", "developer", "primary")
    fresh = installed_profile_actor(root, config, "project-1", "qa", "primary")

    assert resolved.agent_id == legacy.agent_id, "a credentialed profile is left alone"
    assert fresh.agent_id != profile_actor("project-1", "qa", "primary").agent_id
    assert fresh.agent_id == profile_actor(
        "project-1", "qa", "primary", salt="installation-a"
    ).agent_id


def test_a_profile_that_only_ever_bootstrapped_also_keeps_its_identity(
    service, actors, tmp_path: Path
) -> None:
    """A stdio profile has no credential row; presence is the evidence it exists."""
    from hoh_control_plane.local_agent import installed_profile_actor, profile_actor

    root = tmp_path / "install"
    (root / "config").mkdir(parents=True)
    legacy = profile_actor("project-1", "integrator", "primary")
    service.register_agent(
        type(actors["integrator"])(legacy.agent_id, Role.INTEGRATOR, frozenset({"project-1"})),
        skills=["hoh-control-plane"],
        capacity=1,
    )

    resolved = installed_profile_actor(
        root,
        {"installation_id": "installation-a", "database_path": str(service.store.path)},
        "project-1", "integrator", "primary",
    )

    assert resolved.agent_id == legacy.agent_id
