"""Repository sync and the reconciliation barrier that follows every observed commit."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    PROJECT_SCOPE,
    TICKETS_SCOPE,
    ExecutionGate,
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

        def is_default_branch(branch: str) -> bool:
            # Only the default branch's remote head carries planning truth. A
            # teammate's feature branch is not delivered behavior yet, so its
            # pushes are recorded in the observed refs but never close work.
            return branch == upstream_ref or branch.partition("/")[2] == project["default_branch"]

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
            if not is_default_branch(branch):
                continue
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
            changed = changed or any(
                is_default_branch(update["branch"]) for update in branch_updates
            )
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
                # One pending observation per project: a later sync (Planner's
                # or the scheduler sweep's) appends to it instead of opening a
                # duplicate Planner must resolve in turn (HOH-280). Results of
                # HoH's own work (QA rejection, regression) are not observations
                # and keep their own records.
                pending_observation = connection.execute(
                    """
                    SELECT * FROM repository_reconciliations
                    WHERE project_id = ? AND status = 'PENDING'
                      AND observation_ref LIKE 'ctx:entry:%'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                reconciliation_id = (
                    str(pending_observation["reconciliation_id"])
                    if pending_observation is not None else _id(connection, "recon")
                )
                manifest["context_ref"] = f"ctx:entry:{entry_id}"
                manifest["reconciliation_id"] = reconciliation_id
                manifest["reconciliation_extended"] = pending_observation is not None
                all_open_ticket_versions = open_ticket_versions = {
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
                frozen_ticket_ids = None
                if not baseline_reconciliation:
                    frozen_ticket_ids = self._outside_commit_frozen_tickets(
                        connection,
                        list(open_ticket_versions),
                        manifest,
                        current.get("observed_upstream_commit") != manifest["upstream_commit"],
                    )
                existing = None
                if pending_observation is not None:
                    # Extending the one pending observation never narrows it:
                    # a project-wide record stays project-wide, and a
                    # ticket-scoped one grows to the union of reached tickets.
                    existing = self._repository_reconciliation(pending_observation)
                    existing_frozen = existing.get("frozen_ticket_ids")
                    if isinstance(existing_frozen, str):
                        existing_frozen = json.loads(existing_frozen)
                    if (
                        frozen_ticket_ids is None
                        or existing["scope"] != TICKETS_SCOPE
                        or existing_frozen is None
                    ):
                        frozen_ticket_ids = None
                    else:
                        frozen_ticket_ids = sorted(
                            set(frozen_ticket_ids) | set(existing_frozen)
                        )
                scope = PROJECT_SCOPE
                if frozen_ticket_ids is not None:
                    scope = TICKETS_SCOPE
                    open_ticket_versions = {
                        ticket_id: all_open_ticket_versions[ticket_id]
                        for ticket_id in frozen_ticket_ids
                        if ticket_id in all_open_ticket_versions
                    }
                manifest["reconciliation_scope"] = scope
                manifest["frozen_ticket_ids"] = frozen_ticket_ids
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
                if existing is not None:
                    merged_commits = self._union_observed_commits(
                        existing["observed_commits"], observed_commits
                    )
                    # Never shrink or re-baseline what was captured: a ticket
                    # keeps the version first observed, new reached tickets join.
                    merged_versions = {**open_ticket_versions, **existing["ticket_versions"]}
                    connection.execute(
                        """
                        UPDATE repository_reconciliations
                        SET observed_commits = ?, ticket_versions = ?, scope = ?,
                            frozen_ticket_ids = ?
                        WHERE reconciliation_id = ?
                        """,
                        (
                            json.dumps(merged_commits, sort_keys=True),
                            json.dumps(merged_versions, sort_keys=True),
                            scope,
                            None if frozen_ticket_ids is None else json.dumps(frozen_ticket_ids),
                            reconciliation_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO repository_reconciliations
                            (reconciliation_id, project_id, observation_ref, observed_commits,
                             ticket_versions, status, created_at, scope, frozen_ticket_ids)
                        VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                        """,
                        (
                            reconciliation_id,
                            project_id,
                            manifest["context_ref"],
                            json.dumps(observed_commits, sort_keys=True),
                            json.dumps(open_ticket_versions, sort_keys=True),
                            timestamp,
                            scope,
                            None if frozen_ticket_ids is None else json.dumps(frozen_ticket_ids),
                        ),
                    )
                    if frozen_ticket_ids == []:
                        # Nothing on the board can be reached by the outside
                        # paths: the observation is recorded, no ticket is kept
                        # or frozen.
                        connection.execute(
                            """
                            UPDATE repository_reconciliations
                            SET status = 'RESOLVED', resolved_by = 'system:no-ticket-reached',
                                resolved_at = ?, decision_manifest = ?
                            WHERE reconciliation_id = ?
                            """,
                            (
                                timestamp,
                                json.dumps({
                                    "summary": "Observed commits reach no open ticket's scope "
                                               "or dependencies; nothing to reclassify.",
                                    "ticket_decisions": [],
                                    "new_ticket_ids": [],
                                }, sort_keys=True),
                                reconciliation_id,
                            ),
                        )
                self._withdraw_offers(connection, project_id, timestamp, frozen_ticket_ids)
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
                        "extended": manifest["reconciliation_extended"],
                        "scope": scope,
                        **(
                            {"frozen_ticket_ids": frozen_ticket_ids}
                            if frozen_ticket_ids is not None else {}
                        ),
                    },
                )
            pending = self._repository_reconciliation_state(connection, project_id)
            manifest["reconciliation_required"] = bool(pending["pending"])
            manifest["pending_reconciliations"] = pending["pending"]
        return manifest

    @staticmethod
    def _union_observed_commits(
        existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Existing observed commits, then new ones; one row per SHA."""
        by_sha: dict[str, dict[str, Any]] = {}
        for item in [*existing, *incoming]:
            sha = item["commit"]
            if sha not in by_sha:
                by_sha[sha] = {
                    **item,
                    "branches": list(item.get("branches") or []),
                    "relations": list(item.get("relations") or []),
                }
                continue
            for field in ("branches", "relations"):
                for value in item.get(field) or []:
                    if value not in by_sha[sha][field]:
                        by_sha[sha][field].append(value)
        return list(by_sha.values())

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
        frozen_ticket_ids: list[str] | None = None,
        rework_ticket_id: str | None = None,
        resolved_by: str | None = None,
    ) -> str:
        """Open the next Planner turn from a candidate or integration result.

        Repository polling covers teammate branches. This path closes the race
        after an internal QA or merge result: the same transaction that records
        the result also closes execution until Planner has assessed it.

        With ``frozen_ticket_ids`` the result left ``main`` unchanged, so only
        those tickets are captured and closed (scope ``tickets``); every other
        ticket keeps its offers and claims. Without it the whole project closes.

        With ``rework_ticket_id`` as well (wave 7A) the server resolves the
        ticket-scoped reconciliation in the same transaction: that ticket, already
        REWORK or BLOCKED, gets ``rework`` and its dependents ``keep``, since the
        dependency edges already hold them. It stays pending for Planner when an
        older pending reconciliation captured one of the same tickets.

        With ``merged_ticket_id`` and ``changed_paths`` (HoH's own gated merge)
        nothing closes: the record is resolved by the server at once and the
        tickets the merged paths can reach get a non-blocking
        ``planning_base_stale`` flag (wave 7C).
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
        scope = PROJECT_SCOPE
        if frozen_ticket_ids is not None:
            scope = TICKETS_SCOPE
            frozen_ticket_ids = sorted(set(frozen_ticket_ids) & set(ticket_versions))
            ticket_versions = {
                ticket_id: ticket_versions[ticket_id] for ticket_id in frozen_ticket_ids
            }
        reconciliation_id = _id(connection, "recon")
        connection.execute(
            """
            INSERT INTO repository_reconciliations
                (reconciliation_id, project_id, observation_ref, observed_commits,
                 ticket_versions, status, created_at, scope, frozen_ticket_ids)
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (
                reconciliation_id,
                project_id,
                observation_ref,
                json.dumps(observed_commits, sort_keys=True),
                json.dumps(ticket_versions, sort_keys=True),
                timestamp,
                scope,
                None if frozen_ticket_ids is None else json.dumps(frozen_ticket_ids),
            ),
        )
        auto_reason = None
        stale_ticket_ids: list[str] = []
        if merged_ticket_id is not None and changed_paths is not None:
            # Wave 7C: HoH's own gated merge never waits for Planner. main is
            # still correct (fast-forward of a QA-verified tree); what may be
            # stale is the plan of tickets the merged paths can reach. Those
            # are flagged, not frozen: their next QA must include an
            # intent_check, and Planner is told asynchronously.
            partition = self._shadow_merge_partition(
                connection, list(ticket_versions), merged_ticket_id, changed_paths
            )
            stale_ticket_ids = list(partition["frozen_ticket_ids"])
            resolver = "system:merge-advisory" if stale_ticket_ids else "system:scope-disjoint"
            auto_reason = (
                f"Merged {merged_ticket_id} changed {len(changed_paths)} path(s); "
                + (
                    f"{len(stale_ticket_ids)} open ticket(s) it can reach keep running with "
                    "planning_base_stale and an intent_check at their next QA."
                    if stale_ticket_ids else
                    "no open ticket's scope or dependencies reach them."
                )
                + " Recorded as keep by the server; Planner may still stop or retire any ticket."
            )
            manifest = {
                "summary": auto_reason,
                "ticket_decisions": [
                    {"ticket_id": ticket_id, "disposition": "keep", "reason": auto_reason,
                     "decided_by": resolver,
                     **({"planning_base_stale": True} if ticket_id in stale_ticket_ids else {})}
                    for ticket_id in sorted(ticket_versions)
                ],
                "new_ticket_ids": [],
                "planning_base_stale": partition,
            }
            connection.execute(
                """
                UPDATE repository_reconciliations
                SET status = 'RESOLVED', resolved_by = ?,
                    resolved_at = ?, decision_manifest = ?
                WHERE reconciliation_id = ?
                """,
                (resolver, timestamp, json.dumps(manifest, sort_keys=True), reconciliation_id),
            )
            self._flag_planning_base_stale(
                connection,
                stale_ticket_ids,
                {
                    "commit": commit,
                    "merged_ticket_id": merged_ticket_id,
                    "reconciliation_id": reconciliation_id,
                    "reason": partition["reason"],
                    "flagged_at": timestamp,
                    "intent_check": "required",
                },
            )
        elif (
            rework_ticket_id is not None
            and frozen_ticket_ids is not None
            and rework_ticket_id in ticket_versions
            and ticket_versions[rework_ticket_id]["status"] in {
                TicketStatus.REWORK.value, TicketStatus.BLOCKED.value
            }
            and self._older_overlapping_reconciliation(
                connection, self._repository_reconciliation(connection.execute(
                    "SELECT * FROM repository_reconciliations WHERE reconciliation_id = ?",
                    (reconciliation_id,),
                ).fetchone())
            ) is None
        ):
            decided_by = resolved_by or "system:qa-rejection"
            auto_reason = (
                f"{subject}: {rework_ticket_id} is back for rework and its dependents wait on "
                "their dependency edge; resolved by the server."
            )
            manifest = {
                "summary": auto_reason,
                "ticket_decisions": [
                    {"ticket_id": ticket_id,
                     "disposition": "rework" if ticket_id == rework_ticket_id else "keep",
                     "reason": auto_reason, "decided_by": decided_by}
                    for ticket_id in sorted(ticket_versions)
                ],
                "new_ticket_ids": [],
            }
            connection.execute(
                """
                UPDATE repository_reconciliations
                SET status = 'RESOLVED', resolved_by = ?, resolved_at = ?, decision_manifest = ?
                WHERE reconciliation_id = ?
                """,
                (decided_by, timestamp, json.dumps(manifest, sort_keys=True), reconciliation_id),
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
            self._withdraw_offers(connection, project_id, timestamp, frozen_ticket_ids)
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
                "scope": scope,
                **(
                    {"frozen_ticket_ids": frozen_ticket_ids}
                    if frozen_ticket_ids is not None else {}
                ),
                **(
                    {"planning_base_stale_ticket_ids": stale_ticket_ids}
                    if merged_ticket_id is not None else {}
                ),
            },
        )
        if stale_ticket_ids:
            # The Planner's advisory: nothing waits on it. Planner reads it in
            # its own time and may stop, revise or retire any listed ticket.
            self._event(
                connection,
                actor,
                project_id,
                "project.planning_base_advanced",
                "project",
                project_id,
                next_version,
                {
                    "reconciliation_id": reconciliation_id,
                    "commit": commit,
                    "merged_ticket_id": merged_ticket_id,
                    "planning_base_stale_ticket_ids": stale_ticket_ids,
                    "blocking": False,
                },
            )
        return reconciliation_id

    @staticmethod
    def _flag_planning_base_stale(
        connection: Connection, ticket_ids: list[str], flag: dict[str, Any]
    ) -> None:
        """Mark open tickets whose plan predates a merge that can reach them.

        The flag never blocks a claim, an offer or a submission. QA reads it
        from the packet and must add an ``intent_check`` finding; a passed
        verdict that includes one clears it. It deliberately does not bump the
        ticket version, so a live lease's expected_version stays valid.
        """
        if not ticket_ids:
            return
        placeholders = ",".join("?" for _ in ticket_ids)
        connection.execute(
            f"""
            UPDATE tickets SET planning_base_stale = ?
            WHERE ticket_id IN ({placeholders})
              AND status NOT IN ('DONE', 'CANCELLED', 'SUPERSEDED')
            """,
            (json.dumps(flag, sort_keys=True), *ticket_ids),
        )

    @staticmethod
    def _withdraw_offers(
        connection: Connection,
        project_id: str,
        timestamp: str,
        ticket_ids: list[str] | None = None,
    ) -> None:
        """Cancel live offers so no agent claims from a plan Planner must review.

        A project-wide reconciliation withdraws every offer in the project; a
        ticket-scoped one only the frozen tickets' offers.
        """
        if ticket_ids is not None and not ticket_ids:
            return
        ticket_filter, ticket_values = "", ()
        if ticket_ids is not None:
            ticket_filter = f" AND ticket_id IN ({','.join('?' for _ in ticket_ids)})"
            ticket_values = tuple(ticket_ids)
        connection.execute(
            f"""
            UPDATE work_offers SET status = 'cancelled', closed_at = ?
            WHERE status = 'offered' AND queue_id IN (
                SELECT queue_id FROM work_queue
                WHERE project_id = ? AND status IN ('pending', 'offered'){ticket_filter}
            )
            """,
            (timestamp, project_id, *ticket_values),
        )
        connection.execute(
            f"""
            UPDATE work_queue SET status = 'pending', updated_at = ?
            WHERE project_id = ? AND status = 'offered'{ticket_filter}
            """,
            (timestamp, project_id, *ticket_values),
        )

    @staticmethod
    def _frozen_ticket_closure(connection: Connection, ticket_id: str) -> list[str]:
        """The ticket and every open ticket that depends on it, transitively.

        A result that did not move ``main`` can invalidate only the plan of the
        ticket it judged and of work built on that ticket. The walk follows
        dependency edges through tickets of any status, so a DONE link in the
        middle of a chain does not hide what lies beyond it; only open tickets
        are returned, because a terminal one has nothing left to freeze.
        """
        seen = {ticket_id}
        queue = [ticket_id]
        while queue:
            current = queue.pop()
            for row in connection.execute(
                "SELECT ticket_id FROM ticket_dependencies WHERE depends_on_ticket_id = ?",
                (current,),
            ).fetchall():
                dependent = str(row["ticket_id"])
                if dependent not in seen:
                    seen.add(dependent)
                    queue.append(dependent)
        placeholders = ",".join("?" for _ in seen)
        return [
            str(row["ticket_id"])
            for row in connection.execute(
                f"""
                SELECT ticket_id FROM tickets
                WHERE ticket_id IN ({placeholders})
                  AND status NOT IN ('DONE', 'CANCELLED', 'SUPERSEDED')
                ORDER BY ticket_id
                """,
                tuple(sorted(seen)),
            ).fetchall()
        ]

    _SHARED_PATH_PREFIXES = ("docs/", "spec/", "specs/")

    #: Names the rule a shadow partition was computed with, so agreement figures
    #: from a later, different rule are never pooled with these.
    SHADOW_RULE = "scope-closure-v1"

    def _shadow_merge_partition(
        self,
        connection: Connection,
        open_ticket_ids: list[str],
        merged_ticket_id: str | None,
        changed_paths: list[str],
    ) -> dict[str, Any]:
        """The tickets a scope-partitioned barrier would freeze after this merge.

        ``merged_ticket_id`` is None for a commit HoH did not merge; then no
        ticket is reached through a dependency on it.

        Conservative by construction: a ticket is frozen when its scope root
        overlaps a merged path, when it has no scope, or when it depends on the
        merged ticket directly or transitively. A commit touching docs/spec or a
        repository-root file, or one whose paths are unknown, freezes everything
        (``project_wide``), since those reach every ticket's plan.
        """
        paths = [path.replace("\\", "/").lstrip("/") for path in changed_paths]
        ordered = sorted(open_ticket_ids)
        shared = sorted(
            path for path in paths
            if "/" not in path or path.lower().startswith(self._SHARED_PATH_PREFIXES)
        )
        if not paths or shared:
            return {
                "rule": self.SHADOW_RULE,
                "project_wide": True,
                "reason": "shared_paths_changed" if shared else "changed_paths_unknown",
                "frozen_ticket_ids": ordered,
                "flowing_ticket_ids": [],
                "changed_path_count": len(paths),
            }
        dependents = (
            set(self._frozen_ticket_closure(connection, merged_ticket_id))
            if merged_ticket_id is not None else set()
        )
        frozen = []
        for ticket_id in ordered:
            row = connection.execute(
                "SELECT file_scope FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            ticket_scopes = json.loads(row["file_scope"]) if row and row["file_scope"] else []
            if (
                not ticket_scopes
                or ticket_id in dependents
                or any(
                    self._scope_roots_overlap(str(scope), path)
                    for scope in ticket_scopes
                    for path in paths
                )
            ):
                frozen.append(ticket_id)
        return {
            "rule": self.SHADOW_RULE,
            "project_wide": False,
            "reason": "scope_overlap_or_dependency",
            "frozen_ticket_ids": frozen,
            "flowing_ticket_ids": [ticket_id for ticket_id in ordered if ticket_id not in frozen],
            "changed_path_count": len(paths),
        }

    def _outside_commit_frozen_tickets(
        self,
        connection: Connection,
        open_ticket_ids: list[str],
        manifest: dict[str, Any],
        upstream_changed: bool,
    ) -> list[str] | None:
        """The tickets an observed outside commit can affect, or None for all.

        Wave 7C: a commit HoH did not merge freezes only the scope closure of
        its changed paths (scope overlap, unscoped tickets, and every open
        ticket depending on one of those, transitively). The whole project
        still closes for rewritten history, a docs/spec or repository-root
        change, an unknown or truncated diff, and any observation that did not
        move the default branch or its upstream at all.
        """
        mainline = manifest.get("mainline_update")
        if mainline is None and not upstream_changed:
            return None
        paths: list[str] = []
        if mainline is not None:
            if mainline.get("history_relation") != "fast_forward":
                return None
            changes = mainline.get("changes") or []
            if len(changes) >= 200 or (mainline.get("commits") and not changes):
                return None
            paths.extend(str(change["path"]) for change in changes)
        if upstream_changed:
            upstream_ref = manifest.get("upstream_ref")
            for update in manifest.get("branch_updates") or []:
                if (
                    update.get("branch") == upstream_ref
                    and update.get("history_relation") != "fast_forward"
                ):
                    return None
            if int(manifest.get("behind") or 0) > 0:
                changes = manifest.get("incoming_changes") or []
                if not changes or len(changes) >= 200:
                    return None
                paths.extend(str(change["path"]) for change in changes)
        if not paths:
            return []
        partition = self._shadow_merge_partition(connection, open_ticket_ids, None, paths)
        if partition["project_wide"]:
            return None
        frozen = set(partition["frozen_ticket_ids"])
        for ticket_id in list(frozen):
            frozen.update(self._frozen_ticket_closure(connection, ticket_id))
        return sorted(frozen & set(open_ticket_ids))

    @staticmethod
    def _shadow_outcome(
        partition: dict[str, Any], decisions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Did every ticket Planner changed fall inside the predicted freeze?

        A ticket Planner did not ``keep`` (revise, cancel, supersede, complete or
        rework) outside the prediction is a false negative: the partition would
        have let stale work keep running.
        """
        predicted = set(partition.get("frozen_ticket_ids") or [])
        changed = sorted(
            ticket_id for ticket_id, item in decisions.items()
            if item["disposition"] != "keep"
        )
        missed = [ticket_id for ticket_id in changed if ticket_id not in predicted]
        return {"agreed": not missed, "changed_ticket_ids": changed, "missed_ticket_ids": missed}

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
            older = self._older_overlapping_reconciliation(connection, reconciliation)
            if older is not None:
                raise ConflictError(
                    "Resolve repository observations in commit order",
                    {"reason": "older_reconciliation_pending", "reconciliation_id": older},
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
            shadow = reconciliation.get("shadow_partition")
            shadow_outcome = None
            if shadow:
                # Scored beside Planner's manifest, never in it: the prediction
                # stays as it was made and the outcome is added next to it.
                shadow_outcome = self._shadow_outcome(shadow, decisions)
                connection.execute(
                    "UPDATE repository_reconciliations SET shadow_partition = ?, shadow_agreed = ? "
                    "WHERE reconciliation_id = ?",
                    (
                        json.dumps({**shadow, "outcome": shadow_outcome}, sort_keys=True),
                        int(shadow_outcome["agreed"]),
                        reconciliation_id,
                    ),
                )
            self._rearm_work_wakeups(
                connection, project_id,
                reconciliation["frozen_ticket_ids"]
                if reconciliation.get("scope") == TICKETS_SCOPE else None,
            )
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
                {
                    "reconciliation_id": reconciliation_id,
                    "decision_ref": f"ctx:entry:{entry_id}",
                    "scope": reconciliation["scope"],
                    **(
                        {"shadow_agreed": shadow_outcome["agreed"],
                         "shadow_missed_ticket_ids": shadow_outcome["missed_ticket_ids"]}
                        if shadow_outcome is not None else {}
                    ),
                },
            )
            state = self._repository_reconciliation_state(connection, project_id)
            resolved = self._repository_reconciliation(
                connection.execute(
                    "SELECT * FROM repository_reconciliations WHERE reconciliation_id = ?",
                    (reconciliation_id,),
                ).fetchone()
            )
            return {"reconciliation": resolved, **state}

    def _older_overlapping_reconciliation(
        self, connection: Connection, reconciliation: dict[str, Any]
    ) -> str | None:
        """An older pending reconciliation this one must wait for, if any.

        Observations are resolved in commit order wherever they meet: a
        project-wide one waits for every older one and is waited for by every
        newer one; two ticket-scoped ones wait for each other only when they
        captured a common ticket, so unrelated rejections are decided apart.
        """
        scope = reconciliation.get("scope") or PROJECT_SCOPE
        captured = set(reconciliation["ticket_versions"])
        for row in connection.execute(
            """
            SELECT * FROM repository_reconciliations
            WHERE project_id = ? AND status = 'PENDING' AND created_at < ?
            ORDER BY created_at ASC
            """,
            (reconciliation["project_id"], reconciliation["created_at"]),
        ).fetchall():
            older = self._repository_reconciliation(row)
            if (
                scope == PROJECT_SCOPE
                or (older.get("scope") or PROJECT_SCOPE) == PROJECT_SCOPE
                or captured & set(older["ticket_versions"])
            ):
                return str(older["reconciliation_id"])
        return None

    @staticmethod
    def _repository_reconciliation(row: Any) -> dict[str, Any]:
        value = _row(row) or {}
        for field in ("observed_commits", "ticket_versions", "decision_manifest"):
            if isinstance(value.get(field), str):
                value[field] = json.loads(value[field])
        value["scope"] = value.get("scope") or PROJECT_SCOPE
        return value

    def _repository_reconciliation_state(
        self, connection: Connection, project_id: str, ticket_id: str | None = None
    ) -> dict[str, Any]:
        return ExecutionGate.state(connection, project_id, ticket_id)

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

    def _rearm_work_wakeups(
        self, connection: Connection, project_id: str, ticket_ids: list[str] | None = None
    ) -> None:
        # A wake-up is delivered once per queue item. A barrier cancelled the
        # offers agents were told about, and they acknowledged that work while
        # blocked. Lifting the barrier forgets those deliveries so the reissued
        # offers wake an agent again instead of leaving it idle for hours. A
        # ticket-scoped barrier withdrew only its frozen tickets' offers, so only
        # those are re-armed; other agents were never interrupted.
        if ticket_ids is not None and not ticket_ids:
            return
        ticket_filter, ticket_values = "", ()
        if ticket_ids is not None:
            ticket_filter = f" AND ticket_id IN ({','.join('?' for _ in ticket_ids)})"
            ticket_values = tuple(ticket_ids)
        connection.execute(
            f"""
            DELETE FROM agent_event_deliveries
            WHERE project_id = ? AND state <> 'pending' AND event_key IN (
                SELECT 'queue:' || queue_id FROM work_queue
                WHERE project_id = ? AND status IN ('pending', 'offered'){ticket_filter}
            )
            """,
            (project_id, project_id, *ticket_values),
        )

    def _require_repository_reconciled(
        self, connection: Connection, project_id: str, ticket_id: str | None = None
    ) -> None:
        ExecutionGate.require_open(connection, project_id, ticket_id)
