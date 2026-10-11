"""Durable scoped admission, execution accounting and graceful stop.

All mutations use the store's serialized write transaction on both backends.
No client success establishes delivery; completion reads the existing ledgers.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path

from pydantic import ValidationError as ModelValidationError

from hoh_control_plane.contracts import AutonomyLimits
from hoh_control_plane.domain import AutonomyExecutionPaused
from hoh_control_plane.service._common import (
    Actor, Role, TicketStatus, AuthorizationError, ConflictError, ValidationError,
    _id, _iso, _now, _row, require_project, require_role,
)

_STOP_REASONS = {
    "owner_stop", "wall_budget", "launch_budget", "suite_budget", "landing_budget",
    "idle", "provider_limit", "worker_failure", "unknown_usage", "token_threshold",
    "retry_budget",
    "red_main", "history_rewritten", "plan_stale", "repeated_failure", "capacity_unavailable",
}
_OUTCOMES = {"success", "provider_limit", "failure", "crash", "unknown"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _run_row(row):
    result = dict(row)
    for field in ("ticket_ids", "role_agents", "limits"):
        result[field] = json.loads(result[field])
    return result


class AutonomyMixin:
    def _autonomy_run(self, connection, run_id):
        row = self._one(connection, "SELECT * FROM autonomy_runs WHERE run_id = ?", (run_id,))
        return _run_row(row)

    @staticmethod
    def _autonomy_key(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValidationError("A bounded idempotency key is required")

    def _autonomy_available(self, connection, project_id, ticket_ids, role_agents):
        if set(role_agents) != {"developer", "qa", "integrator"} or any(
            not isinstance(agent, str) or not agent for agent in role_agents.values()
        ):
            raise ValidationError("Select exactly one registered agent for each execution role")
        agents = {}
        for role, agent_id in role_agents.items():
            row = _row(connection.execute("SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)).fetchone())
            if row is None or row["role"] != role or project_id not in row["project_ids"] or row["expires_at"] <= _iso():
                raise ConflictError("Selected execution agent is unavailable", {"role": role})
            agents[role] = row
        maker, checker = agents["developer"].get("model_family"), agents["qa"].get("model_family")
        if maker not in {"openai", "anthropic", "google"} or checker not in {"openai", "anthropic", "google"} or maker == checker:
            raise ConflictError("Autonomy requires known independent Developer and QA model families")
        qa = agents["qa"]
        if not qa.get("machine_id") or qa["machine_id"] != qa.get("capability_approved_machine_id"):
            raise ConflictError("QA requires a machine-bound approved capability profile")
        from hoh_control_plane.service._common import _required_execution_capabilities
        for ticket_id in ticket_ids:
            ticket = self._ticket(connection, ticket_id)
            required = _required_execution_capabilities(ticket)
            if not required <= set(qa["execution_capabilities"]) or not required <= set(qa["approved_execution_capabilities"]):
                raise ConflictError("QA capability gap", {"ticket_id": ticket_id})

    def autonomy_start(self, actor: Actor, project_id: str, limits: dict, role_agents: dict,
                       idempotency_key: str, ticket_ids: list[str] | None = None,
                       feature_id: str | None = None, idle_seconds: int = 300):
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._autonomy_key(idempotency_key)
        if bool(ticket_ids) == bool(feature_id) or not 30 <= idle_seconds <= 86400:
            raise ValidationError("Choose a nonempty ticket scope or a feature, and bounded idle time")
        try:
            limits = AutonomyLimits.model_validate(limits).model_dump()
        except ModelValidationError as error:
            raise ValidationError("Invalid autonomy admission limits") from error
        if ticket_ids and (len(ticket_ids) > 500 or len(set(ticket_ids)) != len(ticket_ids)):
            raise ValidationError("Ticket scope must be unique and contain at most 500 tickets")
        request = _json({"project_id": project_id, "limits": limits, "role_agents": role_agents,
                         "ticket_ids": sorted(ticket_ids or []), "feature_id": feature_id, "idle_seconds": idle_seconds})
        with self.store.connection(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM autonomy_runs WHERE created_by = ? AND start_key = ?",
                                          (actor.agent_id, idempotency_key)).fetchone()
            if existing:
                if existing["start_request"] != request:
                    raise ConflictError("Autonomy start key already names different input")
                return self._autonomy_view(connection, _run_row(existing))
            if feature_id:
                feature = self._one(connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,))
                if feature["project_id"] != project_id:
                    raise AuthorizationError("Feature belongs to another project")
                ticket_ids = [row[0] for row in connection.execute("SELECT ticket_id FROM tickets WHERE feature_id = ? ORDER BY ticket_id", (feature_id,)).fetchall()]
            ticket_ids = sorted(ticket_ids or [])
            if not ticket_ids or len(ticket_ids) > 500:
                raise ValidationError("Run needs a bounded nonempty frozen ticket scope")
            for ticket_id in ticket_ids:
                if self._project_for_ticket(connection, ticket_id) != project_id:
                    raise AuthorizationError("Ticket belongs to another project")
                self._require_repository_reconciled(connection, project_id, ticket_id)
                if connection.execute("SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)).fetchone():
                    raise ConflictError("Run cannot adopt an existing worker lease")
            for row in connection.execute("SELECT ticket_ids FROM autonomy_runs WHERE project_id = ? AND state != 'COMPLETED'", (project_id,)).fetchall():
                if set(ticket_ids) & set(json.loads(row[0])):
                    raise ConflictError("Ticket scope already belongs to a retained autonomy run")
            self._autonomy_available(connection, project_id, ticket_ids, role_agents)
            run_id, now = _id(connection, "run"), _now()
            connection.execute("""INSERT INTO autonomy_runs
                (run_id, project_id, created_by, start_key, start_request, ticket_ids, role_agents,
                 limits, state, created_at, deadline_at, idle_seconds, last_activity_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)""",
                (run_id, project_id, actor.agent_id, idempotency_key, request, _json(ticket_ids),
                 _json(role_agents), _json(limits), _iso(now), _iso(now + timedelta(seconds=limits["wall_seconds"])), idle_seconds, _iso(now)))
            return self._autonomy_view(connection, self._autonomy_run(connection, run_id))

    def _autonomy_counts(self, connection, run_id):
        attempts = connection.execute("SELECT state, observed_tokens, lease_id FROM autonomy_attempts WHERE run_id = ?", (run_id,)).fetchall()
        live_leases = {row[0] for row in connection.execute("SELECT lease_id FROM leases WHERE expires_at > ?", (_iso(),)).fetchall()}
        return {
            "launch_attempts": len(attempts),
            "in_flight": sum(row["state"] == "ADMITTED" or row["lease_id"] in live_leases for row in attempts),
            "observed_tokens": sum(row["observed_tokens"] or 0 for row in attempts),
            **{unit: connection.execute("SELECT COUNT(*) FROM autonomy_reservations WHERE run_id = ? AND unit = ? AND state != 'RELEASED'", (run_id, unit)).fetchone()[0]
               for unit in ("suite_starts", "landed_tickets")},
        }

    def _autonomy_busy(self, connection, run, *, include_held=False):
        for ticket_id in run["ticket_ids"]:
            if connection.execute("SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?", (ticket_id, _iso())).fetchone():
                return True
            for table in ("worktree_provision_jobs", "candidate_submission_jobs", "qa_test_jobs", "merge_jobs"):
                held_filter = "" if include_held or table in {"worktree_provision_jobs", "candidate_submission_jobs"} else " AND (status = 'RUNNING' OR autonomy_hold_run_id IS NULL)"
                if connection.execute(f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING'){held_filter}", (ticket_id,)).fetchone():
                    return True
            held_filter = "" if include_held else " AND (status = 'RUNNING' OR autonomy_hold_run_id IS NULL)"
            if connection.execute(f"SELECT 1 FROM mainline_checks WHERE trigger_ticket_id = ? AND status IN ('QUEUED', 'RUNNING'){held_filter}", (ticket_id,)).fetchone():
                return True
        return False

    def _autonomy_notice(self, connection, run):
        delivered, blocked = [], []
        for ticket_id in run["ticket_ids"]:
            status = self._ticket(connection, ticket_id)["status"]
            if status == "DONE":
                delivered.append(ticket_id)
            elif status not in {"CANCELLED", "SUPERSEDED"}:
                blocked.append(ticket_id)
        payload = {"state": run["state"], "reason": run["stop_reason"], "delivered_ticket_ids": delivered,
                   "blocked_ticket_ids": blocked, "required_action": "none" if run["state"] == "COMPLETED" else "Planner must inspect retained work and explicitly resume"}
        connection.execute("""INSERT INTO autonomy_notifications
            (notification_id, run_id, transition_version, payload, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (run_id, transition_version) DO NOTHING""",
            (_id(connection, "notice"), run["run_id"], run["version"], _json(payload), _iso()))

    def _autonomy_transition(self, connection, run, state, reason):
        if run["state"] == state:
            return run
        connection.execute("UPDATE autonomy_runs SET state = ?, stop_reason = ?, version = version + 1 WHERE run_id = ?",
                           (state, reason, run["run_id"]))
        run = self._autonomy_run(connection, run["run_id"])
        if state in {"PAUSED", "COMPLETED"}:
            self._autonomy_notice(connection, run)
        return run

    def _autonomy_drain(self, connection, run, reason):
        if run["state"] == "ACTIVE":
            run = self._autonomy_transition(connection, run, "DRAINING", reason)
        return run

    def _autonomy_refresh(self, connection, run, *, allow_completion=False):
        for reservation in connection.execute("SELECT * FROM autonomy_reservations WHERE run_id = ? AND unit = 'suite_starts' AND state = 'RESERVED'", (run["run_id"],)).fetchall():
            kind, identifier = reservation["job_ref"].split(":", 1)
            table, field = {"qa": ("qa_test_jobs", "test_run_id"), "merge": ("merge_jobs", "merge_job_id"), "main": ("mainline_checks", "check_id")}[kind]
            job = connection.execute(f"SELECT status FROM {table} WHERE {field} = ?", (identifier,)).fetchone()
            if job is not None and job[0] != "RUNNING":
                # A lost callback cannot refund a child that may have executed.
                connection.execute("UPDATE autonomy_reservations SET state = 'SETTLED', settled_at = ? WHERE reservation_id = ?", (_iso(), reservation["reservation_id"]))
                run = self._autonomy_drain(connection, run, "unknown_usage")
        for attempt in connection.execute("SELECT * FROM autonomy_attempts WHERE run_id = ? AND state = 'ADMITTED'", (run["run_id"],)).fetchall():
            live = connection.execute("SELECT 1 FROM leases WHERE lease_id = ? AND expires_at > ?", (attempt["lease_id"], _iso())).fetchone()
            jobs = any(connection.execute(f"SELECT 1 FROM {table} WHERE lease_id = ? AND status = 'RUNNING'", (attempt["lease_id"],)).fetchone()
                       for table in ("worktree_provision_jobs", "candidate_submission_jobs", "qa_test_jobs", "merge_jobs"))
            if not live and not jobs:
                connection.execute("UPDATE autonomy_attempts SET state = 'SETTLED', cli_outcome = 'unknown', finish_request = 'server:expired-attempt', settled_at = ? WHERE attempt_id = ?", (_iso(), attempt["attempt_id"]))
                run = self._autonomy_drain(connection, run, "unknown_usage")
        counts = self._autonomy_counts(connection, run["run_id"])
        if run["state"] == "ACTIVE":
            reason = None
            guardrail = connection.execute("SELECT pause_reason FROM project_run_guardrails WHERE project_id = ? AND merge_queue_paused = 1", (run["project_id"],)).fetchone()
            if guardrail and guardrail[0] == "mainline_check_failed":
                reason = "red_main"
            elif guardrail and guardrail[0] == "consecutive_failures_exceeded":
                reason = "repeated_failure"
            elif _iso() >= run["deadline_at"]:
                reason = "wall_budget"
            elif counts["launch_attempts"] >= run["limits"]["launch_attempts"]:
                reason = "launch_budget"
            elif run["limits"]["observed_tokens"] and counts["observed_tokens"] >= run["limits"]["observed_tokens"]:
                reason = "token_threshold"
            elif not counts["in_flight"] and (_now() - datetime.fromisoformat(run["last_activity_at"].replace("Z", "+00:00"))).total_seconds() >= run["idle_seconds"]:
                reason = "idle"
            if reason:
                run = self._autonomy_drain(connection, run, reason)
        busy = counts["in_flight"] or self._autonomy_busy(connection, run) or connection.execute(
            "SELECT 1 FROM autonomy_reservations WHERE run_id = ? AND state = 'RESERVED'", (run["run_id"],)).fetchone()
        if run["state"] in {"ACTIVE", "DRAINING", "PAUSED"} and not busy:
            if allow_completion and self._autonomy_complete(connection, run):
                run = self._autonomy_transition(connection, run, "COMPLETED", "goal_complete")
            elif run["state"] == "DRAINING":
                run = self._autonomy_transition(connection, run, "PAUSED", run["stop_reason"])
        return run

    def _autonomy_complete(self, connection, run):
        if self._autonomy_busy(connection, run, include_held=True):
            return False
        if any(self._ticket(connection, ticket)["status"] not in {"DONE", "CANCELLED", "SUPERSEDED"} for ticket in run["ticket_ids"]):
            return False
        repository = connection.execute("SELECT * FROM project_repositories WHERE project_id = ?", (run["project_id"],)).fetchone()
        if not repository or not repository["observed_at"] or repository["observed_at"] < (run["resume_at"] or run["created_at"]):
            return False
        # Select the latest check on current main, not a historical green row.
        check = connection.execute("SELECT * FROM mainline_checks WHERE project_id = ? AND mainline_commit = ? ORDER BY created_at DESC, check_id DESC LIMIT 1",
                                   (run["project_id"], repository["observed_mainline_commit"])).fetchone()
        return bool(check and check["status"] == "COMPLETED" and check["outcome"] in {"green", "covered_by_gate"})

    def _autonomy_view(self, connection, run):
        notifications = []
        for row in connection.execute("SELECT * FROM autonomy_notifications WHERE run_id = ? AND acknowledged_at IS NULL ORDER BY created_at", (run["run_id"],)).fetchall():
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            notifications.append(value)
        attempts = [dict(row) for row in connection.execute("SELECT attempt_id, ticket_id, actor_id, role, lease_id, state, observed_tokens, cli_outcome, result_ref FROM autonomy_attempts WHERE run_id = ? ORDER BY created_at", (run["run_id"],)).fetchall()]
        return {"run": run, "counts": self._autonomy_counts(connection, run["run_id"]), "attempts": attempts,
                "notifications": notifications, "token_semantics": "observed admission threshold; running workers may overshoot; unknown usage pauses"}

    def _autonomy_authorize(self, actor, run, *, owner=False):
        require_project(actor, run["project_id"])
        if owner:
            require_role(actor, Role.PLANNER, Role.ADMIN)
        elif actor.role not in {Role.PLANNER, Role.ADMIN} and run["role_agents"].get(actor.role.value) != actor.agent_id:
            raise AuthorizationError("Actor is not selected for this run")

    def autonomy_status(self, actor: Actor, run_id: str):
        # Observation is external I/O; final predicate and transition share one
        # write transaction after it. An empty inbox never enters this path.
        with self.store.connection() as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run)
            terminal = all(self._ticket(connection, ticket)["status"] in {"DONE", "CANCELLED", "SUPERSEDED"} for ticket in run["ticket_ids"])
        observed = False
        if terminal and run["state"] in {"ACTIVE", "DRAINING", "PAUSED"}:
            observer = Actor("system:autonomy", Role.ADMIN, frozenset({run["project_id"]}))
            result = self.sync_repository(observer, run["project_id"], detect_only=True)
            observed = not result.get("detected_only", False)
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run)
            return self._autonomy_view(connection, self._autonomy_refresh(connection, run, allow_completion=observed))

    def autonomy_stop(self, actor: Actor, run_id: str, reason: str = "owner_stop"):
        if reason not in _STOP_REASONS:
            raise ValidationError("Use a redacted stop reason enum")
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run, owner=True)
            return self._autonomy_view(connection, self._autonomy_refresh(connection, self._autonomy_drain(connection, run, reason)))

    def autonomy_signal(self, actor: Actor, run_id: str, reason: str):
        """Selected controllers can stop on a typed observation, never resume."""
        if reason not in _STOP_REASONS - {"owner_stop"}:
            raise ValidationError("Use a redacted execution stop signal")
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run)
            run = self._autonomy_drain(connection, run, reason)
            return self._autonomy_view(connection, self._autonomy_refresh(connection, run))

    def autonomy_resume(self, actor: Actor, run_id: str, expected_version: int, limits: dict | None = None):
        replacement = None
        if limits is not None:
            try:
                replacement = AutonomyLimits.model_validate(limits).model_dump()
            except ModelValidationError as error:
                raise ValidationError("Invalid explicit resume limits") from error
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run, owner=True)
            if run["version"] != expected_version or run["state"] != "PAUSED":
                raise ConflictError("Resume requires current version of a paused run")
            counts = self._autonomy_counts(connection, run_id)
            new_limits = replacement or run["limits"]
            if any(new_limits[unit] < counts[unit] for unit in ("launch_attempts", "suite_starts", "landed_tickets", "in_flight")):
                raise ConflictError("Explicit limits cannot drop below retained consumed or reserved units")
            if counts["in_flight"] or self._autonomy_busy(connection, run):
                raise ConflictError("Drain has not settled")
            if any(counts[unit] >= new_limits[unit] for unit in ("launch_attempts", "suite_starts", "landed_tickets")) or (new_limits["observed_tokens"] and counts["observed_tokens"] >= new_limits["observed_tokens"]):
                raise ConflictError("Retained run has exhausted admission budget")
            if replacement is None and _iso() >= run["deadline_at"]:
                raise ConflictError("An expired wall window requires explicit finite limits")
            for ticket_id in run["ticket_ids"]:
                self._require_repository_reconciled(connection, run["project_id"], ticket_id)
            self._autonomy_available(connection, run["project_id"], run["ticket_ids"], run["role_agents"])
            guardrail = connection.execute("SELECT pause_reason FROM project_run_guardrails WHERE project_id = ? AND merge_queue_paused = 1", (run["project_id"],)).fetchone()
            if guardrail and guardrail[0] in {"mainline_check_failed", "consecutive_failures_exceeded"}:
                raise ConflictError("Project guardrail still requires Planner remediation")
            now = _now()
            deadline = _iso(now + timedelta(seconds=new_limits["wall_seconds"])) if replacement else run["deadline_at"]
            connection.execute("UPDATE autonomy_runs SET state = 'ACTIVE', stop_reason = NULL, version = version + 1, resume_at = ?, last_activity_at = ?, deadline_at = ?, limits = ? WHERE run_id = ?",
                (_iso(now), _iso(now), deadline, _json(new_limits), run_id))
            self._event(connection, actor, run["project_id"], "autonomy.resumed", "run", run_id,
                run["version"] + 1, {"old_limits": run["limits"], "new_limits": new_limits, "old_deadline": run["deadline_at"], "new_deadline": deadline})
            for table in ("qa_test_jobs", "merge_jobs", "mainline_checks"):
                connection.execute(f"UPDATE {table} SET autonomy_hold_run_id = NULL WHERE autonomy_hold_run_id = ?", (run_id,))
            return self._autonomy_view(connection, self._autonomy_run(connection, run_id))

    def autonomy_acknowledge(self, actor: Actor, run_id: str, notification_id: str):
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run, owner=True)
            self._one(connection, "SELECT * FROM autonomy_notifications WHERE run_id = ? AND notification_id = ?", (run_id, notification_id))
            connection.execute("UPDATE autonomy_notifications SET acknowledged_at = COALESCE(acknowledged_at, ?) WHERE run_id = ? AND notification_id = ?", (_iso(), run_id, notification_id))
            return {"acknowledged": notification_id}

    def autonomy_reconcile(self, actor: Actor, run_id: str):
        """Conservatively settle lost callbacks, using server/Git landing truth."""
        with self.store.connection() as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run, owner=True)
            reservations = [dict(row) for row in connection.execute("SELECT * FROM autonomy_reservations WHERE run_id = ? AND unit = 'landed_tickets' AND state = 'RESERVED'", (run_id,)).fetchall()]
        resolutions = []
        unresolved = []
        for reservation in reservations:
            kind, identifier = reservation["job_ref"].split(":", 1)
            if kind != "merge":
                unresolved.append(reservation["reservation_id"])
                continue
            with self.store.connection() as connection:
                job = self._merge_job(connection, identifier)
                candidate = self._one(connection, "SELECT * FROM candidates WHERE candidate_id = ?", (job["candidate_id"],))
                repository = self._one(connection, "SELECT * FROM project_repositories WHERE project_id = ?", (run["project_id"],))
                project = self._one(connection, "SELECT * FROM projects WHERE project_id = ?", (run["project_id"],))
            if job["status"] == "RUNNING":
                unresolved.append(reservation["reservation_id"])
                continue
            landed = job.get("merge_status") == "merged" or self.git_worktrees.commit_is_on_branch(
                repository_path=repository["repository_path"], default_branch=project["default_branch"], commit=candidate["commit_sha"])
            if landed:
                state = "SETTLED"
            else:
                head = self.git_worktrees._resolve_commit(Path(repository["repository_path"]), project["default_branch"])
                if head != candidate["base_commit"]:
                    unresolved.append(reservation["reservation_id"])
                    continue
                state = "RELEASED"
            resolutions.append((reservation["reservation_id"], state, identifier, job["status"]))
        with self.store.connection(immediate=True) as connection:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run, owner=True)
            for reservation_id, state, identifier, expected_status in resolutions:
                job = self._merge_job(connection, identifier)
                if job["status"] != expected_status:
                    raise ConflictError("Merge changed during reservation reconciliation")
                connection.execute("UPDATE autonomy_reservations SET state = ?, settled_at = ? WHERE reservation_id = ? AND state = 'RESERVED'", (state, _iso(), reservation_id))
                self._event(connection, actor, run["project_id"], "autonomy.reservation_reconciled", "run", run_id,
                    run["version"], {"reservation_id": reservation_id, "state": state, "merge_job_id": identifier})
            run = self._autonomy_refresh(connection, run)
            result = self._autonomy_view(connection, run)
            unresolved = [row[0] for row in connection.execute("SELECT reservation_id FROM autonomy_reservations WHERE run_id = ? AND state = 'RESERVED'", (run_id,)).fetchall()]
            result["unresolved_reservation_ids"] = unresolved
            result["required_action"] = "Use existing supervisor/merge recovery to settle running or ambiguous work, then reconcile again" if unresolved else "none"
            return result

    def autonomy_admit(self, actor: Actor, run_id: str, ticket_id: str, expected_version: int,
                       context_snapshot_id: str, idempotency_key: str, lease_seconds: int = 900):
        require_role(actor, Role.DEVELOPER, Role.QA, Role.INTEGRATOR)
        self._autonomy_key(idempotency_key)
        states = {
            Role.DEVELOPER: ({TicketStatus.READY, TicketStatus.REWORK}, TicketStatus.DEV_IN_PROGRESS, "ticket.claimed"),
            Role.QA: ({TicketStatus.QA_READY}, TicketStatus.QA_IN_PROGRESS, "qa.started"),
            Role.INTEGRATOR: ({TicketStatus.VERIFIED, TicketStatus.MERGE_READY}, TicketStatus.MERGE_READY, "integration.started"),
        }
        allowed, next_status, event = states[actor.role]
        return self._claim(actor, ticket_id, expected_version, lease_seconds, allowed, next_status, event,
                           context_snapshot_id, autonomy_run_id=run_id, autonomy_key=idempotency_key)

    def _autonomy_claim_preflight(self, connection, actor, ticket_id, run_id, key, request):
        if run_id:
            run = self._autonomy_run(connection, run_id)
            self._autonomy_authorize(actor, run)
            if ticket_id not in run["ticket_ids"]:
                raise AuthorizationError("Ticket is outside the frozen run scope")
            existing = connection.execute("SELECT * FROM autonomy_attempts WHERE run_id = ? AND actor_id = ? AND admission_key = ?", (run_id, actor.agent_id, key)).fetchone()
            if existing:
                if existing["admission_request"] != request:
                    raise ConflictError("Admission key names different input")
                return json.loads(existing["claim_result"])
            run = self._autonomy_refresh(connection, run)
            if run["state"] != "ACTIVE":
                return {"admitted": False, "run_id": run_id, "state": run["state"], "reason": run["stop_reason"]}
            counts = self._autonomy_counts(connection, run_id)
            if counts["in_flight"] >= run["limits"]["in_flight"]:
                return {"admitted": False, "reason": "in_flight", "run_id": run_id}
            failures = connection.execute("SELECT COUNT(*) FROM autonomy_attempts WHERE run_id = ? AND ticket_id = ? AND role = ? AND cli_outcome IN ('crash', 'failure', 'unknown')", (run_id, ticket_id, actor.role.value)).fetchone()[0]
            if failures > run["limits"]["crash_retries"]:
                run = self._autonomy_drain(connection, run, "retry_budget")
                return {"admitted": False, "reason": run["stop_reason"], "run_id": run_id}
        else:
            project_id = self._project_for_ticket(connection, ticket_id)
            for row in connection.execute("SELECT ticket_ids FROM autonomy_runs WHERE project_id = ? AND state != 'COMPLETED'", (project_id,)).fetchall():
                if ticket_id in json.loads(row[0]):
                    raise ConflictError("Scoped work requires autonomy.admit")
        return None

    def _autonomy_record_claim(self, connection, actor, run_id, key, request, result):
        attempt_id = _id(connection, "attempt")
        result.update({"admitted": True, "run_id": run_id, "attempt_id": attempt_id})
        result["worker_brief"] = None
        result["next_action"] = "Resolve the current ticket packet before continuing or spawning a recovered attempt"
        connection.execute("""INSERT INTO autonomy_attempts (attempt_id, run_id, ticket_id, actor_id, role,
            admission_key, admission_request, lease_id, claim_result, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ADMITTED', ?)""",
            (attempt_id, run_id, result["ticket"]["ticket_id"], actor.agent_id, actor.role.value, key, request,
             result["lease"]["lease_id"], _json(result), _iso()))
        connection.execute("UPDATE autonomy_runs SET last_activity_at = ? WHERE run_id = ?", (_iso(), run_id))

    def autonomy_finish_attempt(self, actor: Actor, attempt_id: str, observed_tokens: int | None,
                                cli_outcome: str, idempotency_key: str):
        self._autonomy_key(idempotency_key)
        if cli_outcome not in _OUTCOMES or (observed_tokens is not None and (type(observed_tokens) is not int or not 0 <= observed_tokens <= 2000000000)):
            raise ValidationError("Invalid bounded worker telemetry")
        request = _json({"tokens": observed_tokens, "outcome": cli_outcome, "key": idempotency_key})
        with self.store.connection(immediate=True) as connection:
            attempt = self._one(connection, "SELECT * FROM autonomy_attempts WHERE attempt_id = ?", (attempt_id,))
            run = self._autonomy_run(connection, attempt["run_id"])
            self._autonomy_authorize(actor, run)
            if actor.agent_id != attempt["actor_id"]:
                raise AuthorizationError("Attempt belongs to another actor")
            if attempt["state"] == "SETTLED":
                if attempt["finish_request"] != request:
                    raise ConflictError("Attempt already settled with different telemetry")
                return dict(attempt)
            # Read durable truth even when the CLI exited nonzero after submission.
            result_ref = None
            for table, id_field in (("candidate_submission_jobs", "submission_id"), ("qa_test_jobs", "test_run_id"), ("merge_jobs", "merge_job_id")):
                row = connection.execute(f"SELECT {id_field} FROM {table} WHERE lease_id = ? AND status IN ('COMPLETED', 'RUNNING', 'QUEUED') ORDER BY created_at DESC LIMIT 1", (attempt["lease_id"],)).fetchone()
                if row:
                    result_ref = f"{table}:{row[0]}"
                    break
            connection.execute("UPDATE autonomy_attempts SET state = 'SETTLED', observed_tokens = ?, cli_outcome = ?, result_ref = ?, finish_request = ?, settled_at = ? WHERE attempt_id = ?",
                               (observed_tokens, cli_outcome, result_ref, request, _iso(), attempt_id))
            reason = "unknown_usage" if observed_tokens is None else "provider_limit" if cli_outcome == "provider_limit" else "worker_failure" if cli_outcome in {"failure", "crash", "unknown"} and result_ref is None else None
            if reason:
                run = self._autonomy_drain(connection, run, reason)
            self._autonomy_refresh(connection, run)
            return dict(self._one(connection, "SELECT * FROM autonomy_attempts WHERE attempt_id = ?", (attempt_id,)))

    def _autonomy_job_run(self, connection, job_ref):
        kind, identifier = job_ref.split(":", 1)
        if kind == "main":
            check = connection.execute("SELECT trigger_ticket_id FROM mainline_checks WHERE check_id = ?", (identifier,)).fetchone()
            if not check or not check[0]:
                return None
            row = connection.execute("SELECT run_id FROM autonomy_reservations WHERE ticket_id = ? AND unit = 'landed_tickets' ORDER BY created_at DESC LIMIT 1", (check[0],)).fetchone()
        else:
            table, field = {"qa": ("qa_test_jobs", "test_run_id"), "merge": ("merge_jobs", "merge_job_id")}[kind]
            row = connection.execute(f"SELECT a.run_id FROM autonomy_attempts a JOIN {table} j ON j.lease_id = a.lease_id WHERE j.{field} = ?", (identifier,)).fetchone()
        return None if row is None else row[0]

    def _autonomy_reserve(self, job_ref, unit, unit_key, ticket_id=None):
        denied = None
        with self.store.connection(immediate=True) as connection:
            run_id = self._autonomy_job_run(connection, job_ref)
            if run_id is None:
                return None
            run = self._autonomy_run(connection, run_id)
            existing = connection.execute("SELECT * FROM autonomy_reservations WHERE run_id = ? AND unit = ? AND unit_key = ?", (run_id, unit, unit_key)).fetchone()
            if existing and existing["state"] != "RELEASED":
                # Ambiguous response/restart is not permission to execute twice.
                raise AutonomyExecutionPaused("Execution reservation already exists; reconcile durable job before retry")
            count = self._autonomy_counts(connection, run_id)[unit]
            if count >= run["limits"][unit]:
                self._autonomy_drain(connection, run, "suite_budget" if unit == "suite_starts" else "landing_budget")
                denied = "Autonomy execution ceiling reached; preserve job and explicitly resume after review"
            elif run["state"] in {"PAUSED", "COMPLETED"}:
                denied = "Run no longer admits execution"
            else:
                # Accepted jobs may continue while draining, within hard ceilings.
                reservation_id = existing["reservation_id"] if existing else _id(connection, "reservation")
                if existing:
                    connection.execute("UPDATE autonomy_reservations SET job_ref = ?, state = 'RESERVED', settled_at = NULL WHERE reservation_id = ?", (job_ref, reservation_id))
                else:
                    connection.execute("INSERT INTO autonomy_reservations (reservation_id, run_id, unit, unit_key, ticket_id, job_ref, state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?)",
                                       (reservation_id, run_id, unit, unit_key, ticket_id, job_ref, _iso()))
                return reservation_id
        raise AutonomyExecutionPaused(denied, {"reason": "autonomy_execution_budget"})

    def _autonomy_settle_reservation(self, reservation_id):
        if reservation_id:
            with self.store.connection(immediate=True) as connection:
                connection.execute("UPDATE autonomy_reservations SET state = 'SETTLED', settled_at = COALESCE(settled_at, ?) WHERE reservation_id = ?", (_iso(), reservation_id))

    def _autonomy_observed_landing(self, connection, project_id, ticket_id):
        # An operator's outside Git write cannot be prevented by a server budget.
        # Record the truth once, without relabelling it as a gated delivery.
        for row in connection.execute("SELECT * FROM autonomy_runs WHERE project_id = ? AND state != 'COMPLETED'", (project_id,)).fetchall():
            run = _run_row(row)
            if ticket_id not in run["ticket_ids"]:
                continue
            reservation = connection.execute("SELECT * FROM autonomy_reservations WHERE run_id = ? AND unit = 'landed_tickets' AND unit_key = ?", (run["run_id"], ticket_id)).fetchone()
            if reservation:
                connection.execute("UPDATE autonomy_reservations SET state = 'SETTLED', settled_at = COALESCE(settled_at, ?) WHERE reservation_id = ?", (_iso(), reservation["reservation_id"]))
            else:
                connection.execute("INSERT INTO autonomy_reservations (reservation_id, run_id, unit, unit_key, ticket_id, job_ref, state, created_at, settled_at) VALUES (?, ?, 'landed_tickets', ?, ?, ?, 'SETTLED', ?, ?)",
                    (_id(connection, "reservation"), run["run_id"], ticket_id, ticket_id, f"observed:{ticket_id}", _iso(), _iso()))
            if self._autonomy_counts(connection, run["run_id"])["landed_tickets"] >= run["limits"]["landed_tickets"]:
                self._autonomy_drain(connection, run, "landing_budget")

    def _autonomy_execution_guard(self, job_ref):
        with self.store.connection() as connection:
            if self._autonomy_job_run(connection, job_ref) is None:
                return None
            kind, identifier = job_ref.split(":", 1)
            suffix = ""
            if kind in {"qa", "merge"}:
                table, field = {"qa": ("qa_test_jobs", "test_run_id"), "merge": ("merge_jobs", "merge_job_id")}[kind]
                suffix = connection.execute(f"SELECT candidate_id FROM {table} WHERE {field} = ?", (identifier,)).fetchone()[0]
            run_id = self._autonomy_job_run(connection, job_ref)
            prefix = f"{job_ref}:{suffix}:"
            prior = connection.execute("SELECT unit_key, state FROM autonomy_reservations WHERE run_id = ? AND unit = 'suite_starts' AND job_ref = ?", (run_id, job_ref)).fetchall()
            if any(row["state"] == "RESERVED" for row in prior):
                raise AutonomyExecutionPaused("Uncertain prior suite execution requires reconciliation")
            offset = sum(row["unit_key"].startswith(prefix) for row in prior)

        @contextmanager
        def guard(index):
            reservation = self._autonomy_reserve(job_ref, "suite_starts", f"{job_ref}:{suffix}:{offset + index}")
            try:
                yield
            finally:
                self._autonomy_settle_reservation(reservation)
        return guard

    def _autonomy_hold_job(self, job_ref, error):
        kind, identifier = job_ref.split(":", 1)
        table, field = {"qa": ("qa_test_jobs", "test_run_id"), "merge": ("merge_jobs", "merge_job_id"), "main": ("mainline_checks", "check_id")}[kind]
        with self.store.connection(immediate=True) as connection:
            run_id = self._autonomy_job_run(connection, job_ref)
            if not run_id:
                raise error
            self._autonomy_drain(connection, self._autonomy_run(connection, run_id), "unknown_usage")
            job = self._one(connection, f"SELECT * FROM {table} WHERE {field} = ?", (identifier,))
            prior = getattr(error, "prior_execution", None)
            if prior is not None and kind in {"qa", "merge"}:
                actor = Actor(job["requested_by"], Role.QA if kind == "qa" else Role.INTEGRATOR, frozenset({job["project_id"]}))
                evidence_id = _id(connection, "testrun")
                commit = self._one(connection, "SELECT commit_sha FROM candidates WHERE candidate_id = ?", (job["candidate_id"],))["commit_sha"]
                storage_ref, path, checksum = self.project_tests.write_report(candidate_id=job["candidate_id"], test_run_id=evidence_id, execution=prior, candidate_commit=commit)
                connection.execute("""INSERT INTO qa_test_runs
                    (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                     duration_ms, storage_ref, report_path, environment_digest, storage_checksum,
                     executed_by, created_at, termination_reason, provenance_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (evidence_id, job["candidate_id"], job["ticket_id"], prior.command_digest, prior.exit_code,
                     int(prior.timed_out), prior.duration_ms, storage_ref, path, prior.environment_digest,
                     checksum, actor.agent_id, _iso(), prior.termination_reason, prior.provenance_status))
                self._event(connection, actor, job["project_id"], "autonomy.execution_paused", "run", run_id,
                    self._autonomy_run(connection, run_id)["version"], {"job_ref": job_ref, "test_run_id": evidence_id})
            elif prior is not None and kind == "main":
                connection.execute("UPDATE mainline_checks SET termination_reason = ?, output_tail = ? WHERE check_id = ?", (prior.termination_reason, (prior.stdout + prior.stderr)[-3000:], identifier))
            connection.execute(f"UPDATE {table} SET status = 'QUEUED', started_at = NULL, autonomy_hold_run_id = ?, error_message = ? WHERE {field} = ?",
                               (run_id, "autonomy_execution_paused: owner review required", identifier))
            return self._one(connection, f"SELECT * FROM {table} WHERE {field} = ?", (identifier,))
