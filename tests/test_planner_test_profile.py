"""A Planner can read the project's current frozen test command through HoH.

A Planner subagent once had to open the database to learn which command QA
would run. The profile is now in the Planner's session.bootstrap and behind a
read-only Planner tool.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import AuthorizationError
from hoh_control_plane.mcp_server import create_mcp_server


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "profile-repository"
    repository.mkdir()
    for arguments in (
        ["init", "--initial-branch=main"],
        ["config", "user.email", "hoh-tests@example.invalid"],
        ["config", "user.name", "HoH Tests"],
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True)
    (repository / "README.md").write_text("# Profile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "Initial"], check=True, capture_output=True)
    return repository


def test_an_unconfigured_project_says_so(service, actors, seeded) -> None:
    profile = service.get_test_profile(actors["planner"], "project-1")
    assert profile == {
        "project_id": "project-1", "configured": False, "test_command": None,
        "timeout_seconds": None, "configured_at": None, "configured_by": None,
    }
    assert service.session_bootstrap(actors["planner"], "project-1")["test_profile"] == profile


def test_the_planner_reads_the_configured_command_from_bootstrap_and_its_tool(
    service, actors, seeded, tmp_path
) -> None:
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    configured = service.configure_test_command(
        actors["planner"], "project-1", ["python", "-m", "pytest", "-q"], 600
    )

    profile = service.get_test_profile(actors["planner"], "project-1")

    assert profile["configured"] is True
    assert profile["test_command"] == ["python", "-m", "pytest", "-q"]
    assert profile["timeout_seconds"] == 600
    assert profile["configured_at"] == configured["configured_at"]
    assert profile["configured_by"] == actors["planner"].agent_id
    assert service.session_bootstrap(actors["planner"], "project-1")["test_profile"] == profile


@pytest.mark.anyio
async def test_the_profile_tool_is_planner_only(service, actors, seeded, tmp_path) -> None:
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    service.configure_test_command(actors["planner"], "project-1", ["pytest"], 300)

    _content, result = await create_mcp_server(service, actors["planner"]).call_tool(
        "planner.get_test_profile", {"project_id": "project-1"}
    )
    assert result["test_command"] == ["pytest"]
    assert result["timeout_seconds"] == 300

    developer_tools = {
        tool.name for tool in await create_mcp_server(service, actors["developer"]).list_tools()
    }
    assert "planner.get_test_profile" not in developer_tools
    with pytest.raises(AuthorizationError):
        service.get_test_profile(actors["developer"], "project-1")
    assert "test_profile" not in service.session_bootstrap(actors["developer"], "project-1")
