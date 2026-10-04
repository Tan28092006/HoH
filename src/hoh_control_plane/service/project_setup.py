"""Project creation and repository configuration."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    Role,
    ValidationError,
    WorkspaceModel,
    _iso,
    _row,
    require_project,
    require_role,
    StoreIntegrityError,
)


class ProjectSetupMixin:
    """Project creation and repository configuration."""

    def create_project(
        self,
        actor: Actor,
        project_id: str,
        name: str,
        description: str,
        default_branch: str,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(project_id, "project_id", 80)
        self._bounded_text(name, "name", 160)
        self._bounded_text(description, "description", 4000, allow_blank=True)
        self._bounded_text(default_branch, "default_branch", 200)
        timestamp = _iso()
        try:
            with self.store.connection(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO projects
                        (project_id, name, description, default_branch, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (project_id, name, description, default_branch, timestamp, timestamp),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "project.created",
                    "project",
                    project_id,
                    1,
                    {"name": name},
                )
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
        except StoreIntegrityError as exc:
            raise ConflictError("Project already exists") from exc
        return project

    def configure_repository(
        self,
        actor: Actor,
        project_id: str,
        repository_path: str,
        workspace_model: str = WorkspaceModel.COLOCATED.value,
    ) -> dict[str, Any]:
        """Attach a checked local Git repository to a project for strict candidate gating.

        workspace_model states where the tree a Developer edits actually lives.
        It defaults to co-located because that is the only model the service can
        serve a worktree for, and because every project configured before the
        setting existed was necessarily co-located.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(repository_path, "repository_path", 1000)
        try:
            model = WorkspaceModel(workspace_model)
        except ValueError as error:
            raise ValidationError(
                f"Unknown workspace_model: {workspace_model}",
                {"reason": "workspace_model_unknown"},
            ) from error
        normalized_path = self.git_worktrees.validate_repository(repository_path)
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            existing_repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if existing_repository and existing_repository["repository_path"] != normalized_path:
                worktree_count = connection.execute(
                    "SELECT COUNT(*) FROM worktrees WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                if worktree_count:
                    raise ConflictError(
                        "Project repository cannot change after ticket worktrees are prepared"
                    )
            previous_model = (
                existing_repository["workspace_model"] if existing_repository else None
            )
            if previous_model is not None and previous_model != model.value:
                # Work already in flight was started under the old model, and its
                # worktree or lease means an agent is holding a path or a claim
                # that the new model would not have handed out.
                stranded = connection.execute(
                    """
                    SELECT COUNT(*) FROM worktrees WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()[0]
                stranded += connection.execute(
                    """
                    SELECT COUNT(*) FROM leases lease
                    JOIN tickets t ON t.ticket_id = lease.ticket_id
                    JOIN features f ON f.feature_id = t.feature_id
                    WHERE f.project_id = ? AND lease.role = ? AND lease.expires_at > ?
                    """,
                    (project_id, Role.DEVELOPER.value, _iso()),
                ).fetchone()[0]
                if stranded:
                    raise ConflictError(
                        "Workspace model cannot change while the project has worktrees "
                        "or active Developer leases from the current model",
                        {"reason": "workspace_model_in_flight"},
                    )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_repositories
                    (project_id, repository_path, workspace_model, configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    repository_path = excluded.repository_path,
                    workspace_model = excluded.workspace_model,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (project_id, normalized_path, model.value, actor.agent_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.repository_configured",
                "project",
                project_id,
                next_version,
                {"configured": True},
            )
            configured_project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
        return {"project": configured_project, "repository_path": normalized_path}
