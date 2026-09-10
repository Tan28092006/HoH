from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP

from hoh_control_plane.domain import Actor, ConflictError, DomainError, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import ContentionError, SQLiteStore


Result = TypeVar("Result")

# ContentionError only ever comes from a transaction that never began (the write
# lock was never acquired), so no partial mutation exists to duplicate; retrying
# the whole call from scratch is safe. store.py already retries lock acquisition
# internally before raising it, so this is a second, coarser bounded layer.
_CONTENTION_RETRY_ATTEMPTS = 3
_CONTENTION_RETRY_BASE_SECONDS = 0.1


def _run_with_contention_retry(operation: Callable[[], Result]) -> Result:
    delay = _CONTENTION_RETRY_BASE_SECONDS
    for attempt in range(_CONTENTION_RETRY_ATTEMPTS):
        try:
            return operation()
        except ContentionError:
            if attempt == _CONTENTION_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover
_TARGET_KEYS = ("project_id", "feature_id", "ticket_id", "feature_ref", "ticket_ref")


def _start_qa_test_worker(database_path: Path, test_run_id: str) -> None:
    """Launch one isolated runner so stdio MCP calls stay available for polling."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.qa_worker",
            "--database",
            str(database_path),
            "--test-run-id",
            test_run_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _start_worktree_provision_worker(database_path: Path, provision_id: str) -> None:
    """Launch Git worktree setup away from the synchronous MCP stdio request."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.worktree_worker",
            "--database",
            str(database_path),
            "--provision-id",
            provision_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _start_candidate_submission_worker(database_path: Path, submission_id: str) -> None:
    """Validate candidate Git identity away from the MCP request."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.candidate_submission_worker",
            "--database",
            str(database_path),
            "--submission-id",
            submission_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _start_merge_worker(database_path: Path, merge_job_id: str) -> None:
    """Fast-forward the protected default branch away from the MCP stdio request."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.integration_worker",
            "--database",
            str(database_path),
            "--merge-job-id",
            merge_job_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def create_mcp_server(service: ControlPlaneService, actor: Actor) -> FastMCP:
    """Create the smallest MCP tool surface permitted for one authenticated role."""
    server = FastMCP(
        "Harness Control Plane",
        instructions=(
            "Discover work first, resolve typed context references, and only invoke "
            "role-authorized write tools. Execution agents register presence, then poll "
            "work.inbox before discovery; a ticket lease remains the exclusive claim. "
            "Every MCP invocation is audit logged with redacted arguments."
        ),
    )

    def invoke(tool_name: str, arguments: dict[str, Any], operation: Callable[[], Result]) -> Result:
        project_id = _project_for_call(service, actor, arguments)
        summary = _safe_input_summary(arguments)
        try:
            result = _run_with_contention_retry(operation)
        except DomainError as error:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="rejected",
                error_code=error.code,
            )
            raise
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as error:
            # Safety net: normalize any raw driver error that reaches this boundary
            # without an existing service-layer translation (e.g. a foreign-key
            # violation this call site has no bespoke message for), so a caller
            # never sees internal storage-engine text through MCP.
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="rejected",
                error_code="database_error",
            )
            raise ConflictError(
                "This change conflicts with the current database state; retry the request."
            ) from error
        except Exception:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="failed",
                error_code="internal_error",
            )
            raise
        service.record_tool_call(
            actor,
            tool_name=tool_name,
            project_id=project_id,
            input_summary=summary,
            outcome="succeeded",
        )
        return result

    @server.tool(name="identity.get_capabilities", structured_output=True)
    def identity_get_capabilities() -> dict[str, Any]:
        """Return the active identity, role, project scope and allowed MCP capabilities."""
        return invoke(
            "identity.get_capabilities",
            {},
            lambda: {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "project_ids": sorted(actor.project_ids),
                "capabilities": service.capabilities(actor),
                "protocol_version": "0.3.0",
            },
        )

    @server.tool(name="agent.register", structured_output=True)
    def agent_register(
        skills: list[str],
        capacity: int = 1,
        machine_id: str | None = None,
        presence_seconds: int = 120,
    ) -> dict[str, Any]:
        """Register this authenticated agent as available for durable work dispatch."""
        arguments = {
            "skills": skills,
            "capacity": capacity,
            "machine_id": machine_id,
            "presence_seconds": presence_seconds,
        }
        return invoke(
            "agent.register",
            arguments,
            lambda: service.register_agent(actor, **arguments),
        )

    @server.tool(name="agent.heartbeat", structured_output=True)
    def agent_heartbeat(presence_seconds: int = 120) -> dict[str, Any]:
        """Renew dispatch availability; this does not renew a ticket work lease."""
        arguments = {"presence_seconds": presence_seconds}
        return invoke(
            "agent.heartbeat",
            arguments,
            lambda: service.heartbeat_agent(actor, **arguments),
        )

    @server.tool(name="work.discover", structured_output=True)
    def work_discover(
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List role-appropriate executable work plus typed context references."""
        arguments = {
            "project_id": project_id,
            "dependency_ready": dependency_ready,
            "blocked_only": blocked_only,
            "limit": limit,
        }
        return invoke(
            "work.discover",
            arguments,
            lambda: service.discover_work(
                actor, project_id, dependency_ready, blocked_only, limit
            ),
        )

    @server.tool(name="feature.get_context", structured_output=True)
    def feature_get_context(feature_ref: str) -> dict[str, Any]:
        """Resolve a typed feature reference into role-aware progress and context."""
        return invoke(
            "feature.get_context",
            {"feature_ref": feature_ref},
            lambda: service.get_feature_context(actor, feature_ref),
        )

    @server.tool(name="ticket.get_context", structured_output=True)
    def ticket_get_context(ticket_ref: str) -> dict[str, Any]:
        """Resolve a typed ticket reference including dependencies, candidate and QA evidence."""
        return invoke(
            "ticket.get_context",
            {"ticket_ref": ticket_ref},
            lambda: service.get_ticket_context(actor, ticket_ref),
        )

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:

        @server.tool(name="work.inbox", structured_output=True)
        def work_inbox(
            project_id: str, limit: int = 20, offer_seconds: int = 90
        ) -> dict[str, Any]:
            """Poll durable role-matched work offers; idle agents receive queued hand-offs here."""
            arguments = {
                "project_id": project_id,
                "limit": limit,
                "offer_seconds": offer_seconds,
            }
            return invoke(
                "work.inbox",
                arguments,
                lambda: service.get_agent_inbox(actor, **arguments),
            )

    if actor.role in {Role.PLANNER, Role.ADMIN}:

        @server.tool(name="planner.configure_repository", structured_output=True)
        def planner_configure_repository(
            project_id: str, repository_path: str
        ) -> dict[str, Any]:
            """Attach a verified local Git repository so Dev candidates require a bound worktree."""
            arguments = {"project_id": project_id, "repository_path": repository_path}
            return invoke(
                "planner.configure_repository",
                arguments,
                lambda: service.configure_repository(actor, **arguments),
            )

        @server.tool(name="planner.configure_test_command", structured_output=True)
        def planner_configure_test_command(
            project_id: str, test_command: list[str], timeout_seconds: int = 600
        ) -> dict[str, Any]:
            """Register the only project test command that future QA candidates may execute."""
            arguments = {
                "project_id": project_id,
                "test_command": test_command,
                "timeout_seconds": timeout_seconds,
            }
            return invoke(
                "planner.configure_test_command",
                arguments,
                lambda: service.configure_test_command(actor, **arguments),
            )

        @server.tool(name="planner.create_project", structured_output=True)
        def planner_create_project(
            project_id: str, name: str, description: str, default_branch: str = "main"
        ) -> dict[str, Any]:
            """Create a project container owned by the Planner's project scope."""
            arguments = {
                "project_id": project_id,
                "name": name,
                "description": description,
                "default_branch": default_branch,
            }
            return invoke(
                "planner.create_project", arguments,
                lambda: service.create_project(actor, **arguments),
            )

        @server.tool(name="planner.create_feature", structured_output=True)
        def planner_create_feature(
            project_id: str,
            feature_id: str,
            title: str,
            objective: str,
            parent_feature_id: str | None = None,
            priority: int = 50,
        ) -> dict[str, Any]:
            """Create a feature or nested subfeature with a stable context reference."""
            arguments = {
                "project_id": project_id,
                "feature_id": feature_id,
                "title": title,
                "objective": objective,
                "parent_feature_id": parent_feature_id,
                "priority": priority,
            }
            return invoke(
                "planner.create_feature", arguments,
                lambda: service.create_feature(actor, **arguments),
            )

        @server.tool(name="planner.create_ticket", structured_output=True)
        def planner_create_ticket(
            feature_id: str,
            ticket_id: str,
            title: str,
            objective: str,
            required_role: str,
            acceptance_criteria: list[str],
            preservation_constraints: list[str],
            file_scope: list[str],
            dependency_ids: list[str] | None = None,
            ticket_type: str = "feature_work",
            priority: int = 50,
            parent_ticket_id: str | None = None,
        ) -> dict[str, Any]:
            """Create a bounded backlog ticket for Developer, QA or Integrator work."""
            arguments = {
                "feature_id": feature_id,
                "ticket_id": ticket_id,
                "title": title,
                "objective": objective,
                "required_role": required_role,
                "acceptance_criteria": acceptance_criteria,
                "preservation_constraints": preservation_constraints,
                "file_scope": file_scope,
                "dependency_ids": dependency_ids or [],
                "ticket_type": ticket_type,
                "priority": priority,
                "parent_ticket_id": parent_ticket_id,
            }
            return invoke(
                "planner.create_ticket",
                arguments,
                lambda: service.create_ticket(
                    actor,
                    feature_id=feature_id,
                    ticket_id=ticket_id,
                    title=title,
                    objective=objective,
                    required_role=_execution_role(required_role),
                    acceptance_criteria=acceptance_criteria,
                    preservation_constraints=preservation_constraints,
                    file_scope=file_scope,
                    dependency_ids=dependency_ids or [],
                    ticket_type=ticket_type,
                    priority=priority,
                    parent_ticket_id=parent_ticket_id,
                ),
            )

        @server.tool(name="planner.set_ticket_ready", structured_output=True)
        def planner_set_ticket_ready(ticket_id: str, expected_version: int) -> dict[str, Any]:
            """Release a planned backlog ticket into the role's executable queue."""
            arguments = {"ticket_id": ticket_id, "expected_version": expected_version}
            return invoke(
                "planner.set_ticket_ready", arguments,
                lambda: service.set_ticket_ready(actor, **arguments),
            )

        @server.tool(name="planner.update_write_scope", structured_output=True)
        def planner_update_write_scope(
            ticket_id: str,
            expected_version: int,
            file_scope: list[str],
            reason: str,
        ) -> dict[str, Any]:
            """Correct a planned or blocked ticket's repository-relative write scope."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "file_scope": file_scope,
                "reason": reason,
            }
            return invoke(
                "planner.update_write_scope",
                arguments,
                lambda: service.update_ticket_write_scope(actor, **arguments),
            )

        @server.tool(name="planner.append_context", structured_output=True)
        def planner_append_context(
            entry_type: str,
            content: str,
            ticket_id: str | None = None,
            feature_id: str | None = None,
            source_ref: str | None = None,
        ) -> dict[str, Any]:
            """Record a planning decision, specification or risk against a feature or ticket."""
            arguments = {
                "entry_type": entry_type,
                "content": content,
                "ticket_id": ticket_id,
                "feature_id": feature_id,
                "source_ref": source_ref,
            }
            return invoke(
                "planner.append_context", arguments,
                lambda: service.append_planner_context(actor, **arguments),
            )

    if actor.role in {Role.DEVELOPER, Role.ADMIN}:

        @server.tool(name="work.claim", structured_output=True)
        def work_claim(
            ticket_id: str, expected_version: int, lease_seconds: int = 900
        ) -> dict[str, Any]:
            """Claim READY or REWORK Developer work through a timed exclusive lease."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
            }
            return invoke("work.claim", arguments, lambda: service.claim_ticket(actor, **arguments))

        @server.tool(name="dev.prepare_worktree", structured_output=True)
        def dev_prepare_worktree(
            ticket_id: str, lease_id: str, expected_version: int
        ) -> dict[str, Any]:
            """Queue ticket-bound Git worktree setup and return a durable provision ID."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
            }

            def queue_and_start() -> dict[str, Any]:
                queued = service.queue_worktree_provision(actor, **arguments)
                provision = queued.get("provision")
                if provision is None or provision["status"] != "QUEUED":
                    return queued
                try:
                    _start_worktree_provision_worker(service.store.path, provision["provision_id"])
                except OSError as error:
                    service.fail_queued_worktree_provision(
                        provision["provision_id"],
                        f"Could not start the isolated worktree worker: {error}",
                    )
                    raise ValidationError("Could not start the isolated worktree worker") from error
                return queued

            return invoke(
                "dev.prepare_worktree",
                arguments,
                queue_and_start,
            )

        @server.tool(name="dev.get_worktree_provision", structured_output=True)
        def dev_get_worktree_provision(
            ticket_id: str,
            lease_id: str,
            provision_id: str,
            lease_seconds: int = 900,
        ) -> dict[str, Any]:
            """Poll a queued worktree provision and renew the Developer lease."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "provision_id": provision_id,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "dev.get_worktree_provision",
                arguments,
                lambda: service.get_worktree_provision(actor, **arguments),
            )

        @server.tool(name="dev.append_progress", structured_output=True)
        def dev_append_progress(ticket_id: str, lease_id: str, message: str) -> dict[str, Any]:
            """Append a durable development checkpoint without exposing content in the UI log."""
            arguments = {"ticket_id": ticket_id, "lease_id": lease_id, "message": message}
            return invoke(
                "dev.append_progress", arguments,
                lambda: service.append_progress(actor, **arguments),
            )

        @server.tool(name="dev.report_blocker", structured_output=True)
        def dev_report_blocker(
            ticket_id: str, lease_id: str, expected_version: int, reason: str
        ) -> dict[str, Any]:
            """Release active development work to BLOCKED with durable blocker context."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
                "reason": reason,
            }
            return invoke(
                "dev.report_blocker", arguments,
                lambda: service.report_blocker(actor, **arguments),
            )

        @server.tool(name="dev.submit_candidate", structured_output=True)
        def dev_submit_candidate(
            ticket_id: str,
            lease_id: str,
            expected_version: int,
            commit_sha: str,
            base_commit: str,
            branch_ref: str,
            developer_claims: list[str],
        ) -> dict[str, Any]:
            """Queue immutable candidate validation and return immediately for polling."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
                "commit_sha": commit_sha,
                "base_commit": base_commit,
                "branch_ref": branch_ref,
                "developer_claims": developer_claims,
            }
            if not service.candidate_submission_requires_worker(actor, ticket_id):
                return invoke(
                    "dev.submit_candidate", arguments,
                    lambda: service.submit_candidate(actor, **arguments),
                )
            queued = invoke(
                "dev.submit_candidate", arguments,
                lambda: service.queue_candidate_submission(actor, **arguments),
            )
            _start_candidate_submission_worker(service.store.path, queued["submission"]["submission_id"])
            return queued

        @server.tool(name="dev.get_candidate_submission", structured_output=True)
        def dev_get_candidate_submission(
            ticket_id: str,
            lease_id: str,
            submission_id: str,
            lease_seconds: int = 900,
        ) -> dict[str, Any]:
            """Poll a queued candidate submission and renew its lease while active."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "submission_id": submission_id,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "dev.get_candidate_submission", arguments,
                lambda: service.get_candidate_submission(actor, **arguments),
            )

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR, Role.ADMIN}:

        @server.tool(name="work.heartbeat", structured_output=True)
        def work_heartbeat(lease_id: str, lease_seconds: int = 900) -> dict[str, Any]:
            """Renew an active Developer or QA lease while useful work is in progress."""
            arguments = {"lease_id": lease_id, "lease_seconds": lease_seconds}
            return invoke(
                "work.heartbeat", arguments, lambda: service.heartbeat(actor, **arguments)
            )

    if actor.role in {Role.INTEGRATOR, Role.ADMIN}:

        @server.tool(name="integrator.claim_candidate", structured_output=True)
        def integrator_claim_candidate(
            ticket_id: str, expected_version: int, lease_seconds: int = 900
        ) -> dict[str, Any]:
            """Claim a QA-verified candidate for the protected integration gate."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "integrator.claim_candidate",
                arguments,
                lambda: service.claim_integration(actor, **arguments),
            )

        @server.tool(name="integrator.merge_candidate", structured_output=True)
        def integrator_merge_candidate(
            ticket_id: str, lease_id: str, expected_version: int
        ) -> dict[str, Any]:
            """Queue a durable merge job and return its merge_job_id before Git runs."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
            }

            def queue_and_start() -> dict[str, Any]:
                queued = service.queue_merge_candidate(actor, **arguments)
                try:
                    _start_merge_worker(service.store.path, queued["merge_job"]["merge_job_id"])
                except OSError as error:
                    service.fail_queued_merge(
                        queued["merge_job"]["merge_job_id"],
                        f"Could not start the isolated integration merge worker: {error}",
                    )
                    raise ValidationError("Could not start the isolated integration merge worker") from error
                return queued

            return invoke(
                "integrator.merge_candidate",
                arguments,
                queue_and_start,
            )

        @server.tool(name="integrator.get_merge_candidate", structured_output=True)
        def integrator_get_merge_candidate(
            ticket_id: str,
            lease_id: str,
            merge_job_id: str,
            lease_seconds: int = 900,
        ) -> dict[str, Any]:
            """Poll a queued merge job; every poll also renews the Integrator ticket lease."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "merge_job_id": merge_job_id,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "integrator.get_merge_candidate",
                arguments,
                lambda: service.get_merge_candidate(actor, **arguments),
            )

    if actor.role in {Role.QA, Role.ADMIN}:

        @server.tool(name="qa.claim_candidate", structured_output=True)
        def qa_claim_candidate(
            ticket_id: str, expected_version: int, lease_seconds: int = 900
        ) -> dict[str, Any]:
            """Claim a frozen candidate for an independent QA pass."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "qa.claim_candidate", arguments,
                lambda: service.claim_qa(actor, **arguments),
            )

        @server.tool(name="qa.run_project_tests", structured_output=True)
        def qa_run_project_tests(ticket_id: str, lease_id: str) -> dict[str, Any]:
            """Queue the frozen candidate test in a detached worker and return its test_run_id."""
            arguments = {"ticket_id": ticket_id, "lease_id": lease_id}

            def queue_and_start() -> dict[str, Any]:
                queued = service.queue_project_tests(actor, **arguments)
                try:
                    _start_qa_test_worker(service.store.path, queued["test_run"]["test_run_id"])
                except OSError as error:
                    service.fail_queued_project_test(
                        queued["test_run"]["test_run_id"],
                        f"Could not start the isolated QA test worker: {error}",
                    )
                    raise ValidationError("Could not start the isolated QA test worker") from error
                return queued

            return invoke(
                "qa.run_project_tests",
                arguments,
                queue_and_start,
            )

        @server.tool(name="qa.get_project_test_run", structured_output=True)
        def qa_get_project_test_run(
            ticket_id: str,
            lease_id: str,
            test_run_id: str,
            lease_seconds: int = 900,
        ) -> dict[str, Any]:
            """Poll one durable QA test run; every poll also renews the QA ticket lease."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "test_run_id": test_run_id,
                "lease_seconds": lease_seconds,
            }
            return invoke(
                "qa.get_project_test_run",
                arguments,
                lambda: service.get_project_test_run(actor, **arguments),
            )

        @server.tool(name="qa.record_test_run", structured_output=True)
        def qa_record_test_run(
            ticket_id: str, lease_id: str, summary: str, evidence_refs: list[str]
        ) -> dict[str, Any]:
            """Record an intermediate QA test run before the final evidence-bearing verdict."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "summary": summary,
                "evidence_refs": evidence_refs,
            }
            return invoke(
                "qa.record_test_run", arguments,
                lambda: service.record_test_run(actor, **arguments),
            )

        @server.tool(name="qa.submit_verdict", structured_output=True)
        def qa_submit_verdict(
            ticket_id: str,
            lease_id: str,
            candidate_id: str,
            expected_version: int,
            verdict: str,
            findings: list[dict[str, Any]],
        ) -> dict[str, Any]:
            """Submit PASS or FAIL using evidence for every finding and acceptance criterion."""
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "candidate_id": candidate_id,
                "expected_version": expected_version,
                "verdict": verdict,
                "findings": findings,
            }
            return invoke(
                "qa.submit_verdict", arguments,
                lambda: service.submit_qa_verdict(actor, **arguments),
            )

    return server


def _execution_role(value: str) -> Role:
    try:
        role = Role(value)
    except ValueError as error:
        raise ValidationError("required_role is invalid") from error
    if role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        raise ValidationError("required_role must be developer, qa or integrator")
    return role


def _safe_input_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep only operation shape and opaque resource references; never persist content."""
    target = {
        key: value
        for key, value in arguments.items()
        if key in _TARGET_KEYS and value not in (None, "")
    }
    summary: dict[str, Any] = {
        "argument_keys": sorted(key for key, value in arguments.items() if value is not None)
    }
    if target:
        summary["target"] = target
    return summary


