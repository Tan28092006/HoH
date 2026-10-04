"""Repository sync and the reconciliation barrier that follows every observed commit."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    NotFoundError,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _row,
    json,
    require_project,
    require_role,
    Connection,
)


class _RepositoryObservationMoved(ConflictError):
    """The stored repository observation changed while a sync inspected Git."""


class RepositoryReconciliationMixin:
    """Repository sync and the reconciliation barrier that follows every observed commit."""

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
