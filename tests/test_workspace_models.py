"""Where a Developer's working tree lives, and what the service refuses (HOH-244)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import (
    AuthorizationError,
    ConflictError,
    ValidationError,
    WorkspaceModel,
)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _configured_model(service, project_id: str = "project-1") -> str:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT workspace_model FROM project_repositories WHERE project_id = ?",
            (project_id,),
        ).fetchone()["workspace_model"]


def _claimed(service, actors, seeded):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    return ticket, claim


def test_a_project_configured_without_a_model_is_colocated(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The only model the service could ever serve, so silence means co-located."""
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))

    assert _configured_model(service) == WorkspaceModel.COLOCATED.value


@pytest.mark.parametrize(
    "model", [WorkspaceModel.COLOCATED.value, WorkspaceModel.AGENT_WORKSPACE.value]
)
def test_each_model_can_be_set_explicitly(
    service, actors, seeded, tmp_path: Path, model: str
) -> None:
    service.configure_repository(
        actors["planner"], "project-1", str(_repository(tmp_path)), model
    )

    assert _configured_model(service) == model


def test_an_unknown_model_is_refused(service, actors, seeded, tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as error:
        service.configure_repository(
            actors["planner"], "project-1", str(_repository(tmp_path)), "somewhere-else"
        )

    assert error.value.details["reason"] == "workspace_model_unknown"


def test_colocated_still_provisions_a_worktree_the_agent_can_open(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The original behaviour has to survive the setting being introduced."""
    service.configure_repository(actors["planner"], "project-1", str(_repository(tmp_path)))
    ticket, claim = _claimed(service, actors, seeded)

    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )

    assert Path(prepared["worktree"]["worktree_path"]).is_dir()
    assert prepared["worktree"]["branch_ref"]
    assert prepared["worktree"]["base_commit"]


def test_agent_workspace_refuses_to_provision_a_path_the_caller_cannot_reach(
    service, actors, seeded, tmp_path: Path
) -> None:
    service.configure_repository(
        actors["planner"],
        "project-1",
        str(_repository(tmp_path)),
        WorkspaceModel.AGENT_WORKSPACE.value,
    )
    ticket, claim = _claimed(service, actors, seeded)

    with pytest.raises(ConflictError) as error:
        service.prepare_worktree(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
        )

    assert error.value.details["reason"] == "workspace_model_not_colocated"


def test_agent_workspace_refuses_before_any_provision_job_row_exists(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A refusal that still queued work would leave an orphan job behind."""
    service.configure_repository(
        actors["planner"],
        "project-1",
        str(_repository(tmp_path)),
        WorkspaceModel.AGENT_WORKSPACE.value,
    )
    ticket, claim = _claimed(service, actors, seeded)

    with pytest.raises(ConflictError) as error:
        service.queue_worktree_provision(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
        )

    assert error.value.details["reason"] == "workspace_model_not_colocated"
    with service.store.connection() as connection:
        queued = connection.execute(
            "SELECT COUNT(*) FROM worktree_provision_jobs WHERE ticket_id = ?",
            (ticket["ticket_id"],),
        ).fetchone()[0]
        provisioned = connection.execute(
            "SELECT COUNT(*) FROM worktrees WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0]
    assert queued == 0
    assert provisioned == 0


def test_the_model_cannot_change_while_work_from_the_old_one_is_in_flight(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Switching under a live worktree would strand a path an agent already holds."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket, claim = _claimed(service, actors, seeded)
    service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )

    with pytest.raises(ConflictError) as error:
        service.configure_repository(
            actors["planner"],
            "project-1",
            str(repository),
            WorkspaceModel.AGENT_WORKSPACE.value,
        )

    assert error.value.details["reason"] == "workspace_model_in_flight"
    assert _configured_model(service) == WorkspaceModel.COLOCATED.value


def test_re_stating_the_same_model_is_not_treated_as_a_change(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket, claim = _claimed(service, actors, seeded)
    service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )

    service.configure_repository(
        actors["planner"], "project-1", str(repository), WorkspaceModel.COLOCATED.value
    )

    assert _configured_model(service) == WorkspaceModel.COLOCATED.value


def test_only_a_planner_sets_the_model(service, actors, seeded, tmp_path: Path) -> None:
    with pytest.raises(AuthorizationError):
        service.configure_repository(
            actors["developer"],
            "project-1",
            str(_repository(tmp_path)),
            WorkspaceModel.AGENT_WORKSPACE.value,
        )


def test_the_setting_does_not_touch_candidate_or_branch_authority(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A Developer still cannot move main, whichever model is configured."""
    service.configure_repository(
        actors["planner"],
        "project-1",
        str(_repository(tmp_path)),
        WorkspaceModel.AGENT_WORKSPACE.value,
    )
    ticket = seeded["ticket"]

    with pytest.raises(AuthorizationError):
        service.claim_integration(
            actors["developer"], ticket["ticket_id"], ticket["version"], 900
        )
