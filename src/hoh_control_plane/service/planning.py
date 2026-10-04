from __future__ import annotations

from hoh_control_plane.service._common import (
    _audit_event_has_field,
    _estimate_tokens,
    _CONTRACT_TOKEN_BUDGET,
    _CONTRACT_TEXT_CHARS,
    Actor,
    Any,
    CLOSED_TICKET_STATUSES,
    CONTEXT_ENTRY_ALIASES,
    CONTEXT_ENTRY_TYPES,
    ConflictError,
    GENERAL_CONTEXT_ENTRY_TYPE,
    NotFoundError,
    RETIRABLE_TICKET_STATUSES,
    Role,
    TicketStatus,
    ValidationError,
    WorkspaceModel,
    _id,
    _iso,
    _row,
    json,
    require_project,
    require_role,
    Connection,
    StoreIntegrityError,
)


class _RepositoryObservationMoved(ConflictError):
    """The stored repository observation changed while a sync inspected Git."""


class PlanningMixin:
    """Planner-authorized project, feature and ticket lifecycle."""

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

    def sync_repository(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Fetch remote truth and open a mandatory Planner reconciliation boundary.

        The observation is compare-and-set against both the remote refs and the
        observed mainline commit. A merge that completes while the fetch runs
        advances the mainline; the sync then retries once from fresh state rather
        than writing the stale SHA back and opening a spurious reconciliation.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        try:
            return self._sync_repository_once(actor, project_id)
        except _RepositoryObservationMoved:
            return self._sync_repository_once(actor, project_id)

    def _sync_repository_once(self, actor: Actor, project_id: str) -> dict[str, Any]:
        with self.store.connection() as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = self._one(
                connection,
                "SELECT * FROM project_repositories WHERE project_id = ?",
                (project_id,),
            )
            previous_refs_raw = repository.get("observed_remote_refs") or "{}"
            previous_refs = json.loads(previous_refs_raw)

        manifest = self.git_worktrees.sync_upstream(
            repository["repository_path"], project["default_branch"]
        )
        document_extensions = {
            ".md", ".mdx", ".rst", ".txt", ".pdf", ".doc", ".docx", ".yaml", ".yml"
        }
        incoming_paths = [row["path"] for row in manifest["incoming_changes"]]
        document_paths = [
            path for path in incoming_paths
            if path.lower().startswith(("docs/", "spec/", "specs/"))
            or any(path.lower().endswith(extension) for extension in document_extensions)
        ]
        manifest = {
            **manifest,
            "document_paths": document_paths,
            "review_required": bool(manifest["behind"]),
        }
        remote_refs = manifest.pop("remote_branches")
        branch_updates = []
        observed_commits: list[dict[str, Any]] = []
        upstream_ref = manifest.get("upstream_ref")
        previous_mainline = repository.get("observed_mainline_commit")
        mainline_update = None
        if previous_mainline != manifest["local_commit"]:
            delta = self.git_worktrees.remote_commit_delta(
                repository["repository_path"], previous_mainline, manifest["local_commit"]
            )
            mainline_update = {
                "branch": f"refs/heads/{project['default_branch']}",
                "previous_commit": previous_mainline,
                "commit": manifest["local_commit"],
                "history_relation": delta["relation"],
                "commits": delta["commits"],
                "changes": delta["changes"],
            }
            for row in delta["commits"]:
                observed_commits.append({
                    **row,
                    "branch": mainline_update["branch"],
                    "relation": delta["relation"],
                })
        default_commits = {
            row["commit"] for row in manifest.get("incoming_commits", [])
        }
        for row in manifest.get("incoming_commits", []):
            observed_commits.append({**row, "branch": upstream_ref, "relation": "incoming"})
        for branch in sorted(set(previous_refs) | set(remote_refs)):
            previous_commit = previous_refs.get(branch)
            commit = remote_refs.get(branch)
            if previous_commit == commit:
                continue
            delta = self.git_worktrees.remote_commit_delta(
                repository["repository_path"], previous_commit, commit,
                baseline_commit=manifest["local_commit"],
            )
            branch_updates.append({
                "branch": branch,
                "previous_commit": previous_commit,
                "commit": commit,
                "change": "new" if previous_commit is None else
                          "deleted" if commit is None else "updated",
                "history_relation": delta["relation"],
                "commits": delta["commits"],
                "changes": delta["changes"],
            })
            for row in delta["commits"]:
                if branch == upstream_ref and row["commit"] in default_commits:
                    continue
                observed_commits.append({**row, "branch": branch, "relation": delta["relation"]})
        branch_document_paths = [
            change["path"]
            for update in branch_updates
            for change in update["changes"]
            if change["path"].lower().startswith(("docs/", "spec/", "specs/"))
            or any(
                change["path"].lower().endswith(extension)
                for extension in document_extensions
            )
        ]
        mainline_document_paths = [
            change["path"]
            for change in (mainline_update or {}).get("changes", [])
            if change["path"].lower().startswith(("docs/", "spec/", "specs/"))
            or any(
                change["path"].lower().endswith(extension)
                for extension in document_extensions
            )
        ]
        manifest["mainline_update"] = mainline_update
        manifest["document_paths"] = list(dict.fromkeys([
            *document_paths, *mainline_document_paths, *branch_document_paths
        ]))
        commits_by_sha: dict[str, dict[str, Any]] = {}
        for item in observed_commits:
            sha = item["commit"]
            if sha not in commits_by_sha:
                commits_by_sha[sha] = {
                    "commit": sha,
                    "subject": item.get("subject", ""),
                    "branches": [item.get("branch")],
                    "relations": [item.get("relation")],
                }
                continue
            if item.get("branch") not in commits_by_sha[sha]["branches"]:
                commits_by_sha[sha]["branches"].append(item.get("branch"))
            if item.get("relation") not in commits_by_sha[sha]["relations"]:
                commits_by_sha[sha]["relations"].append(item.get("relation"))
        observed_commits = list(commits_by_sha.values())
        manifest["branch_updates"] = branch_updates
        manifest["observed_commits"] = observed_commits
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            current = self._one(
                connection,
                "SELECT * FROM project_repositories WHERE project_id = ?",
                (project_id,),
            )
            if current["repository_path"] != repository["repository_path"]:
                raise ConflictError("Project repository changed while its upstream was fetched")
            if (
                (current.get("observed_remote_refs") or "{}") != previous_refs_raw
                or current.get("observed_mainline_commit") != previous_mainline
            ):
                raise _RepositoryObservationMoved(
                    "Repository observation changed while remote refs were inspected; sync again"
                )
            changed = (
                current.get("observed_mainline_commit") != manifest["local_commit"]
                or current.get("observed_upstream_commit") != manifest["upstream_commit"]
            )
            changed = changed or bool(branch_updates)
            has_reconciliation_history = connection.execute(
                "SELECT 1 FROM repository_reconciliations WHERE project_id = ? LIMIT 1",
                (project_id,),
            ).fetchone() is not None
            baseline_reconciliation = not has_reconciliation_history
            if baseline_reconciliation and not observed_commits:
                observed_commits = [{
                    "commit": manifest["local_commit"],
                    "subject": "Current planning baseline",
                    "branches": [f"refs/heads/{project['default_branch']}"],
                    "relations": ["baseline"],
                }]
                manifest["observed_commits"] = observed_commits
            changed = changed or baseline_reconciliation
            manifest["baseline_reconciliation"] = baseline_reconciliation
            manifest["changed"] = changed
            manifest["context_ref"] = None
            manifest["reconciliation_id"] = None
            connection.execute(
                """UPDATE project_repositories
                   SET observed_mainline_commit = ?, observed_upstream_commit = ?,
                       observed_remote_refs = ?, observed_at = ?
                   WHERE project_id = ?""",
                (
                    manifest["local_commit"], manifest["upstream_commit"],
                    json.dumps(remote_refs, sort_keys=True), timestamp, project_id
                ),
            )
            if changed:
                refreshed_project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                next_version = refreshed_project["version"] + 1
                connection.execute(
                    "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                    (next_version, timestamp, project_id),
                )
                entry_id = _id(connection, "ctx")
                reconciliation_id = _id(connection, "recon")
                manifest["context_ref"] = f"ctx:entry:{entry_id}"
                manifest["reconciliation_id"] = reconciliation_id
                open_ticket_versions = {
                    str(row["ticket_id"]): {
                        "version": int(row["version"]),
                        "status": str(row["status"]),
                    }
                    for row in connection.execute(
                        """
                        SELECT t.ticket_id, t.version, t.status
                        FROM tickets t
                        JOIN features f ON f.feature_id = t.feature_id
                        WHERE f.project_id = ?
                          AND t.status NOT IN ('DONE', 'CANCELLED', 'SUPERSEDED')
                        ORDER BY t.ticket_id
                        """,
                        (project_id,),
                    ).fetchall()
                }
                connection.execute(
                    """INSERT INTO context_entries
                       (entry_id, project_id, entry_type, content, source_ref,
                        artifact_commit, actor_id, actor_role, created_at)
                       VALUES (?, ?, 'repository_observation', ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        project_id,
                        json.dumps(manifest, sort_keys=True),
                        f"git:{manifest['local_commit']}...{manifest['upstream_commit'] or manifest['local_commit']}",
                        manifest["upstream_commit"] or manifest["local_commit"],
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO repository_reconciliations
                        (reconciliation_id, project_id, observation_ref, observed_commits,
                         ticket_versions, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                    """,
                    (
                        reconciliation_id,
                        project_id,
                        manifest["context_ref"],
                        json.dumps(observed_commits, sort_keys=True),
                        json.dumps(open_ticket_versions, sort_keys=True),
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE work_offers SET status = 'cancelled', closed_at = ?
                    WHERE status = 'offered' AND queue_id IN (
                        SELECT queue_id FROM work_queue
                        WHERE project_id = ? AND status IN ('pending', 'offered')
                    )
                    """,
                    (timestamp, project_id),
                )
                connection.execute(
                    """
                    UPDATE work_queue SET status = 'pending', updated_at = ?
                    WHERE project_id = ? AND status = 'offered'
                    """,
                    (timestamp, project_id),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "project.repository_observed",
                    "project",
                    project_id,
                    next_version,
                    {
                        "relation": manifest["relation"],
                        "local_commit": manifest["local_commit"],
                        "upstream_commit": manifest["upstream_commit"],
                        "incoming_change_count": len(manifest["incoming_changes"]),
                        "document_change_count": len(manifest["document_paths"]),
                        "branch_update_count": len(branch_updates),
                        "observed_commit_count": len(observed_commits),
                        "reconciliation_id": reconciliation_id,
                    },
                )
            pending = self._repository_reconciliation_state(connection, project_id)
            manifest["reconciliation_required"] = bool(pending["pending"])
            manifest["pending_reconciliations"] = pending["pending"]
        return manifest

    def list_repository_reconciliations(
        self, actor: Actor, project_id: str, status: str = "PENDING", limit: int = 20
    ) -> dict[str, Any]:
        """List immutable Git observations still awaiting a board decision."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        if status not in {"PENDING", "RESOLVED"}:
            raise ValidationError("status must be PENDING or RESOLVED")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be an integer from 1 to 100")
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM repository_reconciliations
                WHERE project_id = ? AND status = ?
                ORDER BY created_at ASC LIMIT ?
                """,
                (project_id, status, limit),
            ).fetchall()
            return {"reconciliations": [self._repository_reconciliation(row) for row in rows]}

    def _open_commit_reconciliation(
        self,
        connection: Connection,
        actor: Actor,
        project_id: str,
        *,
        commit: str,
        subject: str,
        relation: str,
        observation_ref: str,
        branch: str | None = None,
        advance_mainline: bool = False,
        merged_ticket_id: str | None = None,
        changed_paths: list[str] | None = None,
    ) -> str:
        """Open the next Planner turn from a candidate or integration result.

        Repository polling covers teammate branches. This path closes the race
        after an internal QA or merge result: the same transaction that records
        the result also closes execution until Planner has assessed it.
        """
        project = self._one(
            connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        )
        timestamp = _iso()
        observed_commits = [{
            "commit": commit,
            "subject": subject,
            "branches": [branch or f"refs/heads/{project['default_branch']}"],
            "relations": [relation],
        }]
        ticket_versions = {
            str(row["ticket_id"]): {
                "version": int(row["version"]),
                "status": str(row["status"]),
            }
            for row in connection.execute(
                """
                SELECT t.ticket_id, t.version, t.status
                FROM tickets t
                JOIN features f ON f.feature_id = t.feature_id
                WHERE f.project_id = ?
                  AND t.status NOT IN ('DONE', 'CANCELLED', 'SUPERSEDED')
                ORDER BY t.ticket_id
                """,
                (project_id,),
            ).fetchall()
        }
        reconciliation_id = _id(connection, "recon")
        connection.execute(
            """
            INSERT INTO repository_reconciliations
                (reconciliation_id, project_id, observation_ref, observed_commits,
                 ticket_versions, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                reconciliation_id,
                project_id,
                observation_ref,
                json.dumps(observed_commits, sort_keys=True),
                json.dumps(ticket_versions, sort_keys=True),
                timestamp,
            ),
        )
        auto_reason = None
        if merged_ticket_id is not None and changed_paths is not None:
            auto_reason = self._merge_cannot_affect_open_work(
                connection, list(ticket_versions), merged_ticket_id, changed_paths
            )
        if auto_reason is not None:
            manifest = {
                "summary": auto_reason,
                "ticket_decisions": [
                    {"ticket_id": ticket_id, "disposition": "keep", "reason": auto_reason,
                     "decided_by": "system:scope-disjoint"}
                    for ticket_id in sorted(ticket_versions)
                ],
                "new_ticket_ids": [],
            }
            connection.execute(
                """
                UPDATE repository_reconciliations
                SET status = 'RESOLVED', resolved_by = 'system:scope-disjoint',
                    resolved_at = ?, decision_manifest = ?
                WHERE reconciliation_id = ?
                """,
                (timestamp, json.dumps(manifest, sort_keys=True), reconciliation_id),
            )
        if advance_mainline:
            connection.execute(
                """
                UPDATE project_repositories
                SET observed_mainline_commit = ?, observed_at = ?
                WHERE project_id = ?
                """,
                (commit, timestamp, project_id),
            )
        if auto_reason is None:
            connection.execute(
                """
                UPDATE work_offers SET status = 'cancelled', closed_at = ?
                WHERE status = 'offered' AND queue_id IN (
                    SELECT queue_id FROM work_queue
                    WHERE project_id = ? AND status IN ('pending', 'offered')
                )
                """,
                (timestamp, project_id),
            )
            connection.execute(
                """
                UPDATE work_queue SET status = 'pending', updated_at = ?
                WHERE project_id = ? AND status = 'offered'
                """,
                (timestamp, project_id),
            )
        next_version = int(project["version"]) + 1
        connection.execute(
            "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
            (next_version, timestamp, project_id),
        )
        self._event(
            connection,
            actor,
            project_id,
            "project.repository_iteration_opened",
            "project",
            project_id,
            next_version,
            {
                "reconciliation_id": reconciliation_id,
                "observation_ref": observation_ref,
                "commit": commit,
                "relation": relation,
                "auto_resolved": auto_reason is not None,
            },
        )
        return reconciliation_id

    _SHARED_PATH_PREFIXES = ("docs/", "spec/", "specs/")

    def _merge_cannot_affect_open_work(
        self,
        connection: Connection,
        open_ticket_ids: list[str],
        merged_ticket_id: str,
        changed_paths: list[str],
    ) -> str | None:
        """Why a merged commit provably leaves every open ticket as planned, or None.

        Every merge used to stop all execution until Planner restated "keep" for
        each open ticket by hand. When the commit only touches files inside the
        merged ticket's own territory, none of that work can have changed. The
        test is deliberately conservative: an open ticket without a scope, one
        whose scope root overlaps any changed path, one that depends on the merged
        ticket, or a commit touching docs/spec or a repository-root file all leave
        the decision to Planner.
        """
        paths = [path.replace("\\", "/").lstrip("/") for path in changed_paths]
        if not paths:
            return None
        for path in paths:
            if "/" not in path or path.lower().startswith(self._SHARED_PATH_PREFIXES):
                return None
        for ticket_id in open_ticket_ids:
            row = connection.execute(
                "SELECT file_scope FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            ticket_scopes = json.loads(row["file_scope"]) if row and row["file_scope"] else []
            if not ticket_scopes:
                return None
            # The lease collision rule: scope roots compared segment-wise, so a
            # legacy "dir/" entry (HOH-279) still covers files under dir.
            if any(
                self._scope_roots_overlap(str(scope), path)
                for scope in ticket_scopes
                for path in paths
            ):
                return None
            if connection.execute(
                """
                SELECT 1 FROM ticket_dependencies
                WHERE ticket_id = ? AND depends_on_ticket_id = ?
                """,
                (ticket_id, merged_ticket_id),
            ).fetchone() is not None:
                return None
        return (
            f"Merged {merged_ticket_id} changed {len(paths)} path(s) outside every open "
            "ticket's write scope and no open ticket depends on it; recorded as keep "
            "by the server."
        )

    def resolve_repository_reconciliation(
        self,
        actor: Actor,
        project_id: str,
        reconciliation_id: str,
        ticket_decisions: list[dict[str, Any]],
        decision_summary: str,
        new_ticket_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Open execution only after every previously-open ticket was reclassified.

        Ticket lifecycle mutations happen through their dedicated tools.  This
        command checks that the stated disposition matches the resulting board,
        which makes the Git review a real barrier instead of a prompt convention.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(decision_summary, "decision_summary", 4000)
        self._bounded_list(
            ticket_decisions, "ticket_decisions", 500, 4000, strings_only=False
        )
        new_ticket_ids = new_ticket_ids or []
        self._bounded_list(new_ticket_ids, "new_ticket_ids", 100, 100)
        if len(set(new_ticket_ids)) != len(new_ticket_ids):
            raise ValidationError("new_ticket_ids cannot contain duplicates")
        allowed = {"keep", "revise", "cancel", "supersede", "complete", "rework"}
        with self.store.connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM repository_reconciliations WHERE reconciliation_id = ?",
                (reconciliation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Repository reconciliation not found")
            reconciliation = self._repository_reconciliation(row)
            if reconciliation["project_id"] != project_id:
                raise ValidationError("Repository reconciliation belongs to another project")
            if reconciliation["status"] != "PENDING":
                return {"reconciliation": reconciliation, **self._repository_reconciliation_state(connection, project_id)}
            older = connection.execute(
                """
                SELECT reconciliation_id FROM repository_reconciliations
                WHERE project_id = ? AND status = 'PENDING' AND created_at < ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (project_id, reconciliation["created_at"]),
            ).fetchone()
            if older is not None:
                raise ConflictError(
                    "Resolve repository observations in commit order",
                    {"reason": "older_reconciliation_pending", "reconciliation_id": older["reconciliation_id"]},
                )
            decisions: dict[str, dict[str, Any]] = {}
            for item in ticket_decisions:
                if not isinstance(item, dict):
                    raise ValidationError("Each ticket decision must be an object")
                ticket_id = str(item.get("ticket_id", "")).strip()
                disposition = str(item.get("disposition", "")).strip()
                reason = str(item.get("reason", "")).strip()
                if not ticket_id or ticket_id in decisions:
                    raise ValidationError("ticket_decisions require unique ticket_id values")
                if disposition not in allowed:
                    raise ValidationError(
                        "disposition must be keep, revise, cancel, supersede, complete or rework"
                    )
                self._bounded_text(reason, "ticket_decisions.reason", 2000)
                decisions[ticket_id] = {**item, "ticket_id": ticket_id, "disposition": disposition, "reason": reason}
            expected = reconciliation["ticket_versions"]
            missing = sorted(set(expected) - set(decisions))
            extra = sorted(set(decisions) - set(expected))
            if missing or extra:
                raise ValidationError(
                    "Every ticket open at observation time must have exactly one decision",
                    {"missing_ticket_ids": missing, "unexpected_ticket_ids": extra},
                )
            for ticket_id, item in decisions.items():
                ticket = self._ticket(connection, ticket_id)
                if self._project_for_ticket(connection, ticket_id) != project_id:
                    raise ValidationError("Ticket decision refers to another project")
                observed = expected[ticket_id]
                disposition = item["disposition"]
                status_now = ticket["status"]
                version_changed = int(ticket["version"]) > int(observed["version"])
                if disposition == "keep" and status_now in {
                    TicketStatus.DONE.value,
                    TicketStatus.CANCELLED.value,
                    TicketStatus.SUPERSEDED.value,
                }:
                    raise ConflictError(
                        f"{ticket_id} is terminal and cannot be recorded as unchanged"
                    )
                if disposition == "revise" and not version_changed:
                    raise ConflictError(f"{ticket_id} was declared revised but its version did not change")
                if disposition == "cancel" and status_now != TicketStatus.CANCELLED.value:
                    raise ConflictError(f"{ticket_id} must be cancelled before resolving the reconciliation")
                if disposition == "supersede" and status_now != TicketStatus.SUPERSEDED.value:
                    raise ConflictError(f"{ticket_id} must be superseded before resolving the reconciliation")
                if disposition == "supersede":
                    replacement = str(item.get("replacement_ticket_id", "")).strip()
                    if not replacement:
                        raise ValidationError("supersede requires replacement_ticket_id")
                    if self._project_for_ticket(connection, replacement) != project_id:
                        raise ValidationError("Replacement ticket belongs to another project")
                if disposition == "complete" and status_now != TicketStatus.DONE.value:
                    raise ConflictError(f"{ticket_id} must be DONE before resolving the reconciliation")
                if disposition == "rework" and status_now not in {
                    TicketStatus.REWORK.value, TicketStatus.BLOCKED.value
                }:
                    raise ConflictError(f"{ticket_id} must be REWORK or BLOCKED before resolving the reconciliation")
                if disposition == "rework" and not version_changed:
                    raise ConflictError(
                        f"{ticket_id} was declared rework but did not change after the observation"
                    )
            for ticket_id in new_ticket_ids:
                if self._project_for_ticket(connection, ticket_id) != project_id:
                    raise ValidationError("New reconciliation ticket belongs to another project")
                created = self._ticket(connection, ticket_id)
                if created["created_at"] < reconciliation["created_at"]:
                    raise ValidationError(
                        f"{ticket_id} predates this observation and is not a new reconciliation ticket"
                    )
            timestamp = _iso()
            manifest = {
                "summary": decision_summary,
                "ticket_decisions": [decisions[ticket_id] for ticket_id in sorted(decisions)],
                "new_ticket_ids": sorted(new_ticket_ids),
            }
            connection.execute(
                """
                UPDATE repository_reconciliations
                SET status = 'RESOLVED', resolved_by = ?, resolved_at = ?, decision_manifest = ?
                WHERE reconciliation_id = ?
                """,
                (actor.agent_id, timestamp, json.dumps(manifest, sort_keys=True), reconciliation_id),
            )
            self._rearm_work_wakeups(connection, project_id)
            project = self._one(connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,))
            next_version = project["version"] + 1
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, entry_type, content, source_ref,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, 'planner_decision', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, project_id, json.dumps(manifest, sort_keys=True),
                    reconciliation["observation_ref"], actor.agent_id, actor.role.value, timestamp,
                ),
            )
            self._event(
                connection, actor, project_id, "project.repository_reconciled", "project",
                project_id, next_version,
                {"reconciliation_id": reconciliation_id, "decision_ref": f"ctx:entry:{entry_id}"},
            )
            state = self._repository_reconciliation_state(connection, project_id)
            resolved = self._repository_reconciliation(
                connection.execute(
                    "SELECT * FROM repository_reconciliations WHERE reconciliation_id = ?",
                    (reconciliation_id,),
                ).fetchone()
            )
            return {"reconciliation": resolved, **state}

    @staticmethod
    def _repository_reconciliation(row: Any) -> dict[str, Any]:
        value = _row(row) or {}
        for field in ("observed_commits", "ticket_versions", "decision_manifest"):
            if isinstance(value.get(field), str):
                value[field] = json.loads(value[field])
        return value

    def _repository_reconciliation_state(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT reconciliation_id, observation_ref, observed_commits, created_at
            FROM repository_reconciliations
            WHERE project_id = ? AND status = 'PENDING'
            ORDER BY created_at ASC
            """,
            (project_id,),
        ).fetchall()
        pending = []
        for row in rows:
            value = _row(row) or {}
            commits = value.pop("observed_commits")
            value["observed_commit_count"] = len(commits)
            value["commit_refs"] = [item.get("commit") for item in commits[:20]]
            value["opened_at"] = value.pop("created_at", None)
            pending.append(value)
        return {"repository_reconciliation_required": bool(pending), "pending": pending}

    @staticmethod
    def _with_barrier_age(state: dict[str, Any]) -> dict[str, Any]:
        """Annotate a barrier with how long it has been open.

        Only for inbox and dashboard reads: a ticket packet is snapshotted by
        content, and a clock reading inside it would make every read distinct.
        """
        from hoh_control_plane.event_wait import barrier_age_seconds

        pending = [
            {**item, "age_seconds": barrier_age_seconds(item.get("opened_at"))}
            for item in state.get("pending") or []
        ]
        oldest = max((item["age_seconds"] for item in pending), default=0)
        return {**state, "pending": pending, "oldest_age_seconds": oldest}

    def _rearm_work_wakeups(self, connection: Connection, project_id: str) -> None:
        # A wake-up is delivered once per queue item. A barrier cancelled the
        # offers agents were told about, and they acknowledged that work while
        # blocked. Lifting the barrier forgets those deliveries so the reissued
        # offers wake an agent again instead of leaving it idle for hours.
        connection.execute(
            """
            DELETE FROM agent_event_deliveries
            WHERE project_id = ? AND state <> 'pending' AND event_key IN (
                SELECT 'queue:' || queue_id FROM work_queue
                WHERE project_id = ? AND status IN ('pending', 'offered')
            )
            """,
            (project_id, project_id),
        )

    def _require_repository_reconciled(
        self, connection: Connection, project_id: str
    ) -> None:
        state = self._repository_reconciliation_state(connection, project_id)
        if state["repository_reconciliation_required"]:
            raise ConflictError(
                "Repository changed after the current plan was formed; Planner must reconcile every open ticket before execution continues",
                {"reason": "repository_reconciliation_required", **state},
            )

    def configure_test_command(
        self,
        actor: Actor,
        project_id: str,
        test_command: list[str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Register the single shell-free test command frozen into future candidates."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._test_command(test_command)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ValidationError("timeout_seconds must be an integer")
        if not 5 <= timeout_seconds <= 1800:
            raise ValidationError("timeout_seconds must be between 5 and 1800")
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = connection.execute(
                "SELECT project_id FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone()
            if repository is None:
                raise ValidationError(
                    "Planner must configure a project Git repository before its test command"
                )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_test_profiles
                    (project_id, test_command, timeout_seconds, configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    test_command = excluded.test_command,
                    timeout_seconds = excluded.timeout_seconds,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (project_id, json.dumps(test_command), timeout_seconds, actor.agent_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.test_command_configured",
                "project",
                project_id,
                next_version,
                {"configured": True, "timeout_seconds": timeout_seconds},
            )
            profile = self._one(
                connection,
                "SELECT * FROM project_test_profiles WHERE project_id = ?",
                (project_id,),
            )
        return profile

    def configure_verification_profile(
        self,
        actor: Actor,
        project_id: str,
        capabilities: list[str],
        quality_policy: str = "claim-evidence-v1",
    ) -> dict[str, Any]:
        """Declare the observable equipment available to QA for this project.

        The profile describes capabilities, not machine-local commands. Commands
        and credentials stay in their dedicated configuration surfaces, while a
        ticket can state which portable capabilities its evidence plan needs.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_list(capabilities, "capabilities", 50, 100, required=True)
        if quality_policy not in {"legacy", "claim-evidence-v1"}:
            raise ValidationError("quality_policy must be legacy or claim-evidence-v1")
        normalized = sorted(set(capabilities))
        if len(normalized) != len(capabilities):
            raise ValidationError("capabilities cannot contain duplicates")
        for capability in normalized:
            if not capability.replace("_", "").replace("-", "").isalnum():
                raise ValidationError("capabilities must use portable names")
        profile = {
            "schema_version": "1",
            "quality_policy": quality_policy,
            "capabilities": normalized,
        }
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            if quality_policy == "claim-evidence-v1":
                proposed_project = {**project, "verification_profile": profile}
                ready_rows = connection.execute(
                    """
                    SELECT t.* FROM tickets t
                    JOIN features f ON f.feature_id = t.feature_id
                    WHERE f.project_id = ? AND t.status = ?
                    """,
                    (project_id, TicketStatus.READY.value),
                ).fetchall()
                for row in ready_rows:
                    self._assert_quality_ready(proposed_project, _row(row) or {})
            next_version = project["version"] + 1
            connection.execute(
                "UPDATE projects SET verification_profile = ?, version = ?, updated_at = ? "
                "WHERE project_id = ?",
                (json.dumps(profile), next_version, timestamp, project_id),
            )
            self._event(
                connection, actor, project_id, "project.verification_profile_configured",
                "project", project_id, next_version,
                {"quality_policy": quality_policy, "capabilities": normalized},
            )
            return self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )

    def configure_agent_capability_profile(
        self,
        actor: Actor,
        project_id: str,
        agent_id: str,
        capabilities: list[str],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Approve the QA equipment independently verified for one agent.

        Registration remains a session self-report. Dispatch and claim use only
        the intersection of that live declaration and this durable approval, so
        restarting a client cannot grant it new execution authority.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(agent_id, "agent_id", 200)
        self._bounded_list(capabilities, "capabilities", 50, 100)
        self._bounded_list(evidence_refs, "evidence_refs", 20, 1000, required=True)
        normalized = sorted(set(capabilities))
        refs = list(dict.fromkeys(evidence_refs))
        if len(normalized) != len(capabilities):
            raise ValidationError("capabilities cannot contain duplicates")
        if len(refs) != len(evidence_refs):
            raise ValidationError("evidence_refs cannot contain duplicates")
        for capability in normalized:
            if not capability.replace("_", "").replace("-", "").isalnum():
                raise ValidationError("capabilities must use portable names")

        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            available = set((project.get("verification_profile") or {}).get("capabilities") or [])
            unsupported = sorted(set(normalized) - available)
            if unsupported:
                raise ValidationError(
                    "Agent capability approval exceeds the project's verification profile",
                    {"reason": "verification_capabilities_missing", "capabilities": unsupported},
                )
            presence = self._one(
                connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
            )
            if presence["role"] != Role.QA.value:
                raise ValidationError("Only QA agents can receive execution capability approval")
            if project_id not in set(presence.get("project_ids") or []):
                raise ValidationError("QA agent is not registered for this project")
            if not presence.get("machine_id"):
                raise ValidationError(
                    "QA agent must register a stable machine_id before capability approval",
                    {"reason": "capability_machine_missing"},
                )
            # The rule the cards used to state and nothing enforced: approve only
            # what this agent declared and what host-run evidence proves on the
            # machine it is registered from.
            undeclared = sorted(set(normalized) - set(presence.get("execution_capabilities") or []))
            if undeclared:
                raise ValidationError(
                    "Approval may cover only capabilities this QA agent has declared",
                    {"reason": "capability_not_declared", "capabilities": undeclared},
                )
            proven = self._proven_by_preflight(connection, project_id, presence, refs)
            unproven = sorted(set(normalized) - proven)
            if unproven:
                raise ValidationError(
                    "Approval exceeds what the cited host preflight proves",
                    {"reason": "capability_not_proven", "capabilities": unproven},
                )
            connection.execute(
                """UPDATE agent_presence
                   SET approved_execution_capabilities = ?, capability_approval_refs = ?,
                       capability_approved_machine_id = ?, capability_approved_by = ?,
                       capability_approved_at = ?, updated_at = ?
                   WHERE agent_id = ?""",
                (
                    json.dumps(normalized), json.dumps(refs), presence["machine_id"],
                    actor.agent_id, timestamp, timestamp, agent_id,
                ),
            )
            self._event(
                connection, actor, project_id, "agent.capability_profile_configured",
                "agent_presence", agent_id, 1,
                {"capabilities": normalized, "evidence_ref_count": len(refs)},
            )
            resolved = self._resolve_capability_review_jobs(
                connection, actor, project_id, agent_id, set(normalized),
                "Capability approved from host preflight " + ", ".join(refs) + ".",
            )
            return {
                **self._one(
                    connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
                ),
                "resolved_planner_jobs": resolved,
            }

    def _validated_validation_contract(
        self, acceptance_criteria: list[str], contract: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._bounded_list(
            contract, "validation_contract", 30, 1, required=True, strings_only=False
        )
        allowed_methods = {"black_box", "white_box", "mixed"}
        normalized: list[dict[str, Any]] = []
        seen: list[str] = []
        for index, item in enumerate(contract):
            if not isinstance(item, dict):
                raise ValidationError("validation_contract entries must be objects")
            allowed = {
                "criterion", "scenario_ref", "method", "expected_observation",
                "required_evidence_types", "required_capabilities",
            }
            unknown = sorted(set(item) - allowed)
            if unknown:
                raise ValidationError(
                    "validation_contract contains unknown fields", {"fields": unknown}
                )
            criterion = str(item.get("criterion", "")).strip()
            scenario_ref = str(item.get("scenario_ref", "")).strip()
            method = str(item.get("method", "")).strip()
            expected = str(item.get("expected_observation", "")).strip()
            evidence_types = item.get("required_evidence_types") or []
            required_capabilities = item.get("required_capabilities") or []
            # The ticket section is protected from compaction, so every character
            # here is carried by every packet for the ticket's whole life.
            self._bounded_text(criterion, f"validation_contract[{index}].criterion", _CONTRACT_TEXT_CHARS)
            self._bounded_text(scenario_ref, f"validation_contract[{index}].scenario_ref", 200)
            self._bounded_text(expected, f"validation_contract[{index}].expected_observation", _CONTRACT_TEXT_CHARS)
            if method not in allowed_methods:
                raise ValidationError("validation contract method must be black_box, white_box or mixed")
            self._bounded_list(
                evidence_types, f"validation_contract[{index}].required_evidence_types",
                20, 50, required=True,
            )
            self._bounded_list(
                required_capabilities,
                f"validation_contract[{index}].required_capabilities", 20, 100,
            )
            if len(set(evidence_types)) != len(evidence_types):
                raise ValidationError("required_evidence_types cannot contain duplicates")
            seen.append(criterion)
            normalized.append({
                "criterion": criterion,
                "scenario_ref": scenario_ref,
                "method": method,
                "expected_observation": expected,
                "required_evidence_types": sorted(evidence_types),
                "required_capabilities": sorted(set(required_capabilities)),
            })
        if len(seen) != len(set(seen)) or set(seen) != set(acceptance_criteria):
            raise ValidationError(
                "validation contract must cover every acceptance criterion exactly once",
                {"missing_criteria": sorted(set(acceptance_criteria) - set(seen)),
                 "unknown_criteria": sorted(set(seen) - set(acceptance_criteria))},
            )
        estimated = _estimate_tokens(normalized)
        if estimated > _CONTRACT_TOKEN_BUDGET:
            raise ValidationError(
                f"validation contract is about {estimated} tokens; the ticket section is "
                f"never compacted, so keep the whole contract at or below "
                f"{_CONTRACT_TOKEN_BUDGET} tokens by shortening criteria and observations "
                "or splitting the ticket",
                {"contract_estimated_tokens": estimated, "budget": _CONTRACT_TOKEN_BUDGET},
            )
        return normalized

    def configure_validation_contract(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        validation_contract: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """Bind each acceptance criterion to a reproducible QA observation."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {
                TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED,
            }:
                raise ConflictError("Validation contract cannot change during active work")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Validation contract cannot change while a lease is active")
            normalized = self._validated_validation_contract(
                ticket["acceptance_criteria"], validation_contract
            )
            if ticket["status"] == TicketStatus.READY.value:
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                self._assert_quality_ready(
                    project, {**ticket, "validation_contract": normalized}
                )
            next_version = ticket["version"] + 1
            timestamp = _iso()
            connection.execute(
                "UPDATE tickets SET validation_contract = ?, version = ?, updated_at = ? "
                "WHERE ticket_id = ?",
                (json.dumps(normalized), next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'validation_contract', ?, ?, ?, ?)
                """,
                (entry_id, project_id, ticket["feature_id"], ticket_id, reason,
                 actor.agent_id, actor.role.value, timestamp),
            )
            self._event(
                connection, actor, project_id, "ticket.validation_contract_configured",
                "ticket", ticket_id, next_version,
                {"scenario_count": len(normalized), "reason": reason},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "context_entry_id": entry_id,
                "contract_estimated_tokens": _estimate_tokens(normalized),
            }

    @staticmethod
    def _assert_quality_ready(project: dict[str, Any], ticket: dict[str, Any]) -> None:
        profile = project.get("verification_profile") or {}
        if profile.get("quality_policy") != "claim-evidence-v1":
            return
        contract = ticket.get("validation_contract") or []
        if not contract:
            raise ValidationError(
                "A validation contract is required before this ticket can be ready",
                {"reason": "validation_contract_missing", "ticket_id": ticket["ticket_id"]},
            )
        available = set(profile.get("capabilities") or [])
        required = {
            capability
            for scenario in contract
            for capability in scenario.get("required_capabilities", [])
        }
        missing = sorted(required - available)
        if missing:
            raise ValidationError(
                "Project verification profile lacks capabilities required by this ticket",
                {"reason": "verification_capabilities_missing", "capabilities": missing},
            )

    def configure_skill_policy(
        self,
        actor: Actor,
        project_id: str,
        accepted_protocol_versions: list[str],
        accepted_skill_versions: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Declare which agent protocol and skill versions a project currently accepts.

        This is mutable policy data, kept in the project's own record rather
        than in any local skill file, so it can be changed without touching or
        redistributing skill assets.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_list(accepted_protocol_versions, "accepted_protocol_versions", 20, 50)
        if not isinstance(accepted_skill_versions, dict):
            raise ValidationError("accepted_skill_versions must be an object")
        if len(accepted_skill_versions) > 50:
            raise ValidationError("accepted_skill_versions supports at most 50 skills")
        normalized_skill_versions: dict[str, list[str]] = {}
        for skill_name, versions in accepted_skill_versions.items():
            self._bounded_text(skill_name, "accepted_skill_versions key", 100)
            self._bounded_list(
                versions, f"accepted_skill_versions[{skill_name}]", 20, 50, required=True
            )
            normalized_skill_versions[skill_name] = versions
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_skill_policies
                    (project_id, accepted_protocol_versions, accepted_skill_versions,
                     configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    accepted_protocol_versions = excluded.accepted_protocol_versions,
                    accepted_skill_versions = excluded.accepted_skill_versions,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (
                    project_id,
                    json.dumps(accepted_protocol_versions),
                    json.dumps(normalized_skill_versions),
                    actor.agent_id,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.skill_policy_configured",
                "project",
                project_id,
                next_version,
                {
                    "accepted_protocol_versions": accepted_protocol_versions,
                    "accepted_skill_versions": normalized_skill_versions,
                },
            )
            policy = self._one(
                connection,
                "SELECT * FROM project_skill_policies WHERE project_id = ?",
                (project_id,),
            )
            configured_project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
        return {"project": configured_project, "policy": policy}

    def get_skill_compatibility(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Read a project's accepted versions plus each known agent's compatibility.

        Open to every authenticated role with project access: this exposes only
        already-non-secret registration data, not a Planner-only mutation.
        """
        require_project(actor, project_id)
        with self.store.connection() as connection:
            policy = _row(
                connection.execute(
                    "SELECT * FROM project_skill_policies WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            rows = connection.execute(
                "SELECT * FROM agent_presence WHERE expires_at > ? ORDER BY agent_id",
                (_iso(),),
            ).fetchall()
            agents = []
            for row in rows:
                presence = _row(row)
                if (
                    project_id not in presence["project_ids"]
                    and "*" not in presence["project_ids"]
                ):
                    continue
                agents.append(
                    {
                        "agent_id": presence["agent_id"],
                        "role": presence["role"],
                        "protocol_version": presence["protocol_version"],
                        "skill_manifest": presence["skill_manifest"],
                        "compatibility": presence["skill_compatibility"].get(project_id),
                    }
                )
        return {"project_id": project_id, "policy": policy, "agents": agents}

    def create_feature(
        self,
        actor: Actor,
        project_id: str,
        feature_id: str,
        title: str,
        objective: str,
        parent_feature_id: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(feature_id, "feature_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        if parent_feature_id is not None:
            self._bounded_text(parent_feature_id, "parent_feature_id", 100)
        self._priority(priority)
        timestamp = _iso()
        try:
            with self.store.connection(immediate=True) as connection:
                if parent_feature_id:
                    parent = self._one(
                        connection,
                        "SELECT project_id FROM features WHERE feature_id = ?",
                        (parent_feature_id,),
                    )
                    if parent["project_id"] != project_id:
                        raise ValidationError("Parent feature belongs to another project")
                connection.execute(
                    """
                    INSERT INTO features
                        (feature_id, project_id, parent_feature_id, title, objective,
                         priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feature_id,
                        project_id,
                        parent_feature_id,
                        title,
                        objective,
                        priority,
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "feature.created",
                    "feature",
                    feature_id,
                    1,
                    {"title": title},
                )
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
        except StoreIntegrityError as exc:
            raise ConflictError("Feature already exists or project is missing") from exc
        return feature

    def create_ticket(
        self,
        actor: Actor,
        feature_id: str,
        ticket_id: str,
        title: str,
        objective: str,
        required_role: Role,
        acceptance_criteria: list[str],
        preservation_constraints: list[str],
        file_scope: list[str],
        ready: bool = False,
        ticket_type: str = "feature_work",
        priority: int = 50,
        parent_ticket_id: str | None = None,
        dependency_ids: list[str] | None = None,
        validation_contract: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        self._bounded_text(ticket_type, "ticket_type", 50)
        self._priority(priority)
        self._bounded_list(acceptance_criteria, "acceptance_criteria", 30, 2000, required=True)
        self._bounded_list(preservation_constraints, "preservation_constraints", 30, 2000)
        self._bounded_list(file_scope, "file_scope", 100, 1000)
        if file_scope:
            file_scope = [self._directory_scope(scope) for scope in file_scope]
        normalized_contract = (
            self._validated_validation_contract(acceptance_criteria, validation_contract)
            if validation_contract is not None else []
        )
        if required_role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise ValidationError("Ticket must target an executable role")
        dependency_ids = dependency_ids or []
        self._bounded_list(dependency_ids, "dependency_ids", 100, 100)
        timestamp = _iso()
        try:
            with self.store.connection(immediate=True) as connection:
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                status = TicketStatus.READY if ready else TicketStatus.BACKLOG
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                planned_ticket = {
                    "ticket_id": ticket_id,
                    "validation_contract": normalized_contract,
                }
                if ready:
                    self._require_repository_reconciled(connection, project_id)
                    self._assert_quality_ready(project, planned_ticket)
                connection.execute(
                    """
                    INSERT INTO tickets
                        (ticket_id, feature_id, parent_ticket_id, ticket_type, title,
                         objective, status, priority, required_role, acceptance_criteria,
                         validation_contract,
                         preservation_constraints, file_scope, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        feature_id,
                        parent_ticket_id,
                        ticket_type,
                        title,
                        objective,
                        status.value,
                        priority,
                        required_role.value,
                        json.dumps(acceptance_criteria),
                        json.dumps(normalized_contract),
                        json.dumps(preservation_constraints),
                        json.dumps(file_scope),
                        timestamp,
                        timestamp,
                    ),
                )
                for dependency_id in dependency_ids:
                    if dependency_id == ticket_id:
                        raise ValidationError("Ticket cannot depend on itself")
                    connection.execute(
                        """
                        INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id)
                        VALUES (?, ?)
                        """,
                        (ticket_id, dependency_id),
                    )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "ticket.ready" if ready else "ticket.created",
                    "ticket",
                    ticket_id,
                    1,
                    {"title": title, "required_role": required_role.value},
                )
                ticket = self._ticket(connection, ticket_id)
                if ready:
                    self._queue_ticket(connection, actor, project_id, ticket)
        except StoreIntegrityError as exc:
            raise ConflictError("Ticket already exists or dependency is invalid") from exc
        return ticket

    def set_ticket_ready(
        self, actor: Actor, ticket_id: str, expected_version: int
    ) -> dict[str, Any]:
        """Move a planned ticket into the executable queue."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Only backlog or blocked tickets can be made ready")
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            self._assert_quality_ready(project, ticket)
            next_version = ticket["version"] + 1
            timestamp = _iso()
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.READY.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection, actor, project_id, "ticket.ready", "ticket", ticket_id,
                next_version, {"previous_status": ticket["status"]},
            )
            ready_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, ready_ticket)
            return {"ticket": ready_ticket}

    def update_ticket_write_scope(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        file_scope: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Update planned or blocked ticket scope with optimistic concurrency and audit evidence."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_list(file_scope, "file_scope", 100, 1000, required=True)
        self._bounded_text(reason, "reason", 2000)
        normalized_scope = [self._repository_relative_scope(scope) for scope in file_scope]
        if len(set(normalized_scope)) != len(normalized_scope):
            raise ValidationError("file_scope cannot contain duplicate paths")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Write scope cannot change during active development or QA")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Write scope cannot change while a ticket has an active lease")

            previous_scope = ticket["file_scope"]
            if normalized_scope == previous_scope:
                raise ValidationError("file_scope is unchanged")
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets SET file_scope = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (json.dumps(normalized_scope), next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'write_scope_updated', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    f"Planner updated write scope: {reason}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.write_scope_updated",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_file_scope": previous_scope,
                    "file_scope": normalized_scope,
                    "reason": reason,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "previous_file_scope": previous_scope,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def update_ticket_fields(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        title: str | None = None,
        objective: str | None = None,
        priority: int | None = None,
        acceptance_criteria: list[str] | None = None,
        validation_contract: list[dict[str, Any]] | None = None,
        preservation_constraints: list[str] | None = None,
        dependency_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update bounded planning fields on an eligible ticket with optimistic concurrency."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        if title is not None:
            self._bounded_text(title, "title", 240)
        if objective is not None:
            self._bounded_text(objective, "objective", 8000)
        if priority is not None:
            self._priority(priority)
        if acceptance_criteria is not None:
            self._bounded_list(acceptance_criteria, "acceptance_criteria", 30, 2000, required=True)
        if preservation_constraints is not None:
            self._bounded_list(preservation_constraints, "preservation_constraints", 30, 2000)
        normalized_dependency_ids: list[str] | None = None
        if dependency_ids is not None:
            self._bounded_list(dependency_ids, "dependency_ids", 100, 100)
            normalized_dependency_ids = list(dict.fromkeys(dependency_ids))
            if ticket_id in normalized_dependency_ids:
                raise ValidationError("Ticket cannot depend on itself")
        if all(
            value is None
            for value in (
                title, objective, priority, acceptance_criteria, validation_contract,
                preservation_constraints, dependency_ids,
            )
        ):
            raise ValidationError("At least one field must be provided to update")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {
                TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED,
            }:
                raise ConflictError(
                    "Only backlog, ready or blocked tickets can have their fields updated"
                )
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError(
                    "Ticket fields cannot change while a ticket has an active lease"
                )

            normalized_contract = None
            if validation_contract is not None:
                normalized_contract = self._validated_validation_contract(
                    acceptance_criteria or ticket["acceptance_criteria"], validation_contract
                )
            elif acceptance_criteria is not None and ticket.get("validation_contract"):
                raise ValidationError(
                    "Changing acceptance criteria requires a replacement validation contract"
                )
            if ticket["status"] == TicketStatus.READY.value and (
                acceptance_criteria is not None or normalized_contract is not None
            ):
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                self._assert_quality_ready(
                    project,
                    {
                        **ticket,
                        "acceptance_criteria": acceptance_criteria or ticket["acceptance_criteria"],
                        "validation_contract": (
                            normalized_contract
                            if normalized_contract is not None
                            else ticket.get("validation_contract") or []
                        ),
                    },
                )

            if normalized_dependency_ids is not None:
                unfinished_new_dependencies = []
                for dependency_id in normalized_dependency_ids:
                    dependency = self._one(
                        connection, "SELECT * FROM tickets WHERE ticket_id = ?", (dependency_id,)
                    )
                    if self._project_for_ticket(connection, dependency_id) != project_id:
                        raise ValidationError(
                            f"Dependency {dependency_id} must belong to the same project"
                        )
                    if self._dependency_would_cycle(connection, ticket_id, dependency_id):
                        raise ValidationError(
                            f"Dependency on {dependency_id} would create a dependency cycle"
                        )
                    if dependency["status"] != TicketStatus.DONE.value:
                        unfinished_new_dependencies.append(dependency_id)
                if (
                    TicketStatus(ticket["status"]) is TicketStatus.READY
                    and unfinished_new_dependencies
                ):
                    raise ConflictError(
                        "Cannot leave a ready ticket depending on unfinished work: "
                        + ", ".join(unfinished_new_dependencies)
                    )

            timestamp = _iso()
            next_version = ticket["version"] + 1
            set_clauses = ["version = ?", "updated_at = ?"]
            params: list[Any] = [next_version, timestamp]
            changed_fields = []
            if title is not None:
                set_clauses.append("title = ?")
                params.append(title)
                changed_fields.append("title")
            if objective is not None:
                set_clauses.append("objective = ?")
                params.append(objective)
                changed_fields.append("objective")
            if priority is not None:
                set_clauses.append("priority = ?")
                params.append(priority)
                changed_fields.append("priority")
            if acceptance_criteria is not None:
                set_clauses.append("acceptance_criteria = ?")
                params.append(json.dumps(acceptance_criteria))
                changed_fields.append("acceptance_criteria")
            if normalized_contract is not None:
                set_clauses.append("validation_contract = ?")
                params.append(json.dumps(normalized_contract))
                changed_fields.append("validation_contract")
            if preservation_constraints is not None:
                set_clauses.append("preservation_constraints = ?")
                params.append(json.dumps(preservation_constraints))
                changed_fields.append("preservation_constraints")
            params.append(ticket_id)
            connection.execute(
                f"UPDATE tickets SET {', '.join(set_clauses)} WHERE ticket_id = ?", params
            )
            if normalized_dependency_ids is not None:
                connection.execute(
                    "DELETE FROM ticket_dependencies WHERE ticket_id = ?", (ticket_id,)
                )
                for dependency_id in normalized_dependency_ids:
                    connection.execute(
                        """
                        INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id)
                        VALUES (?, ?)
                        """,
                        (ticket_id, dependency_id),
                    )
                changed_fields.append("dependency_ids")

            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'ticket_fields_updated', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.fields_updated",
                "ticket",
                ticket_id,
                next_version,
                {"changed_fields": changed_fields, "reason": reason},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "changed_fields": changed_fields,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def split_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        replacement_ticket_ids: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Retire a ticket whose scope is now carried by two or more narrower tickets."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        self._bounded_list(
            replacement_ticket_ids, "replacement_ticket_ids", 20, 100, required=True
        )
        if len(replacement_ticket_ids) < 2:
            raise ValidationError("Splitting a ticket requires at least two replacement tickets")
        if len(set(replacement_ticket_ids)) != len(replacement_ticket_ids):
            raise ValidationError("replacement_ticket_ids cannot contain duplicates")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in RETIRABLE_TICKET_STATUSES:
                raise ConflictError("Only backlog, ready or blocked tickets can be split")
            self._require_no_active_work(connection, ticket_id)
            replacements = [
                self._retirement_replacement(connection, ticket_id, project_id, replacement_id)
                for replacement_id in replacement_ticket_ids
            ]

            timestamp = _iso()
            next_version = ticket["version"] + 1
            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (TicketStatus.SUPERSEDED.value, next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            source_ref = ",".join(f"ticket:{rid}" for rid in replacement_ticket_ids)
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'ticket_split', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    source_ref,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            # Dependents of retired work stay blocked until a Planner rewires them;
            # naming them here keeps that consequence visible instead of silent.
            dependents = [
                str(row["ticket_id"])
                for row in connection.execute(
                    """
                    SELECT ticket_id FROM ticket_dependencies
                    WHERE depends_on_ticket_id = ? ORDER BY ticket_id
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            self._event(
                connection,
                actor,
                project_id,
                "ticket.split",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "replacement_ticket_ids": replacement_ticket_ids,
                    "closed_offers": closed_offers,
                    "dependent_ticket_ids": dependents,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "replacements": replacements,
                "closed_offers": closed_offers,
                "dependent_ticket_ids": dependents,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def stop_ticket_for_reconciliation(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reconciliation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Revoke stale execution while preserving its worktree and frozen artifacts."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = self._require_pending_reconciliation(
                connection, project_id, reconciliation_id
            )
            if ticket["status"] not in {
                TicketStatus.DEV_IN_PROGRESS.value,
                TicketStatus.QA_IN_PROGRESS.value,
                TicketStatus.MERGE_READY.value,
            }:
                raise ConflictError(
                    "Only active Development, QA or Integration work can be stopped for reconciliation"
                )
            lease = _row(
                connection.execute(
                    "SELECT * FROM leases WHERE ticket_id = ? AND expires_at > ?",
                    (ticket_id, _iso()),
                ).fetchone()
            )
            if lease is None:
                raise ConflictError("Ticket has no live execution lease to stop")
            for table in (
                "worktree_provision_jobs",
                "candidate_submission_jobs",
                "qa_test_jobs",
                "merge_jobs",
            ):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (ticket_id,),
                ).fetchone() is not None:
                    raise ConflictError(
                        "A detached worker is still running; wait for its guarded terminal result before stopping the ticket",
                        {"reason": "reconciliation_worker_active", "table": table},
                    )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            blocked_reason = f"repository_reconciliation/{reconciliation_id}: {reason}"
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (
                    TicketStatus.BLOCKED.value,
                    blocked_reason,
                    next_version,
                    timestamp,
                    ticket_id,
                ),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    blocked_reason,
                    reconciliation["observation_ref"],
                    ticket.get("candidate_commit") or ticket.get("base_commit"),
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.reconciliation_stopped",
                "ticket",
                ticket_id,
                next_version,
                {
                    "reconciliation_id": reconciliation_id,
                    "previous_status": ticket["status"],
                    "revoked_lease_id": lease["lease_id"],
                    "previous_agent_id": lease["agent_id"],
                    "context_ref": f"ctx:entry:{entry_id}",
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "preserved_worktree": _row(
                    connection.execute(
                        "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                    ).fetchone()
                ),
                "preserved_candidate_commit": ticket.get("candidate_commit"),
                "context_ref": f"ctx:entry:{entry_id}",
            }

    def cancel_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire obsolete unclaimed work so dispatch stops offering it."""
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.CANCELLED,
            entry_type="ticket_cancelled",
            event_type="ticket.cancelled",
            reconciliation_id=reconciliation_id,
        )

    def retire_ticket_for_reconciliation(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reconciliation_id: str,
        reason: str,
        target_status: str = TicketStatus.CANCELLED.value,
        replacement_ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Fence obsolete work in any phase and retire it after Git is certain.

        Non-merge workers can be revoked immediately because they reauthorize
        before publishing. A RUNNING merge may already have moved the default
        branch, so its retirement remains PENDING until the supervisor reconciles
        the durable merge intent. Artifacts and worktrees are never deleted.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        if target_status not in {
            TicketStatus.CANCELLED.value,
            TicketStatus.SUPERSEDED.value,
        }:
            raise ValidationError("target_status must be CANCELLED or SUPERSEDED")
        if target_status == TicketStatus.SUPERSEDED.value and not replacement_ticket_id:
            raise ValidationError("SUPERSEDED retirement requires replacement_ticket_id")
        if target_status == TicketStatus.CANCELLED.value and replacement_ticket_id is not None:
            raise ValidationError("CANCELLED retirement cannot name a replacement ticket")
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = self._require_pending_reconciliation(
                connection, project_id, reconciliation_id
            )
            if ticket["status"] in {
                TicketStatus.DONE.value,
                TicketStatus.CANCELLED.value,
                TicketStatus.SUPERSEDED.value,
            }:
                raise ConflictError("A terminal ticket cannot start a retirement request")
            self._retirement_replacement(
                connection, ticket_id, project_id, replacement_ticket_id
            )
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM ticket_retirement_requests
                    WHERE ticket_id = ? AND status = 'PENDING'
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if existing is not None:
                same = (
                    existing["reconciliation_id"] == reconciliation_id
                    and existing["target_status"] == target_status
                    and existing.get("replacement_ticket_id") == replacement_ticket_id
                    and existing["reason"] == reason
                )
                if not same:
                    raise ConflictError("Ticket already has a different pending retirement")
                return {
                    "retirement": existing,
                    "ticket": ticket,
                    "pending_merge_reconciliation": self._ticket_has_running_merge(
                        connection, ticket_id
                    ),
                }
            timestamp = _iso()
            request_id = _id(connection, "retire")
            connection.execute(
                """
                INSERT INTO ticket_retirement_requests
                    (request_id, project_id, ticket_id, reconciliation_id, target_status,
                     replacement_ticket_id, reason, requested_by, status, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    request_id,
                    project_id,
                    ticket_id,
                    reconciliation_id,
                    target_status,
                    replacement_ticket_id,
                    reason,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            self._fence_ticket_for_retirement(connection, ticket_id, request_id, timestamp)
            self._event(
                connection,
                actor,
                project_id,
                "ticket.retirement_requested",
                "ticket",
                ticket_id,
                ticket["version"],
                {
                    "request_id": request_id,
                    "reconciliation_id": reconciliation["reconciliation_id"],
                    "target_status": target_status,
                    "previous_status": ticket["status"],
                },
            )
        self.finalize_pending_ticket_retirements(
            Actor("retirement-finalizer", Role.ADMIN, frozenset({"*"})),
            request_id=request_id,
        )
        return self.get_ticket_retirement(actor, request_id)

    def get_ticket_retirement(self, actor: Actor, request_id: str) -> dict[str, Any]:
        """Read one retirement request and opportunistically settle it."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection() as connection:
            request = self._one(
                connection,
                "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                (request_id,),
            )
            require_project(actor, request["project_id"])
        if request["status"] == "PENDING":
            self.finalize_pending_ticket_retirements(
                Actor("retirement-finalizer", Role.ADMIN, frozenset({"*"})),
                request_id=request_id,
            )
        with self.store.connection() as connection:
            request = self._one(
                connection,
                "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                (request_id,),
            )
            return {
                "retirement": request,
                "ticket": self._ticket(connection, request["ticket_id"]),
                "pending_merge_reconciliation": self._ticket_has_running_merge(
                    connection, request["ticket_id"]
                ),
            }

    def finalize_pending_ticket_retirements(
        self, actor: Actor, request_id: str | None = None
    ) -> dict[str, Any]:
        """Supervisor/operator settlement after every possibly-running merge stops."""
        require_role(actor, Role.ADMIN)
        settled: list[dict[str, Any]] = []
        with self.store.connection(immediate=True) as connection:
            parameters: tuple[Any, ...] = ()
            where = "status = 'PENDING'"
            if request_id is not None:
                where += " AND request_id = ?"
                parameters = (request_id,)
            requests = [
                _row(row) or {}
                for row in connection.execute(
                    f"""
                    SELECT * FROM ticket_retirement_requests
                    WHERE {where} ORDER BY requested_at ASC LIMIT 100
                    """,
                    parameters,
                ).fetchall()
            ]
            for request in requests:
                ticket_id = request["ticket_id"]
                if self._ticket_has_running_merge(connection, ticket_id):
                    continue
                ticket = self._ticket(connection, ticket_id)
                timestamp = _iso()
                if ticket["status"] == TicketStatus.DONE.value:
                    outcome = (
                        "The merge reached the default branch before cancellation completed; "
                        "the ticket remains DONE and Planner must decide whether a revert is needed."
                    )
                    connection.execute(
                        """
                        UPDATE ticket_retirement_requests
                        SET status = 'LANDED', completed_at = ?, outcome = ?
                        WHERE request_id = ?
                        """,
                        (timestamp, outcome, request["request_id"]),
                    )
                    self._event(
                        connection, actor, request["project_id"],
                        "ticket.retirement_landed", "ticket", ticket_id,
                        ticket["version"], {"request_id": request["request_id"]},
                    )
                    settled.append(self._one(
                        connection,
                        "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                        (request["request_id"],),
                    ))
                    continue
                if ticket["status"] in {
                    TicketStatus.CANCELLED.value,
                    TicketStatus.SUPERSEDED.value,
                }:
                    final_status = (
                        "APPLIED" if ticket["status"] == request["target_status"] else "FAILED"
                    )
                    connection.execute(
                        """
                        UPDATE ticket_retirement_requests
                        SET status = ?, completed_at = ?, outcome = ? WHERE request_id = ?
                        """,
                        (
                            final_status,
                            timestamp,
                            f"Ticket was already terminal as {ticket['status']}.",
                            request["request_id"],
                        ),
                    )
                    settled.append(self._one(
                        connection,
                        "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                        (request["request_id"],),
                    ))
                    continue
                self._fence_ticket_for_retirement(
                    connection, ticket_id, request["request_id"], timestamp
                )
                next_version = int(ticket["version"]) + 1
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (request["target_status"], next_version, timestamp, ticket_id),
                )
                entry_id = _id(connection, "ctx")
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type,
                         content, source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        request["project_id"],
                        ticket["feature_id"],
                        ticket_id,
                        (
                            request["reason"]
                            + (
                                f" Replacement ticket: {request['replacement_ticket_id']}."
                                if request.get("replacement_ticket_id") else ""
                            )
                        ),
                        f"retirement:{request['request_id']}",
                        ticket.get("candidate_commit") or ticket.get("base_commit"),
                        request["requested_by"],
                        Role.PLANNER.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_retirement_requests
                    SET status = 'APPLIED', completed_at = ?, outcome = ?
                    WHERE request_id = ?
                    """,
                    (
                        timestamp,
                        f"Ticket retired as {request['target_status']}; mutable artifacts were preserved.",
                        request["request_id"],
                    ),
                )
                open_planner_jobs = [
                    str(row["planner_job_id"])
                    for row in connection.execute(
                        """
                        SELECT planner_job_id FROM planner_jobs
                        WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')
                        """,
                        (ticket_id,),
                    ).fetchall()
                ]
                if open_planner_jobs:
                    placeholders = ",".join("?" for _ in open_planner_jobs)
                    connection.execute(
                        f"DELETE FROM planner_job_leases WHERE planner_job_id IN ({placeholders})",
                        tuple(open_planner_jobs),
                    )
                    connection.execute(
                        f"""
                        UPDATE planner_jobs SET status = 'RESOLVED', resolution = ?,
                            resolved_by = ?, resolved_at = ?, updated_at = ?
                        WHERE planner_job_id IN ({placeholders})
                        """,
                        (
                            f"Ticket retired by {request['request_id']}.",
                            request["requested_by"],
                            timestamp,
                            timestamp,
                            *open_planner_jobs,
                        ),
                    )
                self._event(
                    connection,
                    actor,
                    request["project_id"],
                    "ticket.retirement_applied",
                    "ticket",
                    ticket_id,
                    next_version,
                    {
                        "request_id": request["request_id"],
                        "target_status": request["target_status"],
                        "context_ref": f"ctx:entry:{entry_id}",
                        "resolved_planner_jobs": open_planner_jobs,
                    },
                )
                settled.append(self._one(
                    connection,
                    "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                    (request["request_id"],),
                ))
        return {"settled": settled, "count": len(settled)}

    @staticmethod
    def _ticket_has_running_merge(connection: Connection, ticket_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM merge_jobs WHERE ticket_id = ? AND status = 'RUNNING'",
            (ticket_id,),
        ).fetchone() is not None

    def _fence_ticket_for_retirement(
        self, connection: Connection, ticket_id: str, request_id: str, timestamp: str
    ) -> None:
        message = (
            f"Planner retirement {request_id} revoked execution; mutable artifacts remain preserved."
        )
        for table in ("worktree_provision_jobs", "candidate_submission_jobs"):
            connection.execute(
                f"""
                UPDATE {table} SET status = 'FAILED', completed_at = ?, error_message = ?
                WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (timestamp, message, ticket_id),
            )
        connection.execute(
            """
            UPDATE qa_test_jobs SET status = 'STALE', completed_at = ?, error_message = ?
            WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (timestamp, message, ticket_id),
        )
        connection.execute(
            """
            UPDATE merge_jobs SET status = 'FAILED', completed_at = ?, error_message = ?
            WHERE ticket_id = ? AND status = 'QUEUED'
            """,
            (timestamp, message, ticket_id),
        )
        connection.execute("DELETE FROM worktree_ownership WHERE ticket_id = ?", (ticket_id,))
        connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))

    def supersede_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        replacement_ticket_id: str,
        reason: str,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire work that a named same-project replacement ticket now carries."""
        self._bounded_text(replacement_ticket_id, "replacement_ticket_id", 100)
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.SUPERSEDED,
            entry_type="ticket_superseded",
            event_type="ticket.superseded",
            replacement_ticket_id=replacement_ticket_id,
            reconciliation_id=reconciliation_id,
        )

    def _retire_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        *,
        next_status: TicketStatus,
        entry_type: str,
        event_type: str,
        replacement_ticket_id: str | None = None,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one ticket's dispatch without rewriting active or evidenced work.

        Nothing is deleted: the ticket, its context, candidates and audit history stay
        readable. Only the queue rows that would keep offering it are closed, in the
        same transaction as the status change, so no agent can be handed retired work.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = None
            if reconciliation_id is not None:
                reconciliation = self._require_pending_reconciliation(
                    connection, project_id, reconciliation_id
                )
            reconciliation_retirable = {
                TicketStatus.BACKLOG,
                TicketStatus.READY,
                TicketStatus.BLOCKED,
                TicketStatus.REWORK,
                TicketStatus.QA_READY,
                TicketStatus.VERIFIED,
                TicketStatus.MERGE_READY,
            }
            allowed_statuses = (
                reconciliation_retirable
                if reconciliation is not None
                else RETIRABLE_TICKET_STATUSES
            )
            if TicketStatus(ticket["status"]) not in allowed_statuses:
                raise ConflictError(
                    "Ticket cannot be cancelled or superseded from its current lifecycle state"
                )
            self._require_no_active_work(
                connection, ticket_id, allow_frozen=reconciliation is not None
            )
            replacement = self._retirement_replacement(
                connection, ticket_id, project_id, replacement_ticket_id
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    entry_type,
                    reason,
                    (
                        reconciliation["observation_ref"]
                        if reconciliation is not None
                        else f"ticket:{replacement_ticket_id}" if replacement_ticket_id else None
                    ),
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            # Dependents of retired work stay blocked until a Planner rewires them;
            # naming them here keeps that consequence visible instead of silent.
            dependents = [
                str(row["ticket_id"])
                for row in connection.execute(
                    """
                    SELECT ticket_id FROM ticket_dependencies
                    WHERE depends_on_ticket_id = ? ORDER BY ticket_id
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "replacement_ticket_id": replacement_ticket_id,
                    "closed_offers": closed_offers,
                    "dependent_ticket_ids": dependents,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "replacement": replacement,
                "closed_offers": closed_offers,
                "dependent_ticket_ids": dependents,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str = "base_drift",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Requeue a stale QA-ready candidate whose base commit has drifted from main."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(reason, "reason", 2000)

        if idempotency_key:
            payload = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
            }
            return self.run_idempotent(
                actor,
                operation="candidate:requeue",
                idempotency_key=idempotency_key,
                payload=payload,
                run=lambda: self._requeue_candidate(actor, ticket_id, expected_version, reason),
            )
        return self._requeue_candidate(actor, ticket_id, expected_version, reason)

    def _requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError("Project repository is required before requeuing a candidate")
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if candidate is None:
                raise ConflictError("Ticket has no frozen candidate to requeue")

        repository_path = repository["repository_path"]
        default_branch = project["default_branch"]
        mainline = f"refs/heads/{default_branch}"
        base = candidate["base_commit"]
        base_is_current_main = self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=base,
        ) and self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=base,
            descendant_commit=mainline,
        )
        if base_is_current_main:
            raise ConflictError(
                "Candidate base commit has not drifted from current main; candidate is not stale"
            )

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )

            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError(
                    "A ticket with an active lease cannot have its candidate requeued"
                )
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))

            for table in (
                "worktree_provision_jobs",
                "candidate_submission_jobs",
                "qa_test_jobs",
                "merge_jobs",
            ):
                running = connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (ticket_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        f"A ticket with a running job ({table}) cannot have its candidate requeued"
                    )

            latest_candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if latest_candidate is None or latest_candidate["candidate_id"] != candidate["candidate_id"]:
                raise ConflictError("Candidate changed during requeue")

            timestamp = _iso()
            next_version = ticket["version"] + 1

            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.REWORK.value, reason, next_version, timestamp, ticket_id),
            )

            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'candidate_requeued', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    f"candidate:{candidate['candidate_id']}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )

            self._event(
                connection,
                actor,
                project_id,
                "candidate.requeued",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "candidate_id": candidate["candidate_id"],
                    "candidate_commit": candidate["commit_sha"],
                    "base_commit": candidate["base_commit"],
                    "closed_offers": closed_offers,
                },
            )

            rework_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, rework_ticket)

            return {
                "ticket": rework_ticket,
                "candidate": candidate,
                "closed_offers": closed_offers,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def recover_candidate_for_qa(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        evidence_reference: str,
        readiness_reference: str | None = None,
    ) -> dict[str, Any]:
        """Requeue a frozen candidate for independent QA after an infra-only rejection.

        This never marks a candidate verified, never re-runs Developer submission, and
        never edits or duplicates the immutable candidate: it only proves, from durable
        evidence already on file, that a candidate's prior QA rejection was an
        infrastructure artifact mismatch rather than a logic failure, then routes the
        exact same candidate back to a fresh, independent QA claim.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(evidence_reference, "evidence_reference", 200)
        if readiness_reference is not None:
            self._bounded_text(readiness_reference, "readiness_reference", 200)
        with self.store.connection(immediate=True) as connection:
            self._expire_lease(connection, ticket_id)
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {TicketStatus.BLOCKED, TicketStatus.REWORK}:
                raise ConflictError("Only a BLOCKED or REWORK ticket can be recovered for QA")
            if (ticket.get("blocked_reason") or "").startswith("qa.infrastructure/"):
                readiness = _row(connection.execute(
                    "SELECT * FROM context_entries WHERE entry_id = ? AND ticket_id = ? AND superseded_by IS NULL",
                    (readiness_reference, ticket_id)).fetchone())
                if (readiness is None or readiness["actor_role"] not in {"planner", "admin"}
                        or readiness["entry_type"] not in {"planner_decision", "planning_decision"}
                        or not readiness["source_ref"] or readiness["created_at"] < ticket["updated_at"]):
                    raise ValidationError("Infrastructure recovery requires a current Planner readiness entry with an evidence source")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Recovery requires no active lease on the ticket")
            candidate_rows = connection.execute(
                "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 2",
                (ticket_id,),
            ).fetchall()
            # A policy refresh may submit the same SHA again with a different
            # frozen profile. Match QA's current submission, never an older row
            # selected by SHA or by the caller's preferred evidence reference.
            if not candidate_rows or (len(candidate_rows) > 1
                    and candidate_rows[0]["submitted_at"] == candidate_rows[1]["submitted_at"]):
                raise ValidationError(
                    "Recovery requires an unambiguous latest frozen candidate on this ticket"
                )
            candidate = _row(candidate_rows[0])
            if candidate is None or candidate["commit_sha"] != ticket["candidate_commit"]:
                raise ConflictError(
                    "Candidate identity no longer matches the frozen ticket artifact"
                )
            active_job = connection.execute(
                """
                SELECT 1 FROM qa_test_jobs
                WHERE candidate_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (candidate["candidate_id"],),
            ).fetchone()
            if active_job is not None:
                raise ConflictError("Recovery requires no active project test run")
            provenance = self._verify_infrastructure_mismatch_provenance(
                connection, candidate["candidate_id"], evidence_reference
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.QA_READY.value, next_version, timestamp, ticket_id),
            )
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id(connection, "ctx"),
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    (
                        f"Recovered candidate {candidate['candidate_id']} "
                        f"({candidate['commit_sha']}) for independent QA. The prior rejection "
                        f"on this candidate was an infrastructure artifact mismatch, not a "
                        f"candidate logic failure: {provenance}. Recovered from ticket version "
                        f"{expected_version}; the immutable candidate was not edited, "
                        "duplicated, or resubmitted."
                        + (f" Readiness evidence: {readiness_reference}." if readiness_reference else "")
                    ),
                    evidence_reference,
                    candidate["commit_sha"],
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "candidate.qa_recovery",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate["candidate_id"], "evidence_reference": evidence_reference},
            )
            recovered_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, recovered_ticket)
            return {
                "ticket": recovered_ticket,
                "candidate": self._candidate_with_changed_paths(connection, candidate),
            }

    def _verify_infrastructure_mismatch_provenance(
        self,
        connection: Connection,
        candidate_id: str,
        evidence_reference: str,
    ) -> str:
        """Confirm cited evidence shows an infra-only gap, never a logic failure.

        A logical QA failure recorded anywhere against this candidate blocks recovery
        outright, regardless of which piece of evidence the Planner cites -- recovery
        must never become a generic bypass for a real QA rejection.
        """
        logical_failure = connection.execute(
            "SELECT 1 FROM qa_findings WHERE candidate_id = ? AND status = 'failed' LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if logical_failure is not None:
            raise ValidationError(
                "Candidate has a logical QA failure on record and cannot be recovered"
            )
        if evidence_reference.startswith("testrun_"):
            job = _row(
                connection.execute(
                    "SELECT * FROM qa_test_jobs WHERE test_run_id = ?", (evidence_reference,)
                ).fetchone()
            )
            if job is None or job["candidate_id"] != candidate_id:
                raise ValidationError(
                    "Evidence reference does not belong to this candidate's project test runs"
                )
            recorded_infrastructure = _audit_event_has_field(
                connection, job["ticket_id"], "qa.infrastructure_blocked",
                "test_run_id", evidence_reference,
            )
            if not recorded_infrastructure and (job["status"] != "FAILED" or "candidate_artifact_mismatch" not in (
                job["error_message"] or ""
            )):
                raise ValidationError(
                    "Evidence reference does not show a candidate_artifact_mismatch"
                )
            return f"qa_test_jobs:{evidence_reference}"
        if evidence_reference.startswith("finding_"):
            finding = _row(
                connection.execute(
                    "SELECT * FROM qa_findings WHERE finding_id = ?", (evidence_reference,)
                ).fetchone()
            )
            if finding is None or finding["candidate_id"] != candidate_id:
                raise ValidationError(
                    "Evidence reference does not belong to this candidate's QA findings"
                )
            if finding["status"] != "insufficient_evidence":
                raise ValidationError(
                    "Evidence reference does not show an insufficient-evidence gap"
                )
            return f"qa_findings:{evidence_reference}"
        raise ValidationError(
            "evidence_reference must be a qa_test_jobs test_run_id or a qa_findings finding_id"
        )

    def _require_no_active_work(
        self, connection: Connection, ticket_id: str, *, allow_frozen: bool = False
    ) -> None:
        """Refuse retirement while any agent, worker or frozen artifact still owns this."""
        active_lease = connection.execute(
            "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
            (ticket_id, _iso()),
        ).fetchone()
        if active_lease is not None:
            raise ConflictError("A ticket with an active lease cannot be cancelled or superseded")
        for table in (
            "worktree_provision_jobs",
            "candidate_submission_jobs",
            "qa_test_jobs",
            "merge_jobs",
        ):
            running = connection.execute(
                f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                (ticket_id,),
            ).fetchone()
            if running is not None:
                raise ConflictError("A ticket with a running job cannot be cancelled or superseded")
        candidate = connection.execute(
            "SELECT 1 FROM candidates WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if candidate is not None and not allow_frozen:
            raise ConflictError(
                "A ticket with a frozen candidate cannot be cancelled or superseded"
            )

    def _require_pending_reconciliation(
        self, connection: Connection, project_id: str, reconciliation_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT * FROM repository_reconciliations
            WHERE reconciliation_id = ? AND project_id = ? AND status = 'PENDING'
            """,
            (reconciliation_id, project_id),
        ).fetchone()
        if row is None:
            raise ConflictError(
                "The repository reconciliation is not pending for this project",
                {"reason": "repository_reconciliation_not_pending"},
            )
        return self._repository_reconciliation(row)

    def _retirement_replacement(
        self,
        connection: Connection,
        ticket_id: str,
        project_id: str,
        replacement_ticket_id: str | None,
    ) -> dict[str, Any] | None:
        """Resolve the replacement reference that makes a supersede auditable."""
        if replacement_ticket_id is None:
            return None
        if replacement_ticket_id == ticket_id:
            raise ValidationError("A ticket cannot supersede itself")
        replacement = _row(
            connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (replacement_ticket_id,)
            ).fetchone()
        )
        if replacement is None:
            raise NotFoundError("Replacement ticket not found")
        if self._project_for_ticket(connection, replacement_ticket_id) != project_id:
            raise ValidationError("Replacement ticket must belong to the same project")
        return replacement

    def _close_ticket_dispatch(
        self, connection: Connection, ticket_id: str, timestamp: str
    ) -> int:
        """Close queue, offer and outbox rows together so no live hand-off survives."""
        queue_ids = [
            str(row["queue_id"])
            for row in connection.execute(
                "SELECT queue_id FROM work_queue WHERE ticket_id = ? AND status IN ('pending', 'offered')",
                (ticket_id,),
            ).fetchall()
        ]
        if not queue_ids:
            return 0
        placeholders = ", ".join("?" for _ in queue_ids)
        closed_offers = connection.execute(
            f"""
            UPDATE work_offers SET status = 'cancelled', closed_at = ?
            WHERE queue_id IN ({placeholders}) AND status = 'offered'
            """,
            (timestamp, *queue_ids),
        ).rowcount
        connection.execute(
            f"""
            UPDATE dispatch_outbox SET status = 'cancelled'
            WHERE queue_id IN ({placeholders}) AND status = 'pending'
            """,
            queue_ids,
        )
        connection.execute(
            f"""
            UPDATE work_queue SET status = 'cancelled', updated_at = ?
            WHERE queue_id IN ({placeholders})
            """,
            (timestamp, *queue_ids),
        )
        return max(closed_offers, 0)

    def register_requirement(
        self,
        actor: Actor,
        project_id: str,
        requirement_id: str,
        statement: str,
        source_ref: str,
    ) -> dict[str, Any]:
        """Record something the specification asks for, so coverage can be computed.

        This records what was asked. It does not record what must be true for a
        ticket to be done: that stays on the ticket, because QA verifies a
        ticket's own criteria and a second place to author them would drift.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(requirement_id, "requirement_id", 100)
        self._bounded_text(statement, "statement", 2000)
        self._bounded_text(source_ref, "source_ref", 1000)
        with self.store.connection(immediate=True) as connection:
            self._one(connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,))
            existing = _row(
                connection.execute(
                    "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
                ).fetchone()
            )
            if existing is not None:
                if existing["project_id"] != project_id:
                    raise ConflictError("That requirement id belongs to another project")
                return {**existing, "registered": False}
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO requirements
                    (requirement_id, project_id, statement, source_ref, registered_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (requirement_id, project_id, statement, source_ref, actor.agent_id, timestamp),
            )
            self._event(
                connection, actor, project_id, "requirement.registered", "project",
                project_id, 0, {"requirement_id": requirement_id, "source_ref": source_ref},
            )
            return {
                **self._one(
                    connection,
                    "SELECT * FROM requirements WHERE requirement_id = ?",
                    (requirement_id,),
                ),
                "registered": True,
            }

    def link_requirement(
        self,
        actor: Actor,
        requirement_id: str,
        *,
        ticket_id: str | None = None,
        feature_id: str | None = None,
    ) -> dict[str, Any]:
        """Point a piece of work at the requirement it serves."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if bool(ticket_id) == bool(feature_id):
            raise ValidationError("Provide exactly one of ticket_id or feature_id")
        with self.store.connection(immediate=True) as connection:
            requirement = _row(
                connection.execute(
                    "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
                ).fetchone()
            )
            if requirement is None:
                # Refused rather than created implicitly: a typo would otherwise
                # invent a requirement nobody asked for and report it as covered.
                raise ValidationError(
                    f"No such requirement: {requirement_id}",
                    {"reason": "requirement_unknown"},
                )
            if ticket_id:
                ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
                subject_type, subject_id = "ticket", ticket["ticket_id"]
            else:
                assert feature_id is not None
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                subject_type, subject_id = "feature", feature_id
            if project_id != requirement["project_id"]:
                raise ConflictError("Work and requirement belong to different projects")
            connection.execute(
                """
                INSERT INTO requirement_links
                    (requirement_id, subject_type, subject_id, linked_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(requirement_id, subject_type, subject_id) DO NOTHING
                """,
                (requirement_id, subject_type, subject_id, actor.agent_id, _iso()),
            )
            return {
                "requirement_id": requirement_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
            }

    def requirement_coverage(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """What nothing is pointing at, what is in flight, and what is finished.

        Read instead of re-reading the specification: this answers "what have I
        missed" from the board's own state, which is cheaper than recalling the
        source and, unlike memory, cannot quietly disagree with it.
        """
        require_project(actor, project_id)
        with self.store.connection() as connection:
            requirements = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM requirements WHERE project_id = ? ORDER BY requirement_id",
                    (project_id,),
                ).fetchall()
            ]
            uncovered, in_flight, satisfied = [], [], []
            for requirement in requirements:
                tickets = [
                    _row(row)
                    for row in connection.execute(
                        """
                        SELECT t.ticket_id, t.status FROM requirement_links l
                        JOIN tickets t ON t.ticket_id = l.subject_id
                        WHERE l.requirement_id = ? AND l.subject_type = 'ticket'
                        ORDER BY t.ticket_id
                        """,
                        (requirement["requirement_id"],),
                    ).fetchall()
                ]
                # Retired work does not cover anything, so a cancelled ticket
                # returns its requirement to uncovered rather than hiding it.
                live = [
                    row
                    for row in tickets
                    if TicketStatus(row["status"]) not in CLOSED_TICKET_STATUSES
                ]
                entry = {
                    "requirement_id": requirement["requirement_id"],
                    "statement": requirement["statement"],
                    "source_ref": requirement["source_ref"],
                    "tickets": [row["ticket_id"] for row in live],
                }
                if not live:
                    uncovered.append(entry)
                elif all(row["status"] == TicketStatus.DONE.value for row in live):
                    satisfied.append(entry)
                else:
                    in_flight.append(entry)
        return {
            "project_id": project_id,
            "uncovered": uncovered,
            "in_flight": in_flight,
            "satisfied": satisfied,
            "counts": {
                "total": len(requirements),
                "uncovered": len(uncovered),
                "in_flight": len(in_flight),
                "satisfied": len(satisfied),
            },
        }

    def append_planner_context(
        self,
        actor: Actor,
        *,
        entry_type: str,
        content: str,
        ticket_id: str | None = None,
        feature_id: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Record an auditable planning decision against exactly one context scope."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if bool(ticket_id) == bool(feature_id):
            raise ValidationError("Provide exactly one of ticket_id or feature_id")
        self._bounded_text(entry_type, "entry_type", 64)
        self._bounded_text(content, "content", 8000)
        if source_ref is not None:
            self._bounded_text(source_ref, "source_ref", 1000)
        recorded_type, content = self._vocabulary_entry(entry_type, content)
        with self.store.connection(immediate=True) as connection:
            if ticket_id:
                ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
                resolved_feature_id = ticket["feature_id"]
                aggregate_type, aggregate_id, version = "ticket", ticket_id, ticket["version"]
            else:
                assert feature_id is not None
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
                resolved_feature_id = feature_id
                aggregate_type, aggregate_id, version = "feature", feature_id, feature["version"]
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE entry_type = ? AND content = ?
                      AND ((ticket_id IS NULL AND ? IS NULL) OR ticket_id = ?)
                      AND ((feature_id IS NULL AND ? IS NULL) OR feature_id = ?)
                      AND superseded_by IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        recorded_type, content,
                        ticket_id, ticket_id,
                        resolved_feature_id, resolved_feature_id,
                    ),
                ).fetchone()
            )
            if existing is not None:
                # Saying the same thing twice adds a row and no information. The
                # flag lets a caller tell "recorded" from "already said", which a
                # silent no-op would hide.
                return {**existing, "deduplicated": True}
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, project_id, resolved_feature_id, ticket_id, recorded_type, content,
                    source_ref, actor.agent_id, actor.role.value, _iso(),
                ),
            )
            self._event(
                connection, actor, project_id, "context.planning_appended", aggregate_type,
                aggregate_id, version, {"entry_id": entry_id, "entry_type": recorded_type},
            )
            entry = self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )
            return {**entry, "deduplicated": False}

    @staticmethod
    def _vocabulary_entry(entry_type: str, content: str) -> tuple[str, str]:
        """Keep the type vocabulary closed, without losing what the caller meant.

        An unrecognised label is recorded under the general category rather than
        silently founding a new type, and is kept as a visible qualifier on the
        content so nothing about the caller's intent is thrown away.
        """
        canonical = CONTEXT_ENTRY_ALIASES.get(entry_type, entry_type)
        if canonical in CONTEXT_ENTRY_TYPES:
            return canonical, content
        return GENERAL_CONTEXT_ENTRY_TYPE, f"[{entry_type}] {content}"