def _project_for_call(
    service: ControlPlaneService, actor: Actor, arguments: dict[str, Any]
) -> str | None:
    """Resolve the log partition without changing tool success or failure behavior."""
    project_id = arguments.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    try:
        feature_id = arguments.get("feature_id") or arguments.get("feature_ref")
        if isinstance(feature_id, str) and feature_id:
            return service.project_id_for_feature(actor, _strip_reference(feature_id, "feature"))
        ticket_id = arguments.get("ticket_id") or arguments.get("ticket_ref")
        if isinstance(ticket_id, str) and ticket_id:
            return service.project_id_for_ticket(actor, _strip_reference(ticket_id, "ticket"))
    except DomainError:
        return None
    project_ids = sorted(project_id for project_id in actor.project_ids if project_id != "*")
    return project_ids[0] if len(project_ids) == 1 else None


def _strip_reference(reference: str, kind: str) -> str:
    context_prefix = f"ctx:{kind}:"
    if reference.startswith(context_prefix):
        return reference[len(context_prefix) :].rsplit(":v", 1)[0]
    prefix = f"{kind}:"
    return reference[len(prefix) :] if reference.startswith(prefix) else reference


def main() -> None:
    database_path = Path(os.getenv("HOH_DATABASE_PATH", "data/control-plane.db"))
    agent_id = os.getenv("HOH_AGENT_ID")
    role_name = os.getenv("HOH_AGENT_ROLE")
    project_ids = os.getenv("HOH_PROJECT_IDS", "*")
    if not agent_id or not role_name:
        raise SystemExit("HOH_AGENT_ID and HOH_AGENT_ROLE are required")
    try:
        role = Role(role_name)
    except ValueError as exc:
        raise SystemExit(f"Unknown HOH_AGENT_ROLE: {role_name}") from exc
    store = SQLiteStore(database_path)
    store.initialize()
    service = ControlPlaneService(store)
    actor = Actor(agent_id, role, frozenset(item.strip() for item in project_ids.split(",")))
    create_mcp_server(service, actor).run(transport="stdio")


if __name__ == "__main__":
    main()
