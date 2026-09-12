from __future__ import annotations

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service._common import _iso, _row, require_project


class SessionMixin:
    """A bounded, read-only entry point for a client with no chat history."""

    def session_bootstrap(self, actor: Actor, project_id: str | None = None) -> dict:
        if project_id is not None:
            require_project(actor, project_id)
        with self.store.connection() as connection:
            if project_id is not None:
                projects = [self._one(connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,))]
            elif "*" in actor.project_ids:
                projects = [_row(row) for row in connection.execute(
                    "SELECT * FROM projects ORDER BY project_id LIMIT 101"
                ).fetchall()]
            elif actor.project_ids:
                ids = sorted(actor.project_ids)
                projects = [_row(row) for row in connection.execute(
                    f"SELECT * FROM projects WHERE project_id IN ({','.join('?' for _ in ids)}) "
                    "ORDER BY project_id LIMIT 101", ids
                ).fetchall()]
            else:
                projects = []
            selected = projects[0] if len(projects) == 1 else None
            result = {
                "identity": {"agent_id": actor.agent_id, "role": actor.role.value,
                             "project_ids": sorted(actor.project_ids)},
                "protocol_version": "0.3.0",
                "capabilities": self.capabilities(actor),
                "required_skills": ["hoh-control-plane"] + ([] if actor.role == Role.ADMIN else [f"hoh-{actor.role.value}"]),
                "projects": [{"project_id": p["project_id"], "name": p["name"]} for p in projects[:100]],
                "projects_truncated": len(projects) > 100,
                "selected_project_id": selected["project_id"] if selected else None,
                "workspace": None, "assignments": [], "planner_assignments": [],
                "next_action": "select_project" if selected is None else "register_then_inbox",
            }
            if selected is None:
                return result
            selected_id = selected["project_id"]
            if actor.role == Role.PLANNER:
                result["planner_assignments"] = [_row(row) for row in connection.execute(
                    "SELECT j.planner_job_id, j.ticket_id, j.status, l.lease_id, l.expires_at "
                    "FROM planner_jobs j JOIN planner_job_leases l ON l.planner_job_id = j.planner_job_id "
                    "WHERE j.project_id = ? AND l.agent_id = ? AND l.expires_at > ? "
                    "AND j.status = 'IN_PROGRESS' ORDER BY l.acquired_at LIMIT 100",
                    (selected_id, actor.agent_id, _iso()),
                ).fetchall()]
            repository = _row(connection.execute(
                "SELECT * FROM project_repositories WHERE project_id = ?", (selected_id,)
            ).fetchone())
            colocated = repository is not None and repository["workspace_model"] == "colocated"
            result["workspace"] = {
                "configured": repository is not None,
                "model": repository["workspace_model"] if repository else None,
                "default_branch": selected["default_branch"],
                "repository_path": repository["repository_path"] if colocated else None,
                "requires_shared_filesystem": colocated and actor.role == Role.DEVELOPER,
                "candidate_delivery": "assigned_worktree" if colocated else "commit_reachable_on_service",
            }
            lease_rows = connection.execute(
                "SELECT l.* FROM leases l JOIN tickets t ON t.ticket_id = l.ticket_id "
                "JOIN features f ON f.feature_id = t.feature_id "
                "WHERE f.project_id = ? AND l.agent_id = ? AND l.role = ? AND l.expires_at > ? "
                "ORDER BY l.acquired_at LIMIT 100",
                (selected_id, actor.agent_id, actor.role.value, _iso()),
            ).fetchall()
            for row in lease_rows:
                lease = _row(row)
                ticket_id = lease["ticket_id"]
                jobs = []
                # Fixed identifiers, never interpolated client input.
                for table, key, poll_tool in (
                    ("worktree_provision_jobs", "provision_id", "dev.get_worktree_provision"),
                    ("candidate_submission_jobs", "submission_id", "dev.get_candidate_submission"),
                    ("qa_test_jobs", "test_run_id", "qa.get_project_test_run"),
                    ("merge_jobs", "merge_job_id", "integrator.get_merge_candidate"),
                ):
                    job = _row(connection.execute(
                        f"SELECT {key}, status FROM {table} WHERE ticket_id = ? AND lease_id = ? "
                        "AND requested_by = ? ORDER BY created_at DESC LIMIT 1",
                        (ticket_id, lease["lease_id"], actor.agent_id),
                    ).fetchone())
                    if job:
                        jobs.append({"id": job[key], "id_field": key, "status": job["status"], "poll_tool": poll_tool})
                assignment = _row(connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ? AND agent_id = ?",
                    (ticket_id, actor.agent_id),
                ).fetchone())
                result["assignments"].append({
                    "ticket_ref": f"ticket:{ticket_id}", "lease": lease,
                    "worktree": assignment, "jobs": jobs,
                    "next_action": "refresh_context_and_resume_job" if any(j["status"] in {"QUEUED", "RUNNING"} for j in jobs)
                    else "refresh_context_and_renew_lease",
                })
            if result["assignments"]:
                result["next_action"] = "resume_assignment"
            elif actor.role == Role.PLANNER:
                result["next_action"] = "resume_planner_job" if result["planner_assignments"] else "register_then_discover"
            return result
