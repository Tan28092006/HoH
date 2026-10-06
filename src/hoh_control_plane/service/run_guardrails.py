"""Run-level guardrails: the merge-queue pause, merge budgets and the post-landing main check.

Autonomous runs land work without a human looking at every merge. Three things
stop a bad run from compounding:

* every landing (gated merge or recorded ungated landing) queues one check of
  ``main``: the project's test command, run in the supervisor's test slot on the
  new main. A red main pauses the project's merge queue, opens a high-priority
  revert ticket naming the commit, and raises a Planner job;
* a per-project budget of gated merges per hour; exceeding it holds queued
  merges (the pause clears itself once the hour's count drops);
* a per-project budget of consecutive failures (merge gate failures, red or
  unrunnable main checks); exceeding it pauses the queue until Planner resumes.

The pause is one project flag, read by ``MergeQueueGuard``; the merge queue
(``queue_merge_candidate``, ``execute_queued_merge``) and the supervisor ask it.
It never touches ``ExecutionGate``: development and QA keep flowing.
"""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    DomainError,
    InterventionSeverity,
    PlannerJobType,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _record_ticket_created,
    _row,
    json,
    require_project,
    require_role,
    timedelta,
)

DEFAULT_MAX_MERGES_PER_HOUR = 12
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
RATE_PAUSE = "merges_per_hour_exceeded"
FAILURE_PAUSE = "consecutive_failures_exceeded"
RED_MAIN_PAUSE = "mainline_check_failed"
SYSTEM_GUARDRAIL_ACTOR = "system:run-guardrails"
_OUTPUT_TAIL_CHARS = 3000
_INFRASTRUCTURE = frozenset({
    "resource_unready", "resource_exhausted", "native_termination", "timeout", "cancelled",
    "capture_failure", "output_truncated", "launch_failure", "incomplete_evidence",
    "artifact_mismatch", "test_configuration_error",
})


def _guard_actor(project_id: str) -> Actor:
    return Actor(SYSTEM_GUARDRAIL_ACTOR, Role.ADMIN, frozenset({project_id}))


class MergeQueueGuard:
    """The merge-queue pause and the merges-per-hour budget, read in one place."""

    @staticmethod
    def policy(connection: Any, project_id: str) -> dict[str, Any]:
        row = _row(connection.execute(
            "SELECT * FROM project_run_guardrails WHERE project_id = ?", (project_id,)
        ).fetchone())
        if row is None:
            return {
                "project_id": project_id,
                "max_merges_per_hour": DEFAULT_MAX_MERGES_PER_HOUR,
                "max_consecutive_failures": DEFAULT_MAX_CONSECUTIVE_FAILURES,
                "consecutive_failures": 0,
                "merge_queue_paused": 0,
                "pause_reason": None,
                "pause_detail": None,
                "paused_at": None,
                "configured_by": None,
                "updated_at": None,
                "stored": False,
            }
        return {**row, "stored": True}

    @staticmethod
    def merges_last_hour(connection: Any, project_id: str) -> int:
        cutoff = _iso(_now() - timedelta(hours=1))
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE project_id = ? "
            "AND event_type = 'integration.completed' AND occurred_at >= ?",
            (project_id, cutoff),
        ).fetchone()
        return int(row[0])

    @classmethod
    def blocked(cls, connection: Any, project_id: str) -> dict[str, Any] | None:
        """Why the merge queue may not start a merge now, or None. Read-only."""
        policy = cls.policy(connection, project_id)
        count = cls.merges_last_hour(connection, project_id)
        if policy["merge_queue_paused"] and policy["pause_reason"] != RATE_PAUSE:
            return {"reason": "merge_queue_paused", "pause_reason": policy["pause_reason"],
                    "pause_detail": policy["pause_detail"], "paused_at": policy["paused_at"]}
        if count >= int(policy["max_merges_per_hour"]):
            return {"reason": "merge_queue_paused", "pause_reason": RATE_PAUSE,
                    "merges_last_hour": count,
                    "max_merges_per_hour": policy["max_merges_per_hour"]}
        return None

    @classmethod
    def require_open(
        cls, connection: Any, project_id: str, *, include_hourly_budget: bool = False
    ) -> None:
        """Refuse while the queue is paused. The hourly budget only delays a merge
        already queued, so queueing ignores it unless asked."""
        blocked = cls.blocked(connection, project_id)
        if blocked is not None and not include_hourly_budget and blocked["pause_reason"] == RATE_PAUSE:
            blocked = None
        if blocked is not None:
            raise ConflictError(
                f"The project's merge queue is paused ({blocked['pause_reason']}); "
                "no new merge starts until it is resumed",
                blocked,
            )


