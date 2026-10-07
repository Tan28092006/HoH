"""Periodic Git reconciliation that does not wait for a Planner to be present."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    CLOSED_TICKET_STATUSES,
    MERGE_JOB_STUCK_SECONDS,
    REPOSITORY_OBSERVE_SECONDS,
    Actor,
    Any,
    ConflictError,
    DomainError,
    InterventionSeverity,
    Path,
    PlannerJobType,
    Role,
    TicketStatus,
    _audit_event_has_field,
    _iso,
    _now,
    datetime,
    require_role,
    timedelta,
)

#: Reasons the sweep gives Planner. Both open an ``integration_conflict``
#: Planner job whose summary starts with the reason.
#: The actor a landing recorded by the sweep is attributed to.
SWEEP_ACTOR_ID = "system:sweep"
CANDIDATE_ALREADY_ON_MAIN = "candidate_already_on_main"
MERGE_JOB_STUCK = "merge_job_stuck"
_CANDIDATE_ON_MAIN_EVENT = "repository.candidate_already_on_main"
_MERGE_JOB_STUCK_EVENT = "integration.merge_job_stuck"
_TERMINAL = tuple(sorted(
    {status.value for status in CLOSED_TICKET_STATUSES} | {TicketStatus.DONE.value}
))
_MAX_TICKETS_PER_SWEEP = 200
_MAX_MERGE_JOBS_PER_SWEEP = 20


def _age_seconds(value: Any) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (_now() - stamp).total_seconds()


class RepositorySweepMixin:
    """Observe Git for every configured project and hand Planner what only Git shows."""

    def repository_observation_due(self, project_id: str) -> bool:
        """True when nobody observed this project's repository in the last minute.

        The throttle is the stored ``observed_at``, so a waiting Planner and the
        scheduler's sweep share it across processes. A repository never observed
        is due: the Planner wait performs the first observation.
        """
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT observed_at FROM project_repositories WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None or not row["observed_at"]:
            return True
        return _age_seconds(row["observed_at"]) >= REPOSITORY_OBSERVE_SECONDS

    def sweep_repositories(self, actor: Actor) -> dict[str, Any]:
        """One pass of state reconciliation against Git, run by the scheduler.

        For each project whose repository a Planner has observed before and that
        is due (``REPOSITORY_OBSERVE_SECONDS``): run the repository sync in
        ``detect_only`` mode, so a commit reaching some tickets opens their
        ticket-scoped reconciliation even with no Planner chat open, while one
        that would close the whole project is only recorded for a Planner sync;
        record, as ungated landings attributed to ``system:sweep``, open tickets
        whose candidate work is already on the default branch (by ancestry or by
        patch-id); and escalate merge jobs that outlived the worker timeout and
        that recovery cannot settle. A recorded landing is never a QA approval.
        A project that was never observed is left
        alone, because its first observation opens the baseline reconciliation
        and belongs to the Planner that will resolve it. One project's failure
        never stops the others.
        """
        require_role(actor, Role.ADMIN)
        with self.store.connection() as connection:
            projects = [
                dict(row) for row in connection.execute(
                    """
                    SELECT p.project_id, p.default_branch, r.repository_path, r.observed_at
                    FROM projects p JOIN project_repositories r ON r.project_id = p.project_id
                    ORDER BY p.project_id
                    """
                ).fetchall()
            ]
        summary: dict[str, Any] = {
            "observed": [],
            "failed": [],
            "candidates_already_on_main": [],
            "stuck_merge_jobs": [],
        }
        for project in projects:
            project_id = project["project_id"]
            if not project["observed_at"]:
                continue
            if _age_seconds(project["observed_at"]) < REPOSITORY_OBSERVE_SECONDS:
                continue
            steps = (
                ("observed", lambda: [self._sweep_observation(actor, project_id)]),
                ("candidates_already_on_main",
                 lambda: self._flag_candidates_already_on_main(actor, project)),
                ("stuck_merge_jobs", lambda: self._flag_stuck_merge_jobs(actor, project_id)),
            )
            for key, step in steps:
                try:
                    summary[key].extend(step())
                except Exception as error:  # noqa: BLE001 - one project must not stop the sweep
                    summary["failed"].append({
                        "project_id": project_id,
                        "step": key,
                        "error_type": type(error).__name__,
                        "summary": str(error)[:300],
                    })
        return summary

    def _sweep_observation(self, actor: Actor, project_id: str) -> dict[str, Any]:
        # Detect only: a change that would close the whole project waits for a
        # Planner sync to open that barrier (review C4, gap G11).
        manifest = self.sync_repository(actor, project_id, detect_only=True)
        return {
            "project_id": project_id,
            "changed": bool(manifest.get("changed")),
            "detected_only": bool(manifest.get("detected_only")),
            "reconciliation_id": manifest.get("reconciliation_id"),
        }

    def _flag_candidates_already_on_main(
        self, actor: Actor, project: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Record, as ungated, open tickets whose candidate work is already on main.

        Two Git facts count as landed: the candidate commit is reachable from the
        default branch, or a default-branch commit's own diff has the same stable
        patch-id as ``git diff base candidate`` (an applied diff or a cherry-pick).
        Either is recorded through ``record_external_landing`` as an ungated
        landing attributed to ``system:sweep``: it certifies no QA verdict and no
        acceptance criterion. Work in flight (a live lease or job) is retried on a
        later sweep; a landing the service refuses for any other reason falls
        back to one Planner job, as before. A ticket with a queued or running
        merge is skipped; the merge path settles that itself.
        """
        project_id = project["project_id"]
        placeholders = ", ".join("?" for _ in _TERMINAL)
        with self.store.connection() as connection:
            rows = [
                dict(row) for row in connection.execute(
                    f"""
                    SELECT t.ticket_id, t.candidate_commit, t.version,
                           (SELECT c.base_commit FROM candidates c
                            WHERE c.ticket_id = t.ticket_id AND c.commit_sha = t.candidate_commit
                            ORDER BY c.submitted_at DESC LIMIT 1) AS candidate_base
                    FROM tickets t
                    JOIN features f ON f.feature_id = t.feature_id
                    WHERE f.project_id = ? AND t.candidate_commit IS NOT NULL
                      AND t.status NOT IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM merge_jobs m
                          WHERE m.ticket_id = t.ticket_id AND m.status IN ('QUEUED', 'RUNNING')
                      )
                    ORDER BY t.ticket_id LIMIT ?
                    """,
                    (project_id, *_TERMINAL, _MAX_TICKETS_PER_SWEEP),
                ).fetchall()
            ]
            rows = [
                row for row in rows
                if not _audit_event_has_field(
                    connection, row["ticket_id"], _CANDIDATE_ON_MAIN_EVENT,
                    "candidate_commit", row["candidate_commit"],
                )
            ]
        if not rows:
            return []
        repository_path = project["repository_path"]
        branch = project["default_branch"]
        head = self.git_worktrees._resolve_commit(Path(repository_path), f"refs/heads/{branch}")
        landed: list[tuple[dict[str, Any], str, str]] = []
        for row in rows:
            if self.git_worktrees.commit_is_on_branch(
                repository_path=repository_path, default_branch=branch,
                commit=row["candidate_commit"],
            ):
                landed.append((row, row["candidate_commit"], "ancestry"))
                continue
            base = row.get("candidate_base")
            if not base or base == head:
                continue
            equivalent = self.git_worktrees.equivalent_landing(
                repository_path=repository_path, default_branch=branch,
                base_commit=base, candidate_commit=row["candidate_commit"],
            )
            if equivalent:
                landed.append((row, equivalent, "patch_id"))
        sweep_actor = Actor(SWEEP_ACTOR_ID, Role.ADMIN, frozenset({project_id}))
        flagged: list[dict[str, Any]] = []
        for row, landed_commit, match in landed:
            ticket_id, commit = row["ticket_id"], row["candidate_commit"]
            detection = {
                "recorded_by": SWEEP_ACTOR_ID,
                "match": match,
                "candidate_commit": commit,
                "candidate_base": row.get("candidate_base"),
                "mainline_commit": head,
            }
            how = (
                "is reachable from " if match == "ancestry"
                else f"has the same patch-id as {landed_commit} on "
            )
            try:
                result = self.record_external_landing(
                    sweep_actor, ticket_id, int(row["version"]), landed_commit,
                    (
                        f"Auto-recorded by {SWEEP_ACTOR_ID}: candidate {commit} of {ticket_id} "
                        f"{how}{branch}. Ungated: this record certifies no QA verdict "
                        "and no acceptance criterion."
                    ),
                    detection=detection,
                )
            except ConflictError:
                # A live lease or job, a newer version, or a ticket that just
                # closed: the next sweep sees the settled state and tries again.
                continue
            except DomainError as error:
                planner_job = self._raise_candidate_on_main(
                    actor, project_id, branch, head, ticket_id, commit, landed_commit, match,
                    str(error),
                )
                if planner_job is not None:
                    flagged.append({
                        "project_id": project_id, "ticket_id": ticket_id,
                        "candidate_commit": commit, "landed_commit": landed_commit,
                        "match": match, "mainline_commit": head, "recorded": False,
                        "planner_job_id": planner_job["planner_job_id"],
                    })
                continue
            flagged.append({
                "project_id": project_id, "ticket_id": ticket_id,
                "candidate_commit": commit, "landed_commit": landed_commit,
                "match": match, "mainline_commit": head, "recorded": True,
                "gated": False,
                "mainline_check_id": result.get("mainline_check_id"),
            })
        return flagged

    def _raise_candidate_on_main(
        self, actor: Actor, project_id: str, branch: str, head: str, ticket_id: str,
        commit: str, landed_commit: str, match: str, refusal: str,
    ) -> dict[str, Any] | None:
        """The fallback when the service refused the automatic record: one Planner job."""
        with self.store.connection(immediate=True) as connection:
            ticket = self._ticket(connection, ticket_id)
            # Git was asked outside the lock; the ticket may have moved since.
            if ticket["status"] in _TERMINAL or ticket["candidate_commit"] != commit:
                return None
            if _audit_event_has_field(
                connection, ticket_id, _CANDIDATE_ON_MAIN_EVENT, "candidate_commit", commit
            ):
                return None
            candidate = connection.execute(
                "SELECT candidate_id FROM candidates WHERE ticket_id = ? AND commit_sha = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id, commit),
            ).fetchone()
            candidate_id = candidate["candidate_id"] if candidate else None
            self._event(
                connection, actor, project_id, _CANDIDATE_ON_MAIN_EVENT, "ticket", ticket_id,
                ticket["version"],
                {
                    "reason": CANDIDATE_ALREADY_ON_MAIN,
                    "candidate_commit": commit,
                    "landed_commit": landed_commit,
                    "match": match,
                    "candidate_id": candidate_id,
                    "mainline_commit": head,
                    "ticket_status": ticket["status"],
                },
            )
            return self._open_planner_job(
                connection, actor, project_id, ticket,
                job_type=PlannerJobType.INTEGRATION_CONFLICT,
                summary=(
                    f"{CANDIDATE_ALREADY_ON_MAIN}: candidate {candidate_id or commit} of "
                    f"{ticket_id} ({ticket['status']}) landed on {branch} as {landed_commit} "
                    f"({match}), but HoH did not merge it, and the automatic ungated record "
                    f"was refused: {refusal[:300]} If this is the delivered work, record it "
                    "with planner.record_external_landing (ungated) and classify the ticket "
                    "complete; otherwise decide a revert or rework."
                )[:4000],
                evidence_refs=[
                    *([f"candidate:{candidate_id}"] if candidate_id else []),
                    f"mainline:{head}",
                ],
                candidate_id=candidate_id,
                artifact_commit=head,
            )

    def _flag_stuck_merge_jobs(self, actor: Actor, project_id: str) -> list[dict[str, Any]]:
        """Escalate merge jobs past the worker timeout that recovery cannot settle.

        Recovery is tried first (it never takes the repository from a live
        worker). A job it still leaves RUNNING, marked for reconciliation, gets one
        high-severity Planner job carrying the job id and the Git facts, which
        ``planner.close_stuck_merge_job`` then acts on.
        """
        cutoff = _iso(_now() - timedelta(seconds=MERGE_JOB_STUCK_SECONDS))
        with self.store.connection() as connection:
            job_ids = [
                row["merge_job_id"] for row in connection.execute(
                    """
                    SELECT merge_job_id FROM merge_jobs
                    WHERE project_id = ? AND status = 'RUNNING'
                      AND COALESCE(started_at, created_at) <= ?
                    ORDER BY created_at LIMIT ?
                    """,
                    (project_id, cutoff, _MAX_MERGE_JOBS_PER_SWEEP),
                ).fetchall()
            ]
        flagged: list[dict[str, Any]] = []
        for merge_job_id in job_ids:
            with self.store.connection() as connection:
                if connection.execute(
                    "SELECT 1 FROM audit_events WHERE aggregate_id = ? AND event_type = ? LIMIT 1",
                    (merge_job_id, _MERGE_JOB_STUCK_EVENT),
                ).fetchone():
                    continue
            job = self.reconcile_merge_job(actor, merge_job_id)
            if job["status"] != "RUNNING" or not str(job.get("error_message") or "").startswith(
                "Reconciliation required"
            ):
                # Settled now, or a live operation still holds the repository.
                continue
            facts = self._stuck_merge_git_facts(job)
            with self.store.connection(immediate=True) as connection:
                job = self._merge_job(connection, merge_job_id)
                if job["status"] != "RUNNING":
                    continue
                if connection.execute(
                    "SELECT 1 FROM audit_events WHERE aggregate_id = ? AND event_type = ? LIMIT 1",
                    (merge_job_id, _MERGE_JOB_STUCK_EVENT),
                ).fetchone():
                    continue
                ticket = self._ticket(connection, job["ticket_id"])
                self._event(
                    connection, actor, project_id, _MERGE_JOB_STUCK_EVENT, "merge_job",
                    merge_job_id, job["expected_version"],
                    {"reason": MERGE_JOB_STUCK, "merge_job_id": merge_job_id,
                     "ticket_id": job["ticket_id"], **facts},
                )
                on_branch = facts.get("candidate_on_default_branch")
                git_fact = (
                    f"Git: candidate {facts.get('candidate_commit')} is "
                    + ("on" if on_branch else "not on" if on_branch is False else "unknown on")
                    + f" {facts.get('default_branch')} (head {facts.get('default_branch_head')})."
                )
                planner_job = self._open_planner_job(
                    connection, actor, project_id, ticket,
                    job_type=PlannerJobType.INTEGRATION_CONFLICT,
                    summary=(
                        f"{MERGE_JOB_STUCK}: merge job {merge_job_id} for {job['ticket_id']} has "
                        f"been RUNNING since {job.get('started_at') or job['created_at']} and "
                        f"recovery cannot decide ({str(job.get('error_message'))[:300]}). "
                        f"{git_fact} Close it with planner.close_stuck_merge_job, then record "
                        "the ungated landing if the candidate is on the branch, or retire or "
                        "requeue the ticket if it is not."
                    )[:4000],
                    evidence_refs=[
                        f"merge_job:{merge_job_id}",
                        f"candidate:{job['candidate_id']}",
                        *([f"mainline:{facts['default_branch_head']}"]
                          if facts.get("default_branch_head") else []),
                    ],
                    candidate_id=job["candidate_id"],
                    severity=InterventionSeverity.HIGH,
                    artifact_commit=facts.get("default_branch_head"),
                )
            flagged.append({
                "project_id": project_id,
                "merge_job_id": merge_job_id,
                "ticket_id": job["ticket_id"],
                "candidate_on_default_branch": on_branch,
                "planner_job_id": planner_job["planner_job_id"],
            })
        return flagged
