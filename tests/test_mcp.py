from __future__ import annotations

import sqlite3

import pytest

from mcp.server.fastmcp.exceptions import ToolError

import hoh_control_plane.mcp_server as mcp_server
from hoh_control_plane.domain import Actor, ConflictError, Role, ValidationError
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import ContentionError, SQLiteStore


@pytest.mark.anyio
async def test_mcp_exposes_only_role_appropriate_tools(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    service.create_ticket(
        planner,
        "feature-1",
        "DEV-1",
        "Build feature",
        "Observable result",
        Role.DEVELOPER,
        ["Behavior is visible"],
        [],
        ["src/**"],
        True,
    )
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    qa = Actor("qa-01", Role.QA, frozenset({"project-1"}))
    integrator = Actor("integrator-01", Role.INTEGRATOR, frozenset({"project-1"}))
    planner_server = create_mcp_server(service, planner)
    developer_server = create_mcp_server(service, developer)
    qa_server = create_mcp_server(service, qa)
    integrator_server = create_mcp_server(service, integrator)

    tools = await developer_server.list_tools()
    names = {tool.name for tool in tools}
    qa_names = {tool.name for tool in await qa_server.list_tools()}
    integrator_names = {tool.name for tool in await integrator_server.list_tools()}
    planner_names = {tool.name for tool in await planner_server.list_tools()}
    identity_content, identity = await developer_server.call_tool(
        "identity.get_capabilities", {}
    )
    content, structured = await developer_server.call_tool(
        "work.discover", {"project_id": "project-1", "dependency_ready": True}
    )
    feature_content, feature_context = await developer_server.call_tool(
        "feature.get_context", {"feature_ref": "feature:feature-1"}
    )
    ticket_content, ticket_context = await developer_server.call_tool(
        "ticket.get_context", {"ticket_ref": "ticket:DEV-1"}
    )

    assert {
        "identity.get_capabilities",
        "work.discover",
        "feature.get_context",
        "ticket.get_context",
        "work.claim",
        "work.heartbeat",
        "dev.prepare_worktree",
        "dev.get_worktree_provision",
        "dev.append_progress",
        "dev.submit_candidate",
    }.issubset(names)
    assert "planner.create_ticket" not in names
    assert "qa.submit_verdict" not in names
    assert {"qa.claim_candidate", "qa.record_test_run", "qa.submit_verdict"}.issubset(
        qa_names
    )
    assert {"qa.run_project_tests", "qa.get_project_test_run"}.issubset(qa_names)
    assert "dev.submit_candidate" not in qa_names
    assert {
        "work.inbox",
        "work.heartbeat",
        "integrator.claim_candidate",
        "integrator.merge_candidate",
        "integrator.get_merge_candidate",
    }.issubset(integrator_names)
    assert "dev.submit_candidate" not in integrator_names
    assert "qa.submit_verdict" not in integrator_names
    assert "planner.configure_repository" in planner_names
    assert "planner.configure_test_command" in planner_names
    assert content[0].text
    assert structured["project"]["project_id"] == "project-1"
    assert identity_content[0].text
    assert identity["role"] == "developer"
    assert feature_content[0].text and feature_context["open_ticket_count"] == 1
    assert ticket_content[0].text and ticket_context["ticket"]["ticket_id"] == "DEV-1"


@pytest.mark.anyio
async def test_planner_mcp_creates_ready_ticket_and_records_safe_tool_activity(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    server = create_mcp_server(service, planner)

    names = {tool.name for tool in await server.list_tools()}
    assert {
        "planner.create_project",
        "planner.create_feature",
        "planner.create_ticket",
        "planner.set_ticket_ready",
        "planner.append_context",
    }.issubset(names)
    assert "dev.append_progress" not in names

    _, project = await server.call_tool(
        "planner.create_project",
        {
            "project_id": "project-1",
            "name": "Project",
            "description": "A project planned through MCP",
            "default_branch": "main",
        },
    )
    _, feature = await server.call_tool(
        "planner.create_feature",
        {
            "project_id": "project-1",
            "feature_id": "feature-1",
            "title": "Feature",
            "objective": "Deliver an observable feature",
        },
    )
    _, ticket = await server.call_tool(
        "planner.create_ticket",
        {
            "feature_id": "feature-1",
            "ticket_id": "DEV-100",
            "title": "Implement the feature",
            "objective": "Users can observe the feature",
            "required_role": "developer",
            "acceptance_criteria": ["The behavior is demonstrable"],
            "preservation_constraints": ["Existing flows continue to work"],
            "file_scope": ["src/**"],
        },
    )
    _, ready = await server.call_tool(
        "planner.set_ticket_ready",
        {"ticket_id": "DEV-100", "expected_version": ticket["version"]},
    )
    await server.call_tool(
        "planner.append_context",
        {
            "ticket_id": "DEV-100",
            "entry_type": "planning_decision",
            "content": "Split code and QA work by observable behavior.",
        },
    )

    activity = service.list_agent_activity(planner, "project-1")
    assert project["project_id"] == "project-1"
    assert feature["feature_id"] == "feature-1"
    assert ready["ticket"]["status"] == "READY"
    assert [item["tool_name"] for item in activity["calls"]] == [
        "planner.create_project",
        "planner.create_feature",
        "planner.create_ticket",
        "planner.set_ticket_ready",
        "planner.append_context",
    ]
    assert activity["calls"][-1]["input_summary"] == {
        "argument_keys": ["content", "entry_type", "ticket_id"],
        "target": {"ticket_id": "DEV-100"},
    }


@pytest.mark.anyio
async def test_planner_updates_blocked_ticket_write_scope_with_version_and_audit(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    ticket = service.create_ticket(
        planner,
        "feature-1",
        "DEV-SCOPE",
        "Repair scope metadata",
        "Correct write scope after a validation failure",
        Role.DEVELOPER,
        ["The candidate validates only declared repository paths"],
        [],
        ["service.py"],
        ready=True,
    )
    claim = service.claim_ticket(
        developer, ticket["ticket_id"], ticket["version"], lease_seconds=900
    )

    with pytest.raises(ConflictError, match="active development"):
        service.update_ticket_write_scope(
            planner,
            ticket["ticket_id"],
            claim["ticket"]["version"],
            ["src/hoh_control_plane/service.py"],
            "Correct the repository-relative scope.",
        )

    blocked = service.report_blocker(
        developer,
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "Candidate validation found an incorrectly declared write scope.",
    )
    planner_server = create_mcp_server(service, planner)
    developer_server = create_mcp_server(service, developer)

    planner_names = {tool.name for tool in await planner_server.list_tools()}
    developer_names = {tool.name for tool in await developer_server.list_tools()}
    assert "planner.update_write_scope" in planner_names
    assert "planner.update_write_scope" not in developer_names

    _, updated = await planner_server.call_tool(
        "planner.update_write_scope",
        {
            "ticket_id": ticket["ticket_id"],
            "expected_version": blocked["ticket"]["version"],
            "file_scope": ["src/hoh_control_plane/service.py"],
            "reason": "Correct the repository-relative scope after Git validation evidence.",
        },
    )

    assert updated["ticket"]["status"] == "BLOCKED"
    assert updated["ticket"]["version"] == blocked["ticket"]["version"] + 1
    assert updated["ticket"]["file_scope"] == ["src/hoh_control_plane/service.py"]
    assert updated["previous_file_scope"] == ["service.py"]
    assert updated["context_entry"]["entry_type"] == "write_scope_updated"
    assert service.list_agent_activity(planner, "project-1")["calls"][-1]["tool_name"] == (
        "planner.update_write_scope"
    )
    audit_event = next(
        event
        for event in service.list_events(planner, "project-1")["events"]
        if event["event_type"] == "ticket.write_scope_updated"
    )
    assert audit_event["aggregate_version"] == updated["ticket"]["version"]
    assert audit_event["payload"] == {
        "previous_file_scope": ["service.py"],
        "file_scope": ["src/hoh_control_plane/service.py"],
        "reason": "Correct the repository-relative scope after Git validation evidence.",
    }

    with pytest.raises(ConflictError, match="version"):
        service.update_ticket_write_scope(
            planner,
            ticket["ticket_id"],
            blocked["ticket"]["version"],
            ["src/hoh_control_plane/service.py"],
            "Stale write-scope repair.",
        )
    with pytest.raises(ValidationError, match="repository-relative"):
        service.update_ticket_write_scope(
            planner,
            ticket["ticket_id"],
            updated["ticket"]["version"],
            ["../secrets/**"],
            "Reject path traversal in write scope.",
        )
    with pytest.raises(ValidationError, match="repository-relative"):
        service.update_ticket_write_scope(
            planner,
            ticket["ticket_id"],
            updated["ticket"]["version"],
            ["/outside-the-repository/**"],
            "Reject an absolute write scope.",
        )


@pytest.mark.anyio
async def test_execution_mcp_registers_and_polls_its_durable_inbox(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp-dispatch.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    service.create_ticket(
        planner, "feature-1", "DEV-QUEUE", "Queued work", "Do one bounded thing",
        Role.DEVELOPER, ["The bounded thing is observable"], [], ["src/**"], ready=True,
    )
    server = create_mcp_server(service, developer)

    names = {tool.name for tool in await server.list_tools()}
    _, presence = await server.call_tool(
        "agent.register", {"skills": ["hoh-developer"], "machine_id": "dev-machine"}
    )
    _, inbox = await server.call_tool("work.inbox", {"project_id": "project-1"})

    assert {"agent.register", "agent.heartbeat", "work.inbox"}.issubset(names)
    assert presence["agent_id"] == "dev-01"
    assert inbox["offers"][0]["ticket"]["ticket_id"] == "DEV-QUEUE"
    activity = service.list_agent_activity(developer, "project-1")["calls"]
    assert activity[-1]["tool_name"] == "work.inbox"
    assert "skills" not in activity[-2]["input_summary"].get("target", {})


@pytest.mark.anyio
async def test_developer_can_report_a_blocker_and_planner_can_requeue_it(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    ticket = service.create_ticket(
        planner, "feature-1", "DEV-101", "Blocked work", "Need a contract",
        Role.DEVELOPER, ["Contract is known"], [], ["src/**"], ready=True,
    )
    developer_server = create_mcp_server(service, developer)
    planner_server = create_mcp_server(service, planner)

    _, claim = await developer_server.call_tool(
        "work.claim", {"ticket_id": "DEV-101", "expected_version": ticket["version"]}
    )
    _, blocked = await developer_server.call_tool(
        "dev.report_blocker",
        {
            "ticket_id": "DEV-101",
            "lease_id": claim["lease"]["lease_id"],
            "expected_version": claim["ticket"]["version"],
            "reason": "The upstream API contract is undecided.",
        },
    )
    _, requeued = await planner_server.call_tool(
        "planner.set_ticket_ready",
        {"ticket_id": "DEV-101", "expected_version": blocked["ticket"]["version"]},
    )

    assert blocked["ticket"]["status"] == "BLOCKED"
    assert requeued["ticket"]["status"] == "READY"
    calls = service.list_agent_activity(planner, "project-1")["calls"]
    assert [call["tool_name"] for call in calls] == [
        "work.claim", "dev.report_blocker", "planner.set_ticket_ready"
    ]


@pytest.mark.anyio
async def test_qa_mcp_queues_a_detached_project_test_worker(
    service, actors, seeded, monkeypatch
) -> None:
    queued = {
        "ticket": seeded["ticket"],
        "test_run": {
            "test_run_id": "testrun_async",
            "ticket_id": seeded["ticket"]["ticket_id"],
            "status": "QUEUED",
        },
    }
    launched: list[tuple[object, str]] = []
    monkeypatch.setattr(service, "queue_project_tests", lambda *_args, **_kwargs: queued)
    monkeypatch.setattr(
        mcp_server,
        "_start_qa_test_worker",
        lambda database_path, test_run_id: launched.append((database_path, test_run_id)),
    )

    _, result = await create_mcp_server(service, actors["qa"]).call_tool(
        "qa.run_project_tests",
        {"ticket_id": seeded["ticket"]["ticket_id"], "lease_id": "lease_qa"},
    )

    assert result["test_run"]["status"] == "QUEUED"
    assert launched == [(service.store.path, "testrun_async")]


@pytest.mark.anyio
async def test_developer_mcp_queues_a_detached_worktree_provision(
    service, actors, seeded, monkeypatch
) -> None:
    queued = {
        "ticket": seeded["ticket"],
        "provision": {
            "provision_id": "worktree_job_async",
            "ticket_id": seeded["ticket"]["ticket_id"],
            "status": "QUEUED",
        },
    }
    launched: list[tuple[object, str]] = []
    monkeypatch.setattr(service, "queue_worktree_provision", lambda *_args, **_kwargs: queued)
    monkeypatch.setattr(
        mcp_server,
        "_start_worktree_provision_worker",
        lambda database_path, provision_id: launched.append((database_path, provision_id)),
    )

    _, result = await create_mcp_server(service, actors["developer"]).call_tool(
        "dev.prepare_worktree",
        {
            "ticket_id": seeded["ticket"]["ticket_id"],
            "lease_id": "lease_developer",
            "expected_version": seeded["ticket"]["version"],
        },
    )

    assert result["provision"]["status"] == "QUEUED"
    assert launched == [(service.store.path, "worktree_job_async")]


@pytest.mark.anyio
async def test_developer_and_qa_mcp_flow_records_test_run_and_verdict(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    store.initialize()
    service = ControlPlaneService(store)
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1"}))
    qa = Actor("qa-01", Role.QA, frozenset({"project-1"}))
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    ticket = service.create_ticket(
        planner, "feature-1", "DEV-102", "Implement behavior", "Behavior works",
        Role.DEVELOPER, ["The behavior is demonstrable"], [], ["src/**"], ready=True,
    )
    developer_server = create_mcp_server(service, developer)
    qa_server = create_mcp_server(service, qa)

    _, claim = await developer_server.call_tool(
        "work.claim", {"ticket_id": "DEV-102", "expected_version": ticket["version"]}
    )
    await developer_server.call_tool(
        "dev.append_progress",
        {"ticket_id": "DEV-102", "lease_id": claim["lease"]["lease_id"], "message": "Implementation complete."},
    )
    _, candidate = await developer_server.call_tool(
        "dev.submit_candidate",
        {
            "ticket_id": "DEV-102",
            "lease_id": claim["lease"]["lease_id"],
            "expected_version": claim["ticket"]["version"],
            "commit_sha": "a" * 40,
            "base_commit": "b" * 40,
            "branch_ref": "agent/dev-01/DEV-102",
            "developer_claims": ["Behavior is ready for independent QA."],
        },
    )
    _, qa_claim = await qa_server.call_tool(
        "qa.claim_candidate",
        {"ticket_id": "DEV-102", "expected_version": candidate["ticket"]["version"]},
    )
    await qa_server.call_tool(
        "qa.record_test_run",
        {
            "ticket_id": "DEV-102",
            "lease_id": qa_claim["lease"]["lease_id"],
            "summary": "The isolated regression suite passed.",
            "evidence_refs": ["evidence://qa/DEV-102/run-1.json"],
        },
    )
    _, verdict = await qa_server.call_tool(
        "qa.submit_verdict",
        {
            "ticket_id": "DEV-102",
            "lease_id": qa_claim["lease"]["lease_id"],
            "candidate_id": candidate["candidate"]["candidate_id"],
            "expected_version": qa_claim["ticket"]["version"],
            "verdict": "passed",
            "findings": [{
                "criterion": "The behavior is demonstrable",
                "status": "verified",
                "severity": "none",
                "observation": "Passed isolated regression coverage.",
                "evidence": [{
                    "type": "test_result",
                    "observation": "passed",
                    "storage_ref": "evidence://qa/DEV-102/run-1.json",
                }],
            }],
        },
    )

    assert verdict["ticket"]["status"] == "VERIFIED"
    context = service.get_ticket_context(planner, "DEV-102")
    assert any(entry["entry_type"] == "qa_test_run" for entry in context["context_entries"])


@pytest.mark.anyio
async def test_invoke_retries_a_database_contention_error_before_succeeding(
    service, actors, monkeypatch
) -> None:
    """A transient ContentionError must never reach the caller as long as a
    later attempt of the same call succeeds within the bounded retry budget.
    """
    calls: list[int] = []

    def flaky_capabilities(_actor):
        calls.append(1)
        if len(calls) < 3:
            raise ContentionError("database is busy")
        return {"ok": True}

    monkeypatch.setattr(service, "capabilities", flaky_capabilities)

    _, result = await create_mcp_server(service, actors["developer"]).call_tool(
        "identity.get_capabilities", {}
    )

    assert len(calls) == 3
    assert result["capabilities"] == {"ok": True}


@pytest.mark.anyio
async def test_invoke_raises_after_exhausting_the_contention_retry_budget(
    service, actors, monkeypatch
) -> None:
    """The retry budget is bounded: persistent contention must still surface as
    the same clean, documented, retryable error -- never an infinite wait.
    """

    def always_busy(_actor):
        raise ContentionError("database is busy")

    monkeypatch.setattr(service, "capabilities", always_busy)

    with pytest.raises(ToolError, match="database is busy"):
        await create_mcp_server(service, actors["developer"]).call_tool(
            "identity.get_capabilities", {}
        )


@pytest.mark.anyio
async def test_invoke_normalizes_a_raw_sqlite_error_instead_of_leaking_it(
    service, actors, monkeypatch
) -> None:
    """A foreign-key or lock error that reaches this boundary without an
    existing service-layer translation must never expose raw driver text
    through MCP.
    """

    def raises_integrity_error(_actor):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    monkeypatch.setattr(service, "capabilities", raises_integrity_error)

    with pytest.raises(ToolError) as excinfo:
        await create_mcp_server(service, actors["developer"]).call_tool(
            "identity.get_capabilities", {}
        )

    assert "foreign key" not in str(excinfo.value).lower()
    assert "constraint" not in str(excinfo.value).lower()


@pytest.mark.anyio
async def test_ticket_context_tool_accepts_a_declared_token_budget(
    service, actors, seeded
) -> None:
    """The caller declares the budget; the packet reports how it was fitted."""
    ticket_id = seeded["ticket"]["ticket_id"]
    developer_server = create_mcp_server(service, actors["developer"])

    _content, default_packet = await developer_server.call_tool(
        "ticket.get_context", {"ticket_ref": f"ticket:{ticket_id}"}
    )
    _bounded_content, bounded_packet = await developer_server.call_tool(
        "ticket.get_context",
        {"ticket_ref": f"ticket:{ticket_id}", "token_budget": 300},
    )

    assert default_packet["token_budget"] > bounded_packet["token_budget"]
    assert bounded_packet["token_budget"] == 300
    assert bounded_packet["context_version"] == bounded_packet["ticket"]["version"]
    assert bounded_packet["context_snapshot_id"].startswith("ctxsnap_")
    assert bounded_packet["generated_at"].endswith("Z")
    assert isinstance(bounded_packet["omitted_sections"], list)

    with pytest.raises(ToolError):
        await developer_server.call_tool(
            "ticket.get_context",
            {"ticket_ref": f"ticket:{ticket_id}", "token_budget": 1},
        )