class RunGuardrailsMixin:
    """Merge budgets, the merge-queue pause and post-landing checks of main."""

    # -- policy -----------------------------------------------------------

    def configure_run_guardrails(
        self,
        actor: Actor,
        project_id: str,
        max_merges_per_hour: int = DEFAULT_MAX_MERGES_PER_HOUR,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        for name, value, upper in (
            ("max_merges_per_hour", max_merges_per_hour, 120),
            ("max_consecutive_failures", max_consecutive_failures, 50),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= upper:
                raise ValidationError(f"{name} must be an integer from 1 to {upper}")
        with self.store.connection(immediate=True) as connection:
            self._one(connection, "SELECT project_id FROM projects WHERE project_id = ?", (project_id,))
            self._ensure_guardrail_row(connection, project_id)
            connection.execute(
                "UPDATE project_run_guardrails SET max_merges_per_hour = ?, "
                "max_consecutive_failures = ?, configured_by = ?, updated_at = ? "
                "WHERE project_id = ?",
                (max_merges_per_hour, max_consecutive_failures, actor.agent_id, _iso(), project_id),
            )
            self._event(
                connection, actor, project_id, "project.run_guardrails_configured", "project",
                project_id, 1,
                {"max_merges_per_hour": max_merges_per_hour,
                 "max_consecutive_failures": max_consecutive_failures},
            )
            return self._run_guardrail_state(connection, project_id)

    def get_run_guardrails(self, actor: Actor, project_id: str) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.INTEGRATOR, Role.ADMIN)
        require_project(actor, project_id)
        with self.store.connection() as connection:
            return self._run_guardrail_state(connection, project_id)

    def resume_merge_queue(self, actor: Actor, project_id: str, reason: str) -> dict[str, Any]:
        """Planner lifts a pause after deciding what the failure meant.

        Resuming resets the consecutive-failure count. It does not hide a red
        main: the revert ticket stays open, and the next landing is checked again.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            self._ensure_guardrail_row(connection, project_id)
            before = MergeQueueGuard.policy(connection, project_id)
            connection.execute(
                "UPDATE project_run_guardrails SET merge_queue_paused = 0, pause_reason = NULL, "
                "pause_detail = NULL, paused_at = NULL, consecutive_failures = 0, updated_at = ? "
                "WHERE project_id = ?",
                (_iso(), project_id),
            )
            self._event(
                connection, actor, project_id, "project.merge_queue_resumed", "project",
                project_id, 1,
                {"previous_pause_reason": before["pause_reason"], "reason": reason},
            )
            return self._run_guardrail_state(connection, project_id)

    def _run_guardrail_state(self, connection: Any, project_id: str) -> dict[str, Any]:
        policy = MergeQueueGuard.policy(connection, project_id)
        latest = _row(connection.execute(
            "SELECT check_id, mainline_commit, status, outcome, revert_ticket_id, completed_at "
            "FROM mainline_checks WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone())
        return {
            **{key: value for key, value in policy.items() if key != "stored"},
            "merge_queue_paused": bool(policy["merge_queue_paused"]),
            "merges_last_hour": MergeQueueGuard.merges_last_hour(connection, project_id),
            "blocked": MergeQueueGuard.blocked(connection, project_id),
            "latest_mainline_check": latest,
        }

    @staticmethod
    def _ensure_guardrail_row(connection: Any, project_id: str) -> None:
        connection.execute(
            "INSERT INTO project_run_guardrails (project_id, max_merges_per_hour, "
            "max_consecutive_failures, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id) DO NOTHING",
            (project_id, DEFAULT_MAX_MERGES_PER_HOUR, DEFAULT_MAX_CONSECUTIVE_FAILURES, _iso()),
        )

    # -- pause and failure accounting ------------------------------------------

    def _pause_merge_queue(
        self,
        connection: Any,
        project_id: str,
        *,
        pause_reason: str,
        detail: str,
        ticket: dict[str, Any] | None,
        evidence_refs: list[str],
        artifact_commit: str | None = None,
    ) -> None:
        """Set the project flag once and raise one high-severity Planner job."""
        self._ensure_guardrail_row(connection, project_id)
        current = MergeQueueGuard.policy(connection, project_id)
        # One pause at a time; only the self-clearing hourly pause is upgraded.
        if current["merge_queue_paused"] and (
            current["pause_reason"] == pause_reason or current["pause_reason"] != RATE_PAUSE
        ):
            return
        actor = _guard_actor(project_id)
        connection.execute(
            "UPDATE project_run_guardrails SET merge_queue_paused = 1, pause_reason = ?, "
            "pause_detail = ?, paused_at = ?, updated_at = ? WHERE project_id = ?",
            (pause_reason, detail[:2000], _iso(), _iso(), project_id),
        )
        self._event(
            connection, actor, project_id, "project.merge_queue_paused", "project", project_id, 1,
            {"pause_reason": pause_reason, "detail": detail[:500], "evidence_refs": evidence_refs},
        )
        if ticket is not None:
            self._open_planner_job(
                connection, actor, project_id, ticket,
                job_type=PlannerJobType.INTEGRATION_CONFLICT,
                summary=(
                    f"merge_queue_paused ({pause_reason}): {detail} No new merge starts in "
                    "this project until planner.resume_merge_queue; development and QA "
                    "continue."
                )[:4000],
                evidence_refs=evidence_refs,
                severity=InterventionSeverity.HIGH,
                artifact_commit=artifact_commit,
            )

    def _record_run_outcome(
        self,
        connection: Any,
        project_id: str,
        *,
        succeeded: bool,
        ticket: dict[str, Any] | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        """Count consecutive failures; past the budget, pause the merge queue."""
        self._ensure_guardrail_row(connection, project_id)
        if succeeded:
            connection.execute(
                "UPDATE project_run_guardrails SET consecutive_failures = 0, updated_at = ? "
                "WHERE project_id = ?",
                (_iso(), project_id),
            )
            return
        connection.execute(
            "UPDATE project_run_guardrails SET consecutive_failures = consecutive_failures + 1, "
            "updated_at = ? WHERE project_id = ?",
            (_iso(), project_id),
        )
        policy = MergeQueueGuard.policy(connection, project_id)
        if int(policy["consecutive_failures"]) >= int(policy["max_consecutive_failures"]):
            self._pause_merge_queue(
                connection, project_id, pause_reason=FAILURE_PAUSE,
                detail=(
                    f"{policy['consecutive_failures']} consecutive merge or main-check failures "
                    f"reached the budget of {policy['max_consecutive_failures']}."
                ),
                ticket=ticket,
                evidence_refs=[evidence_ref] if evidence_ref else [],
            )

    def merge_queue_admits(self, merge_job_id: str) -> bool:
        """Whether a queued merge may start now; applies the merges-per-hour budget.

        Exceeding the hourly budget sets a rate pause (one Planner job); once the
        hour's count drops below the budget the rate pause clears itself. Any
        other pause holds until Planner resumes the queue.
        """
        with self.store.connection(immediate=True) as connection:
            job = _row(connection.execute(
                "SELECT project_id, ticket_id FROM merge_jobs WHERE merge_job_id = ?",
                (merge_job_id,),
            ).fetchone())
            if job is None:
                return True
            project_id = job["project_id"]
            policy = MergeQueueGuard.policy(connection, project_id)
            blocked = MergeQueueGuard.blocked(connection, project_id)
            if blocked is None:
                if policy["merge_queue_paused"] and policy["pause_reason"] == RATE_PAUSE:
                    connection.execute(
                        "UPDATE project_run_guardrails SET merge_queue_paused = 0, "
                        "pause_reason = NULL, pause_detail = NULL, paused_at = NULL, "
                        "updated_at = ? WHERE project_id = ?",
                        (_iso(), project_id),
                    )
                    self._event(
                        connection, _guard_actor(project_id), project_id,
                        "project.merge_queue_resumed", "project", project_id, 1,
                        {"previous_pause_reason": RATE_PAUSE, "reason": "hourly budget recovered"},
                    )
                return True
            if blocked["pause_reason"] == RATE_PAUSE:
                self._pause_merge_queue(
                    connection, project_id, pause_reason=RATE_PAUSE,
                    detail=(
                        f"{blocked['merges_last_hour']} gated merges in the last hour reached "
                        f"the budget of {blocked['max_merges_per_hour']}; queued merges wait "
                        "and resume on their own when the hour's count drops."
                    ),
                    ticket=self._ticket(connection, job["ticket_id"]),
                    evidence_refs=[f"merge_job:{merge_job_id}"],
                )
            return False

    # -- the post-landing check of main ------------------------------------------

    def _queue_mainline_check(
        self,
        connection: Any,
        project_id: str,
        *,
        mainline_commit: str,
        trigger_ref: str,
        trigger_ticket_id: str | None,
        covered_by_gate: bool = False,
    ) -> str | None:
        """Queue one check of main after a landing; a queued one is retargeted.

        Only the newest main needs testing: a check still waiting is moved to the
        new commit instead of queueing a second suite. Skipped when the project
        has no test command. ``covered_by_gate`` records, without running
        anything, a main the merge gate already tested at exactly this commit.
        """
        if covered_by_gate:
            check_id = _id(connection, "main_check")
            timestamp = _iso()
            connection.execute(
                "INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, "
                "trigger_ticket_id, status, outcome, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, 'COMPLETED', 'covered_by_gate', ?, ?)",
                (check_id, project_id, mainline_commit, trigger_ref, trigger_ticket_id,
                 timestamp, timestamp),
            )
            return check_id
        if connection.execute(
            "SELECT 1 FROM project_test_profiles WHERE project_id = ?", (project_id,)
        ).fetchone() is None:
            return None
        waiting = connection.execute(
            "SELECT check_id FROM mainline_checks WHERE project_id = ? AND status = 'QUEUED' "
            "ORDER BY created_at LIMIT 1",
            (project_id,),
        ).fetchone()
        if waiting is not None:
            connection.execute(
                "UPDATE mainline_checks SET mainline_commit = ?, trigger_ref = ?, "
                "trigger_ticket_id = ? WHERE check_id = ?",
                (mainline_commit, trigger_ref, trigger_ticket_id, waiting["check_id"]),
            )
            return str(waiting["check_id"])
        check_id = _id(connection, "main_check")
        connection.execute(
            "INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, "
            "trigger_ticket_id, status, created_at) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?)",
            (check_id, project_id, mainline_commit, trigger_ref, trigger_ticket_id, _iso()),
        )
        self._event(
            connection, _guard_actor(project_id), project_id, "mainline_check.queued",
            "mainline_check", check_id, 1,
            {"mainline_commit": mainline_commit, "trigger_ref": trigger_ref},
        )
        return check_id

    def execute_queued_mainline_check(self, check_id: str) -> dict[str, Any]:
        """Run the project's test command on the recorded main commit (supervisor test slot)."""
        with self.store.connection(immediate=True) as connection:
            check = self._mainline_check(connection, check_id)
            if check["status"] != "QUEUED":
                return check
            project_id = check["project_id"]
            profile = _row(connection.execute(
                "SELECT test_command, timeout_seconds FROM project_test_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone())
            repository = _row(connection.execute(
                "SELECT repository_path FROM project_repositories WHERE project_id = ?",
                (project_id,),
            ).fetchone())
            connection.execute(
                "UPDATE mainline_checks SET status = 'RUNNING', started_at = ? WHERE check_id = ?",
                (_iso(), check_id),
            )
        if profile is None or repository is None:
            return self.fail_queued_mainline_check(
                check_id, "The project has no test command or repository to check main with."
            )
        command = profile["test_command"]
        if isinstance(command, str):
            command = json.loads(command)
        try:
            with self.git_worktrees.integration_checkout(
                repository_path=repository["repository_path"], project_id=project_id,
                ticket_id="mainline-check", candidate_commit=check["mainline_commit"],
            ) as checkout_path:
                execution = self.project_tests.execute(
                    command, checkout_path, int(profile["timeout_seconds"])
                )
        except DomainError as error:
            return self.fail_queued_mainline_check(check_id, str(error))
        infrastructure = (
            execution.artifact_mismatch
            or execution.termination_reason in _INFRASTRUCTURE
            or bool(execution.capture_errors)
        )
        if infrastructure:
            outcome = "infrastructure"
        elif execution.termination_reason == "passed" and execution.exit_code == 0 and not execution.timed_out:
            outcome = "green"
        else:
            outcome = "red"
        tail = ((execution.stdout or "")[-_OUTPUT_TAIL_CHARS:] + "\n"
                + (execution.stderr or "")[-1000:]).strip()
        return self._finish_mainline_check(
            check_id, outcome=outcome, exit_code=execution.exit_code,
            termination_reason=execution.termination_reason, output_tail=tail,
        )

    def fail_queued_mainline_check(self, check_id: str, error_message: str) -> dict[str, Any]:
        """A check that could not run: counted as a failure, never as a red main."""
        return self._finish_mainline_check(
            check_id, outcome="infrastructure", exit_code=None,
            termination_reason="check_not_run", output_tail=None,
            error_message=str(error_message)[:2000],
        )

    def _finish_mainline_check(
        self,
        check_id: str,
        *,
        outcome: str,
        exit_code: int | None,
        termination_reason: str | None,
        output_tail: str | None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            check = self._mainline_check(connection, check_id)
            if check["status"] in {"COMPLETED", "FAILED"}:
                return check
            project_id = check["project_id"]
            commit = check["mainline_commit"]
            status = "COMPLETED" if outcome in {"green", "red"} else "FAILED"
            connection.execute(
                "UPDATE mainline_checks SET status = ?, outcome = ?, exit_code = ?, "
                "termination_reason = ?, output_tail = ?, error_message = ?, completed_at = ? "
                "WHERE check_id = ?",
                (status, outcome, exit_code, termination_reason, output_tail, error_message,
                 _iso(), check_id),
            )
            actor = _guard_actor(project_id)
            self._event(
                connection, actor, project_id, f"mainline_check.{outcome}", "mainline_check",
                check_id, 2,
                {"mainline_commit": commit, "exit_code": exit_code,
                 "termination_reason": termination_reason},
            )
            trigger = (
                self._ticket(connection, check["trigger_ticket_id"])
                if check.get("trigger_ticket_id") else None
            )
            if outcome == "green":
                self._record_run_outcome(connection, project_id, succeeded=True)
                return self._mainline_check(connection, check_id)
            if outcome == "red":
                revert = self._open_revert_ticket(connection, check, trigger)
                connection.execute(
                    "UPDATE mainline_checks SET revert_ticket_id = ? WHERE check_id = ?",
                    (revert["ticket_id"] if revert else None, check_id),
                )
                last_green = connection.execute(
                    "SELECT mainline_commit FROM mainline_checks WHERE project_id = ? "
                    "AND outcome IN ('green', 'covered_by_gate') ORDER BY completed_at DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                self._pause_merge_queue(
                    connection, project_id, pause_reason=RED_MAIN_PAUSE,
                    detail=(
                        f"The project's test command failed on main at {commit} "
                        f"(after {check['trigger_ref']}; last green main "
                        f"{last_green['mainline_commit'] if last_green else 'unknown'}). "
                        + (f"Revert ticket {revert['ticket_id']} names the commit. "
                           if revert else "")
                    ),
                    ticket=revert or trigger,
                    evidence_refs=[f"mainline_check:{check_id}", f"mainline:{commit}",
                                   check["trigger_ref"]],
                    artifact_commit=commit,
                )
            self._record_run_outcome(
                connection, project_id, succeeded=False, ticket=trigger,
                evidence_ref=f"mainline_check:{check_id}",
            )
            return self._mainline_check(connection, check_id)

    def _open_revert_ticket(
        self, connection: Any, check: dict[str, Any], trigger: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """A high-priority BACKLOG ticket naming the offending commit.

        It goes through the ordinary gate like any candidate: Planner completes
        its contract and readies it; nothing is reverted or reset here.
        """
        if trigger is None:
            return None
        project_id = check["project_id"]
        commit = check["mainline_commit"]
        ticket_id = _id(connection, "REVERT")
        timestamp = _iso()
        actor = _guard_actor(project_id)
        connection.execute(
            """
            INSERT INTO tickets
                (ticket_id, feature_id, parent_ticket_id, ticket_type, title, objective,
                 status, priority, required_role, acceptance_criteria, validation_contract,
                 preservation_constraints, file_scope, created_at, updated_at)
            VALUES (?, ?, ?, 'revert', ?, ?, ?, 95, ?, ?, '[]', ?, ?, ?, ?)
            """,
            (
                ticket_id, trigger["feature_id"], trigger["ticket_id"],
                f"Restore a green main: revert or fix {commit[:12]}",
                (
                    f"The project's test command failed on the default branch at {commit}, "
                    f"right after {check['trigger_ref']} (ticket {trigger['ticket_id']}). "
                    f"Revert {commit} with git revert (never reset or force-push main), or "
                    "land a fix, as an ordinary candidate through the gate. Evidence: "
                    f"mainline_check:{check['check_id']}."
                ),
                TicketStatus.BACKLOG.value,
                Role.DEVELOPER.value,
                json.dumps([
                    f"The project's frozen test command passes on the default branch after "
                    f"this lands, with {commit} reverted or fixed."
                ]),
                json.dumps(["Never reset or force-push the default branch."]),
                json.dumps(trigger.get("file_scope") or []),
                timestamp, timestamp,
            ),
        )
        _record_ticket_created(connection, actor, ticket_id)
        self._event(
            connection, actor, project_id, "ticket.created", "ticket", ticket_id, 1,
            {"title": f"revert {commit}", "required_role": Role.DEVELOPER.value,
             "ticket_type": "revert", "offending_commit": commit,
             "mainline_check_id": check["check_id"]},
        )
        return self._ticket(connection, ticket_id)

    @staticmethod
    def _mainline_check(connection: Any, check_id: str) -> dict[str, Any]:
        row = _row(connection.execute(
            "SELECT * FROM mainline_checks WHERE check_id = ?", (check_id,)
        ).fetchone())
        if row is None:
            raise ValidationError("Mainline check not found")
        return row
