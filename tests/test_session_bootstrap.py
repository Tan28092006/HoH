from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, Role
from hoh_control_plane.mcp_server import create_mcp_server
from test_git_worktrees import _repository


def test_fresh_session_discovers_scope_without_claiming(service, seeded, actors):
    bootstrap = service.session_bootstrap(actors["developer"])
    assert bootstrap["selected_project_id"] == "project-1"
    assert bootstrap["assignments"] == []
    assert bootstrap["next_action"] == "register_then_inbox"
    assert not bootstrap["workspace"]["configured"]
    assert service.get_ticket_context(actors["developer"], "DEV-142")["ticket"]["status"] == "READY"


def test_bootstrap_cannot_read_another_project(service, seeded, actors):
    with pytest.raises(AuthorizationError):
        service.session_bootstrap(actors["developer"], "other")
    isolated = Actor("outsider", Role.DEVELOPER, frozenset({"other"}))
    assert service.session_bootstrap(isolated)["projects"] == []


def test_resumed_session_recovers_lease_and_queued_job_without_duplication(service, seeded, actors, tmp_path):
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    claim = service.claim_ticket(actors["developer"], "DEV-142", seeded["ticket"]["version"], 900)
    job = service.queue_worktree_provision(actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"])
    for _ in range(2):
        bootstrap = service.session_bootstrap(actors["developer"])
        assignment = bootstrap["assignments"][0]
        assert assignment["lease"] == claim["lease"]
        assert assignment["jobs"][0]["id"] == job["provision"]["provision_id"]
        assert assignment["worktree"] is None
    assert service.session_bootstrap(actors["developer_2"])["assignments"] == []


def test_remote_workspace_does_not_advertise_a_server_local_checkout(service, seeded, actors, tmp_path):
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)), workspace_model="agent_workspace")
    workspace = service.session_bootstrap(actors["developer"])["workspace"]
    assert workspace["model"] == "agent_workspace"
    assert workspace["repository_path"] is None
    assert not workspace["requires_shared_filesystem"]


@pytest.mark.anyio
async def test_bootstrap_is_available_over_mcp(service, seeded, actors):
    server = create_mcp_server(service, actors["developer"])
    _, result = await server.call_tool("session.bootstrap", {})
    assert result["identity"]["agent_id"] == "dev-01"


def test_planner_resumes_its_existing_decision_job(service, seeded, actors):
    from test_planner_work_queue import _blocked_ticket
    _blocked_ticket(service, actors, seeded)
    jobs = service.discover_planner_jobs(actors["planner"], "project-1")
    job = jobs[0]
    claimed = service.claim_planner_job(actors["planner"], job["planner_job_id"])
    bootstrap = service.session_bootstrap(actors["planner"])
    assert bootstrap["next_action"] == "resume_planner_job"
    assert bootstrap["planner_assignments"][0]["lease_id"] == claimed["lease"]["lease_id"]
