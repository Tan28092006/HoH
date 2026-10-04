"""HOH-279: a file_scope entry ending in "/" means the whole directory."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hoh_control_plane.domain import Role
from hoh_control_plane.scopes import directory_scope
from hoh_control_plane.service import ControlPlaneService


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "scope-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Scope\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial")
    return repository


def _submit_under(service, actors, ticket, relative: str):
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"], claim["ticket"]["version"]
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    target = worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("change\n", encoding="utf-8")
    _git(worktree, "add", relative)
    _git(worktree, "commit", "-m", "Change inside the directory scope")
    return service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Changed a file under the directory scope."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], ticket["ticket_id"]
        )["context_snapshot_id"],
    )


def _project(service, actors, tmp_path):
    planner = actors["planner"]
    service.create_project(planner, project_id="project-1", name="P", description="d", default_branch="main")
    service.create_feature(planner, project_id="project-1", feature_id="f", title="F", objective="o")
    service.configure_repository(planner, "project-1", str(_repository(tmp_path)))


def test_directory_scope_helper() -> None:
    assert directory_scope("migrations/") == "migrations/**"
    assert directory_scope("src\\x\\") == "src/x/**"
    assert directory_scope("src/x/**") == "src/x/**"
    assert directory_scope("README.md") == "README.md"
    assert ControlPlaneService._path_matches_scope("migrations/001.sql", "migrations/")
    assert not ControlPlaneService._path_matches_scope("other/001.sql", "migrations/")


def test_trailing_slash_scope_is_stored_as_globstar_and_accepts_submission(
    service, actors, tmp_path: Path
) -> None:
    _project(service, actors, tmp_path)
    ticket = service.create_ticket(
        actors["planner"], feature_id="f", ticket_id="T-1", title="t", objective="o",
        required_role=Role.DEVELOPER, acceptance_criteria=["works"],
        preservation_constraints=[], file_scope=["src/x/"], ready=True,
    )
    assert ticket["file_scope"] == ["src/x/**"]

    submitted = _submit_under(service, actors, ticket, "src/x/deep/module.py")

    assert submitted["candidate"]["commit_sha"]


def test_update_write_scope_normalises_directory_entries(service, actors, tmp_path: Path) -> None:
    _project(service, actors, tmp_path)
    ticket = service.create_ticket(
        actors["planner"], feature_id="f", ticket_id="T-2", title="t", objective="o",
        required_role=Role.DEVELOPER, acceptance_criteria=["works"],
        preservation_constraints=[], file_scope=["docs/**"], ready=False,
    )
    updated = service.update_ticket_write_scope(
        actors["planner"], "T-2", ticket["version"], ["migrations/"], "widen to migrations"
    )
    assert updated["ticket"]["file_scope"] == ["migrations/**"]


def test_legacy_trailing_slash_row_still_matches_at_submission(
    service, actors, tmp_path: Path
) -> None:
    _project(service, actors, tmp_path)
    ticket = service.create_ticket(
        actors["planner"], feature_id="f", ticket_id="T-3", title="t", objective="o",
        required_role=Role.DEVELOPER, acceptance_criteria=["works"],
        preservation_constraints=[], file_scope=["src/y/**"], ready=True,
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET file_scope = ? WHERE ticket_id = ?",
            (json.dumps(["src/y/"]), "T-3"),
        )

    submitted = _submit_under(service, actors, ticket, "src/y/legacy.py")

    assert submitted["candidate"]["commit_sha"]
