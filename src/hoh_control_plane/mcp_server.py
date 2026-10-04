from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import anyio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from hoh_control_plane.auth import (
    STDIO_TRANSPORT,
    TOKENS_VARIABLE,
    AgentCredential,
    credentials_from_environment,
    requested_transport,
    resolve_actor,
)
from hoh_control_plane.observability import correlation_scope
from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    DomainError,
    Role,
    ValidationError,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service._common import require_snapshot_id
from hoh_control_plane.store_factory import store_from_environment
from hoh_control_plane.store import ContentionError


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
_TARGET_KEYS = (
    "project_id", "feature_id", "ticket_id", "feature_ref", "ticket_ref", "note_ref"
)


def _worker_location(database_path: Path | str) -> tuple[str, dict[str, str] | None]:
    location = str(database_path)
    if location.startswith(("postgresql://", "postgres://")):
        environment = os.environ.copy()
        environment["HOH_DATABASE_URL"] = location
        # The store factory selects the server URL from env. Never expose its
        # password in command-line arguments / process listings.
        return "data/control-plane.db", environment
    return location, None


def _supervised_workers() -> bool:
    mode = os.getenv('HOH_WORKER_MODE', 'legacy')
    if mode not in {'legacy', 'supervised'}:
        raise ValidationError('HOH_WORKER_MODE must be legacy or supervised')
    return mode == 'supervised'


def _start_qa_test_worker(database_path: Path, test_run_id: str) -> None:
    """Launch one isolated runner so stdio MCP calls stay available for polling."""
    if _supervised_workers():
        return  # The durable queue is drained by hoh-supervisor.
    database_argument, environment = _worker_location(database_path)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.qa_worker",
            "--database",
            database_argument,
            "--test-run-id",
            test_run_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )


def _start_worktree_provision_worker(database_path: Path, provision_id: str) -> None:
    """Launch Git worktree setup away from the synchronous MCP stdio request."""
    if _supervised_workers():
        return
    database_argument, environment = _worker_location(database_path)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.worktree_worker",
            "--database",
            database_argument,
            "--provision-id",
            provision_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )


def _start_candidate_submission_worker(database_path: Path, submission_id: str) -> None:
    """Validate candidate Git identity away from the MCP request."""
    if _supervised_workers():
        return
    database_argument, environment = _worker_location(database_path)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.candidate_submission_worker",
            "--database",
            database_argument,
            "--submission-id",
            submission_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )


def _start_merge_worker(database_path: Path, merge_job_id: str) -> None:
    """Fast-forward the protected default branch away from the MCP stdio request."""
    if _supervised_workers():
        return
    database_argument, environment = _worker_location(database_path)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hoh_control_plane.integration_worker",
            "--database",
            database_argument,
            "--merge-job-id",
            merge_job_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )


def create_mcp_server(service: ControlPlaneService, actor: Actor, *, stateless_http: bool = False) -> FastMCP:
    """Create the smallest MCP tool surface permitted for one authenticated role."""
    server = FastMCP(
        "Harness Control Plane",
        stateless_http=stateless_http,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
            + [value.strip() for value in os.getenv("HOH_MCP_ALLOWED_HOSTS", "").split(",") if value.strip()],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
            + [value.strip() for value in os.getenv("HOH_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip()],
        ),
        instructions=(
            "Discover work first, resolve typed context references, and only invoke "
            "role-authorized write tools. Register presence; call work.wait_for_event "
            "to wait without model polling. A ticket lease remains the exclusive claim. "
            "Every MCP invocation is audit logged with redacted arguments."
        ),
    )

    def invoke(
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Result],
        idempotency_key: str | None = None,
    ) -> Result:
        project_id = _project_for_call(service, actor, arguments)
        summary = _safe_input_summary(arguments)

        def run() -> Result:
            return service.run_idempotent(
                actor,
                operation=tool_name,
                idempotency_key=idempotency_key,
                payload=arguments,
                run=operation,
                project_id=project_id,
            )

        try:
            with correlation_scope():
                result = _run_with_contention_retry(run)
        except DomainError as error:
            service.record_tool_call(
                actor,
                tool_name=tool_name,
                project_id=project_id,
                input_summary=summary,
                outcome="rejected",
                error_code=error.code,
            )
            # Stable, machine-readable parity with the REST error body: a stdio
            # client branches on the code instead of parsing the prose.
            raise ToolError(f"[{error.code}] {error}") from error
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
            raise ToolError(
                f"[{ConflictError.code}] This change conflicts with the current database "
                "state; retry the request."
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

    @server.tool(name="session.bootstrap", structured_output=True)
    def session_bootstrap(project_id: str | None = None) -> dict[str, Any]:
        """Start/resume without chat history: identity, projects, workspace, own leases and job IDs.

        Read-only. Does not claim work, create a checkout or renew a lease.
        """
        return invoke("session.bootstrap", {"project_id": project_id},
                      lambda: service.session_bootstrap(actor, project_id))

    @server.tool(name="agent.register", structured_output=True)
    def agent_register(
        skills: list[str],
        execution_capabilities: list[str] | None = None,
        capacity: int = 1,
        machine_id: str | None = None,
        presence_seconds: int = 120,
        skill_manifest: list[dict[str, Any]] | None = None,
        protocol_version: str | None = None,
    ) -> dict[str, Any]:
        """Register this authenticated agent as available for durable work dispatch.

        execution_capabilities lists only observable project equipment this
        session has checked it can use. Strict QA work is offered and claimable
        only when this set and the independent Planner/operator approval both
        cover the ticket validation contract on the same machine.

        skill_manifest is an optional list of {name, version, checksum, loaded_at}
        entries recording which skill files this agent actually loaded; combined
        with protocol_version, it lets a project's skill policy flag this agent
        as incompatible before it can claim work, without ever granting it any
        role permission it does not already hold.
        """
        arguments = {
            "skills": skills,
            "execution_capabilities": execution_capabilities,
            "capacity": capacity,
            "machine_id": machine_id,
            "presence_seconds": presence_seconds,
            "skill_manifest": skill_manifest,
            "protocol_version": protocol_version,
        }
        return invoke(
            "agent.register",
            arguments,
            lambda: service.register_agent(actor, **arguments),
        )

    @server.tool(name="skill.get_compatibility", structured_output=True)
    def skill_get_compatibility(project_id: str) -> dict[str, Any]:
        """Read a project's accepted skill/protocol versions and each known agent's status."""
        arguments = {"project_id": project_id}
        return invoke(
            "skill.get_compatibility",
            arguments,
            lambda: service.get_skill_compatibility(actor, **arguments),
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

    if actor.role in {Role.PLANNER, Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        @server.tool(name="work.wait_for_event", structured_output=True)
        async def work_wait_for_event(project_id: str, timeout_seconds: int = 43200,
                                      ack_event_id: str | None = None,
                                      job_kind: str | None = None, job_id: str | None = None,
                                      resume_wait_id: str | None = None) -> dict[str, Any]:
            """Keep this GUI chat waiting without model polling until work or a job result arrives.

            The server holds this call for up to 12 hours and returns the moment an
            event arrives; a shorter timeout_seconds is raised to that hold, so do
            not pass one. While waiting write nothing to the chat. If the result is
            status=timeout, call this again at once, still without chat text.
            Register first. Pass ack_event_id from the previous delivery when handled.
            For a queued job pass kind provision/submission/qa/merge and its ID; the
            server maintains its valid lease while this call is connected; on its
            event call the returned poll_tool once. Otherwise, on return, resolve
            current context and claim through the normal role tools. Events are
            hints, never ownership; acknowledge one once handled or obsolete, or it
            is replayed. Speak only for an event you act on, a superseded wait, an
            error, or a question only the user can answer: ask it and stop rather
            than hide it inside a wait. Stopping/closing the chat ends its wait.
            Client MCP timeout must exceed the hold (generated configs set 86400 s).
            After this same chat reconnects, use event_wait.wait_id from bootstrap
            as resume_wait_id to replace only that old wait. A superseded result
            means this request must stop, without retrying or claiming anything.
            Multiple MCP processes do not prove that multiple chats are working.
            """
            from hoh_control_plane import event_wait
            from hoh_control_plane.event_wait import wait_for_event
            if (
                isinstance(timeout_seconds, (int, float))
                and not isinstance(timeout_seconds, bool)
                and 0 < timeout_seconds < event_wait.MIN_MCP_WAIT_SECONDS
            ):
                # Raised, never lowered: an invalid value still reaches validation.
                timeout_seconds = event_wait.MIN_MCP_WAIT_SECONDS
            arguments = dict(project_id=project_id, timeout_seconds=timeout_seconds,
                             ack_event_id=ack_event_id, job_kind=job_kind, job_id=job_id,
                             resume_wait_id=resume_wait_id)
            context = server.get_context()
            try:
                context.request_context
                has_transport = True
            except ValueError:
                has_transport = False  # Direct in-process invocation in tests.
            started = time.monotonic()
            async def keepalive():
                try:
                    with anyio.fail_after(5):
                        request = context.request_context.request
                        if request is None:
                            await context.session.send_ping()
                        elif await request.is_disconnected():
                            raise ConnectionError('HTTP response stream disconnected')
                        await context.report_progress(time.monotonic() - started, message='Waiting for a HoH event')
                except Exception as error:
                    raise ConnectionError('MCP connection lost while waiting; reconnect and bootstrap') from error
            outcome, error_code = 'failed', 'request_cancelled'
            try:
                with correlation_scope():
                    result = await wait_for_event(service, actor, **arguments,
                                                  keepalive=keepalive if has_transport else None)
                outcome, error_code = 'succeeded', None
                return result
            except ConnectionError as error:
                error_code = 'connection_lost'
                raise ToolError('[connection_lost] Reconnect MCP and resume the same identity via session.bootstrap') from error
            except DomainError as error:
                outcome, error_code = 'rejected', error.code
                raise ToolError(f'[{error.code}] {error}') from error
            finally:
                service.record_tool_call(actor, tool_name='work.wait_for_event',
                    project_id=project_id, input_summary=_safe_input_summary(arguments),
                    outcome=outcome, error_code=error_code)

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        @server.tool(name="work.report_failure", structured_output=True)
        def work_report_failure(
            project_id: str,
            ticket_id: str,
            failure_class: str,
            summary: str,
            evidence_refs: list[str] | None = None,
            job_kind: str | None = None,
            job_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Report one durable execution failure to Planner before becoming idle.

            Use this only after a repeatable or terminal error. Do not report an
            empty inbox, stale context, lease/state conflict or first transient
            transport timeout. Supply a failed worker job when the error came
            from provision, submission, QA execution or merge.
            """
            arguments = {
                "project_id": project_id,
                "ticket_id": ticket_id,
                "failure_class": failure_class,
                "summary": summary,
                "evidence_refs": evidence_refs,
                "job_kind": job_kind,
                "job_id": job_id,
            }
            return invoke(
                "work.report_failure",
                arguments,
                lambda: service.report_execution_failure(actor, **arguments),
                idempotency_key,
            )

    @server.tool(name="work.discover", structured_output=True)
    def work_discover(
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = 50,
        status: list[str] | None = None,
        feature_id: str | None = None,
        priority_min: int | None = None,
        awaiting_role: str | None = None,
        unassigned_only: bool = False,
        cursor: str | None = None,
        detail: bool | None = None,
    ) -> dict[str, Any]:
        """List role-appropriate work as feature metadata plus typed ticket references.

        Returns identity, status and counts only. Resolve ticket.get_context for
        the one ticket you intend to act on rather than expecting bodies here.

        For Planner the broad call returns view="feature_rollup": one row per
        feature with complete ticket_counts and awaiting counts, and no ticket
        rows. Call again with feature_id to get that feature's ticket rows, or
        detail=true for rows across the board. Narrowing by status,
        awaiting_role, priority_min, blocked_only or unassigned_only also
        returns rows.
        """
        arguments = {
            "project_id": project_id,
            "dependency_ready": dependency_ready,
            "blocked_only": blocked_only,
            "limit": limit,
            "status": status,
            "feature_id": feature_id,
            "priority_min": priority_min,
            "awaiting_role": awaiting_role,
            "unassigned_only": unassigned_only,
            "cursor": cursor,
            "detail": detail,
        }
        return invoke(
            "work.discover",
            arguments,
            lambda: service.discover_work(actor, **arguments),
        )

    @server.tool(name="events.list", structured_output=True)
    def events_list(
        project_id: str, after: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """Read what changed since a cursor, collapsed by what it changed.

        Ask this on waking instead of re-reading the board: it answers "what
        moved" rather than "what is", so a quiet interval costs almost nothing.
        Entries carry typed references; resolve those only for what you act on.
        """
        arguments = {"project_id": project_id, "after": after, "limit": limit}
        return invoke(
            "events.list",
            arguments,
            lambda: service.list_event_digest(actor, **arguments),
        )

    @server.tool(name="feature.get_context", structured_output=True)
    def feature_get_context(
        feature_ref: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        """Resolve a typed feature reference into role-aware progress and context."""
        return invoke(
            "feature.get_context",
            {"feature_ref": feature_ref, "token_budget": token_budget},
            lambda: service.get_feature_context(actor, feature_ref, token_budget),
        )

    @server.tool(name="ticket.get_context", structured_output=True)
    def ticket_get_context(
        ticket_ref: str,
        token_budget: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any]:
        """Resolve one current role-bounded ticket packet.

        Planner/Admin may set include_history only for a deliberate audit of older
        typed decisions. A larger token_budget alone never widens role visibility.
        """
        return invoke(
            "ticket.get_context",
            {
                "ticket_ref": ticket_ref,
                "token_budget": token_budget,
                "include_history": include_history,
            },
            lambda: service.get_ticket_context(
                actor, ticket_ref, token_budget, include_history
            ),
        )

    @server.tool(name="context.resolve", structured_output=True)
    def context_resolve(reference: str) -> dict[str, Any]:
        """Resolve one typed reference cited by a packet's omitted_sections.

        Accepts ctx:entry:<id>, ctx:candidate:<id>:developer_claims,
        ctx:candidate:<id>:changed_paths, ctx:finding:<id> and, for Planner or
        Admin, ctx:ticket:<id>:v<n>:history. Returns only that item.
        """
        arguments = {"reference": reference}
        return invoke(
            "context.resolve",
            arguments,
            lambda: service.resolve_context_reference(actor, **arguments),
        )

    @server.tool(name="spec.digest", structured_output=True)
    def spec_digest(
        project_id: str,
        after_revision: int = 0,
        limit: int = 50,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Which specification sections moved since a revision, with line spans to open.

        From revision 0 this is the table of contents; pass max_depth=2 there, as
        a full outline of a large document costs about half of what reading it
        would. From a later revision it is only what changed, so staying current
        costs a cursor rather than a re-read; leave max_depth off for that.
        """
        arguments = {
            "project_id": project_id,
            "after_revision": after_revision,
            "limit": limit,
            "max_depth": max_depth,
        }
        return invoke(
            "spec.digest",
            arguments,
            lambda: service.spec_digest(actor, **arguments),
        )

    @server.tool(name="experience.record", structured_output=True)
    def experience_record(
        ticket_id: str, lesson: str, file_scope: list[str] | None = None
    ) -> dict[str, Any]:
        """File a lesson against the file scope it was learned in, for whoever works there next."""
        arguments = {"ticket_ref": ticket_id, "lesson": lesson, "file_scope": file_scope}
        return invoke(
            "experience.record",
            arguments,
            lambda: service.record_experience(actor, **arguments),
        )

    @server.tool(name="experience.get", structured_output=True)
    def experience_get(note_ref: str) -> dict[str, Any]:
        """Resolve one file-scoped advisory lesson surfaced in a ticket packet."""
        arguments = {"note_ref": note_ref}
        return invoke(
            "experience.get",
            arguments,
            lambda: service.get_experience(actor, **arguments),
        )

    if actor.role in {Role.PLANNER, Role.ADMIN}:

        @server.tool(name="experience.list", structured_output=True)
        def experience_list(project_id: str, limit: int = 5) -> dict[str, Any]:
            """Audit project lessons, most reinforced first."""
            arguments = {"project_id": project_id, "limit": limit}
            return invoke(
                "experience.list",
                arguments,
                lambda: {"notes": service.list_experience(actor, **arguments)},
            )

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:

        @server.tool(name="work.inbox", structured_output=True)
        def work_inbox(
            project_id: str, limit: int = 20, offer_seconds: int = 90
        ) -> dict[str, Any]:
            """Read durable role-matched offers and any QA capability blockers.

            QA must inspect capability_blockers before concluding that no candidate
            exists. A blocker names the queued ticket plus missing declarations or
            independently approved capabilities; it is not permission to weaken the
            validation contract or self-approve equipment.
            """
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
        async def planner_configure_repository(
            project_id: str, repository_path: str, workspace_model: str = "colocated"
        ) -> dict[str, Any]:
            """Attach a verified local Git repository so Dev candidates require a bound worktree.

            workspace_model is "colocated" when agents run beside the service and use
            service-provisioned worktrees, or "agent_workspace" when each Developer
            holds its own checkout and the service provisions none.
            """
            arguments = {
                "project_id": project_id,
                "repository_path": repository_path,
                "workspace_model": workspace_model,
            }
            return await anyio.to_thread.run_sync(lambda: invoke(
                'planner.configure_repository', arguments,
                lambda: service.configure_repository(actor, **arguments)))

        @server.tool(name="planner.sync_repository", structured_output=True)
        async def planner_sync_repository(project_id: str) -> dict[str, Any]:
            """Fetch upstream and report bounded commit/path changes without pulling or merging."""
            arguments = {"project_id": project_id}
            return await anyio.to_thread.run_sync(lambda: invoke(
                "planner.sync_repository", arguments,
                lambda: service.sync_repository(actor, project_id)))

        @server.tool(name="planner.list_repository_reconciliations", structured_output=True)
        def planner_list_repository_reconciliations(
            project_id: str, status: str = "PENDING", limit: int = 20
        ) -> dict[str, Any]:
            """List commit observations whose ticket plans still require review."""
            arguments = {"project_id": project_id, "status": status, "limit": limit}
            return invoke(
                "planner.list_repository_reconciliations",
                arguments,
                lambda: service.list_repository_reconciliations(actor, **arguments),
            )

        @server.tool(name="planner.resolve_repository_reconciliation", structured_output=True)
        def planner_resolve_repository_reconciliation(
            project_id: str,
            reconciliation_id: str,
            ticket_decisions: list[dict[str, Any]],
            decision_summary: str,
            new_ticket_ids: list[str] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Reopen execution after classifying every ticket open before a Git change.

            Apply ticket mutations first with cancel/supersede/update and the
            existing integration landing workflow.  The decision list is then
            checked against the actual board before the barrier can resolve.
            """
            arguments = {
                "project_id": project_id,
                "reconciliation_id": reconciliation_id,
                "ticket_decisions": ticket_decisions,
                "decision_summary": decision_summary,
                "new_ticket_ids": new_ticket_ids or [],
            }
            return invoke(
                "planner.resolve_repository_reconciliation",
                arguments,
                lambda: service.resolve_repository_reconciliation(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.record_external_landing", structured_output=True)
        def planner_record_external_landing(
            ticket_id: str,
            expected_version: int,
            landed_commit: str,
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Record Git-proven default-branch work during a pending reconciliation.

            This closes stale duplicate execution but certifies no QA criterion.
            The service verifies commit reachability and writes an ungated landing.
            """
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "landed_commit": landed_commit,
                "reason": reason,
            }
            return invoke(
                "planner.record_external_landing",
                arguments,
                lambda: service.record_external_landing(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.reconcile_delivery_report", structured_output=True)
        def planner_reconcile_delivery_report(merge_job_id: str) -> dict[str, Any]:
            """Recover exactly one missing Planner report for a completed merge."""
            arguments = {"merge_job_id": merge_job_id}
            return invoke(
                "planner.reconcile_delivery_report", arguments,
                lambda: service.reconcile_delivery_report(actor, merge_job_id),
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

        @server.tool(name="planner.configure_skill_policy", structured_output=True)
        def planner_configure_skill_policy(
            project_id: str,
            accepted_protocol_versions: list[str],
            accepted_skill_versions: dict[str, list[str]],
        ) -> dict[str, Any]:
            """Declare which agent protocol and skill versions this project currently accepts."""
            arguments = {
                "project_id": project_id,
                "accepted_protocol_versions": accepted_protocol_versions,
                "accepted_skill_versions": accepted_skill_versions,
            }
            return invoke(
                "planner.configure_skill_policy",
                arguments,
                lambda: service.configure_skill_policy(actor, **arguments),
            )

        @server.tool(name="planner.configure_verification_profile", structured_output=True)
        def planner_configure_verification_profile(
            project_id: str,
            capabilities: list[str],
            quality_policy: str = "claim-evidence-v1",
        ) -> dict[str, Any]:
            """Declare the project's portable QA equipment and evidence policy."""
            arguments = {
                "project_id": project_id,
                "capabilities": capabilities,
                "quality_policy": quality_policy,
            }
            return invoke(
                "planner.configure_verification_profile", arguments,
                lambda: service.configure_verification_profile(actor, **arguments),
            )

        @server.tool(name="planner.configure_agent_capability_profile", structured_output=True)
        def planner_configure_agent_capability_profile(
            project_id: str,
            agent_id: str,
            capabilities: list[str],
            evidence_refs: list[str],
        ) -> dict[str, Any]:
            """Approve a QA agent's equipment from host preflight records only.

            evidence_refs must be preflight:<id> records taken for this agent on
            its registered machine, and capabilities must be both declared by the
            agent and proven by those records; anything else is refused.
            Approving closes the capability review job it answers.
            """
            arguments = {
                "project_id": project_id,
                "agent_id": agent_id,
                "capabilities": capabilities,
                "evidence_refs": evidence_refs,
            }
            return invoke(
                "planner.configure_agent_capability_profile", arguments,
                lambda: service.configure_agent_capability_profile(actor, **arguments),
            )

        @server.tool(name="planner.configure_capability_preflight", structured_output=True)
        def planner_configure_capability_preflight(
            project_id: str, capability: str, command: list[str], reason: str,
        ) -> dict[str, Any]:
            """Set the host preflight command for a capability no built-in probe covers.

            The command runs on the HoH host in the repository; exit status zero
            proves the capability. An empty command removes it. Built-in probes
            cover source_inspection, git_worktree, dependency_manifest,
            build_execution, browser_interaction and screenshot.
            """
            arguments = {"project_id": project_id, "capability": capability,
                         "command": command, "reason": reason}
            return invoke(
                "planner.configure_capability_preflight", arguments,
                lambda: service.configure_capability_preflight(actor, **arguments),
            )

        @server.tool(name="planner.create_project", structured_output=True)
        def planner_create_project(
            project_id: str,
            name: str,
            description: str,
            default_branch: str = "main",
            idempotency_key: str | None = None,
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
                idempotency_key,
            )

        @server.tool(name="planner.create_feature", structured_output=True)
        def planner_create_feature(
            project_id: str,
            feature_id: str,
            title: str,
            objective: str,
            parent_feature_id: str | None = None,
            priority: int = 50,
            idempotency_key: str | None = None,
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
                idempotency_key,
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
            validation_contract: list[dict[str, Any]] | None = None,
            idempotency_key: str | None = None,
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
                "validation_contract": validation_contract,
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
                    validation_contract=validation_contract,
                ),
                idempotency_key,
            )

        @server.tool(name="planner.set_ticket_ready", structured_output=True)
        def planner_set_ticket_ready(
            ticket_id: str, expected_version: int, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            """Release a planned backlog ticket into the role's executable queue."""
            arguments = {"ticket_id": ticket_id, "expected_version": expected_version}
            return invoke(
                "planner.set_ticket_ready", arguments,
                lambda: service.set_ticket_ready(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.configure_validation_contract", structured_output=True)
        def planner_configure_validation_contract(
            ticket_id: str,
            expected_version: int,
            validation_contract: list[dict[str, Any]],
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Bind every criterion to a scenario, observation and required evidence."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "validation_contract": validation_contract,
                "reason": reason,
            }
            return invoke(
                "planner.configure_validation_contract", arguments,
                lambda: service.configure_validation_contract(actor, **arguments),
                idempotency_key,
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

        @server.tool(name="planner.list_interventions", structured_output=True)
        def planner_list_interventions(
            project_id: str, include_acknowledged: bool = False
        ) -> dict[str, Any]:
            """Read pending and delivered intervention requests waiting on a Planner."""
            arguments = {
                "project_id": project_id,
                "include_acknowledged": include_acknowledged,
            }
            return invoke(
                "planner.list_interventions",
                arguments,
                lambda: {
                    "interventions": service.list_planner_interventions(actor, **arguments)
                },
            )

        @server.tool(name="planner.acknowledge_intervention", structured_output=True)
        def planner_acknowledge_intervention(
            intervention_id: str, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            """Confirm a wakeup was received. This is not a planning decision."""
            arguments = {"intervention_id": intervention_id}
            return invoke(
                "planner.acknowledge_intervention",
                arguments,
                lambda: {
                    "intervention": service.acknowledge_planner_intervention(actor, **arguments)
                },
                idempotency_key,
            )

        @server.tool(name="planner.claim_job", structured_output=True)
        def planner_claim_job(
            planner_job_id: str,
            lease_seconds: int = 900,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Take the single exclusive, timed lease on one queued planning decision."""
            arguments = {"planner_job_id": planner_job_id, "lease_seconds": lease_seconds}
            return invoke(
                "planner.claim_job",
                arguments,
                lambda: service.claim_planner_job(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.resolve_job", structured_output=True)
        def planner_resolve_job(
            planner_job_id: str,
            lease_id: str,
            resolution: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Close a planning decision and release its exclusive lease."""
            arguments = {
                "planner_job_id": planner_job_id,
                "lease_id": lease_id,
                "resolution": resolution,
            }
            return invoke(
                "planner.resolve_job",
                arguments,
                lambda: service.resolve_planner_job(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.cancel_ticket", structured_output=True)
        def planner_cancel_ticket(
            ticket_id: str,
            expected_version: int,
            reason: str,
            reconciliation_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Retire an obsolete backlog, ready or blocked ticket from dispatch."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
                "reconciliation_id": reconciliation_id,
            }
            return invoke(
                "planner.cancel_ticket",
                arguments,
                lambda: service.cancel_ticket(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.supersede_ticket", structured_output=True)
        def planner_supersede_ticket(
            ticket_id: str,
            expected_version: int,
            replacement_ticket_id: str,
            reason: str,
            reconciliation_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Retire a ticket that an existing same-project replacement now carries."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "replacement_ticket_id": replacement_ticket_id,
                "reason": reason,
                "reconciliation_id": reconciliation_id,
            }
            return invoke(
                "planner.supersede_ticket",
                arguments,
                lambda: service.supersede_ticket(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.stop_ticket_for_reconciliation", structured_output=True)
        def planner_stop_ticket_for_reconciliation(
            ticket_id: str,
            expected_version: int,
            reconciliation_id: str,
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Revoke stale active execution while preserving its worktree and artifacts."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reconciliation_id": reconciliation_id,
                "reason": reason,
            }
            return invoke(
                "planner.stop_ticket_for_reconciliation",
                arguments,
                lambda: service.stop_ticket_for_reconciliation(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.resume_ticket_after_failure", structured_output=True)
        def planner_resume_ticket_after_failure(
            planner_job_id: str,
            lease_id: str,
            expected_version: int,
            resume_role: str,
            resolution: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Resolve one failure review and requeue its ticket to the chosen role."""
            arguments = {
                "planner_job_id": planner_job_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
                "resume_role": resume_role,
                "resolution": resolution,
            }
            return invoke(
                "planner.resume_ticket_after_failure",
                arguments,
                lambda: service.resume_ticket_after_failure(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.retire_ticket_for_reconciliation", structured_output=True)
        def planner_retire_ticket_for_reconciliation(
            ticket_id: str,
            expected_version: int,
            reconciliation_id: str,
            reason: str,
            target_status: str = "CANCELLED",
            replacement_ticket_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Retire obsolete work from any phase while preserving its artifacts.

            Running non-merge workers lose authority immediately. A merge that may
            have touched Git is reconciled first and returns a durable request to poll.
            """
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reconciliation_id": reconciliation_id,
                "reason": reason,
                "target_status": target_status,
                "replacement_ticket_id": replacement_ticket_id,
            }
            return invoke(
                "planner.retire_ticket_for_reconciliation",
                arguments,
                lambda: service.retire_ticket_for_reconciliation(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.get_ticket_retirement", structured_output=True)
        def planner_get_ticket_retirement(request_id: str) -> dict[str, Any]:
            """Poll an in-flight retirement whose merge side effect needed reconciliation."""
            arguments = {"request_id": request_id}
            return invoke(
                "planner.get_ticket_retirement",
                arguments,
                lambda: service.get_ticket_retirement(actor, request_id),
            )

        @server.tool(name="planner.update_ticket", structured_output=True)
        def planner_update_ticket(
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
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Update bounded planning fields on an eligible backlog, ready or blocked ticket."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
                "title": title,
                "objective": objective,
                "priority": priority,
                "acceptance_criteria": acceptance_criteria,
                "validation_contract": validation_contract,
                "preservation_constraints": preservation_constraints,
                "dependency_ids": dependency_ids,
            }
            return invoke(
                "planner.update_ticket",
                arguments,
                lambda: service.update_ticket_fields(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.split_ticket", structured_output=True)
        def planner_split_ticket(
            ticket_id: str,
            expected_version: int,
            replacement_ticket_ids: list[str],
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Retire a ticket whose scope is now carried by two or more narrower tickets."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "replacement_ticket_ids": replacement_ticket_ids,
                "reason": reason,
            }
            return invoke(
                "planner.split_ticket",
                arguments,
                lambda: service.split_ticket(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.recover_candidate_for_qa", structured_output=True)
        def planner_recover_candidate_for_qa(
            ticket_id: str,
            expected_version: int,
            evidence_reference: str,
            readiness_reference: str | None = None,
        ) -> dict[str, Any]:
            """Requeue a frozen candidate for QA after an infrastructure-only rejection."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "evidence_reference": evidence_reference,
                "readiness_reference": readiness_reference,
            }
            return invoke(
                "planner.recover_candidate_for_qa",
                arguments,
                lambda: service.recover_candidate_for_qa(actor, **arguments),
            )

        @server.tool(name="planner.register_requirement", structured_output=True)
        def planner_register_requirement(
            project_id: str, requirement_id: str, statement: str, source_ref: str
        ) -> dict[str, Any]:
            """Record something the specification asks for, so coverage can be computed."""
            arguments = {
                "project_id": project_id,
                "requirement_id": requirement_id,
                "statement": statement,
                "source_ref": source_ref,
            }
            return invoke(
                "planner.register_requirement",
                arguments,
                lambda: service.register_requirement(actor, **arguments),
            )

        @server.tool(name="planner.link_requirement", structured_output=True)
        def planner_link_requirement(
            requirement_id: str,
            ticket_id: str | None = None,
            feature_id: str | None = None,
        ) -> dict[str, Any]:
            """Point a ticket or feature at the requirement it serves."""
            arguments = {
                "requirement_id": requirement_id,
                "ticket_id": ticket_id,
                "feature_id": feature_id,
            }
            return invoke(
                "planner.link_requirement",
                arguments,
                lambda: service.link_requirement(actor, **arguments),
            )

        @server.tool(name="planner.requirement_coverage", structured_output=True)
        def planner_requirement_coverage(project_id: str) -> dict[str, Any]:
            """List what nothing points at, what is in flight, and what is finished.

            Read this instead of re-reading the specification: it answers what has
            been missed from the board's own state.
            """
            arguments = {"project_id": project_id}
            return invoke(
                "planner.requirement_coverage",
                arguments,
                lambda: service.requirement_coverage(actor, **arguments),
            )

        @server.tool(name="spec.sync", structured_output=True)
        def spec_sync(project_id: str, document: str, text: str) -> dict[str, Any]:
            """Re-index the specification, reporting which sections were added, changed or removed."""
            arguments = {"project_id": project_id, "document": document, "text": text}
            return invoke(
                "spec.sync",
                arguments,
                lambda: service.sync_spec(actor, **arguments),
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

        @server.tool(name="planner.requeue_candidate", structured_output=True)
        def planner_requeue_candidate(
            ticket_id: str,
            expected_version: int,
            reason: str = "base_drift",
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Requeue a stale QA-ready candidate whose base commit has drifted from main."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
            }
            return invoke(
                "planner.requeue_candidate",
                arguments,
                lambda: service.requeue_candidate(actor, **arguments),
                idempotency_key,
            )

    if actor.role in {Role.DEVELOPER, Role.ADMIN}:

        @server.tool(name="work.claim", structured_output=True)
        def work_claim(
            ticket_id: str,
            expected_version: int,
            lease_seconds: int = 900,
            idempotency_key: str | None = None,
            context_snapshot_id: str | None = None,
        ) -> dict[str, Any]:
            """Claim READY or REWORK Developer work through a timed exclusive lease."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
                "context_snapshot_id": context_snapshot_id,
            }
            return invoke(
                "work.claim",
                arguments,
                lambda: service.claim_ticket(actor, **arguments),
                idempotency_key,
            )

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
            context_snapshot_id: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Queue immutable candidate validation and return immediately for polling.

            context_snapshot_id must come from a ticket.get_context read made after
            the claim; a submission decided from stale context is refused.
            """
            context_snapshot_id = _require_snapshot(context_snapshot_id, "dev.submit_candidate")
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
                "commit_sha": commit_sha,
                "base_commit": base_commit,
                "branch_ref": branch_ref,
                "developer_claims": developer_claims,
                "context_snapshot_id": context_snapshot_id,
            }
            if not service.candidate_submission_requires_worker(actor, ticket_id):
                return invoke(
                    "dev.submit_candidate", arguments,
                    lambda: service.submit_candidate(actor, **arguments),
                    idempotency_key,
                )
            queued = invoke(
                "dev.submit_candidate", arguments,
                lambda: service.queue_candidate_submission(actor, **arguments),
                idempotency_key,
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
            ticket_id: str,
            expected_version: int,
            lease_seconds: int = 900,
            idempotency_key: str | None = None,
            context_snapshot_id: str | None = None,
        ) -> dict[str, Any]:
            """Claim a QA-verified candidate for the protected integration gate."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
                "context_snapshot_id": context_snapshot_id,
            }
            return invoke(
                "integrator.claim_candidate",
                arguments,
                lambda: service.claim_integration(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="integrator.merge_candidate", structured_output=True)
        def integrator_merge_candidate(
            ticket_id: str,
            lease_id: str,
            expected_version: int,
            context_snapshot_id: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Queue a durable merge job and return its merge_job_id before Git runs.

            context_snapshot_id must come from a ticket.get_context read made after
            the Integrator claim; a merge decided from stale context is refused.
            """
            context_snapshot_id = _require_snapshot(context_snapshot_id, "integrator.merge_candidate")
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "expected_version": expected_version,
                "context_snapshot_id": context_snapshot_id,
            }

            def queue_and_start() -> dict[str, Any]:
                queued = service.queue_merge_candidate(actor, **arguments)
                if queued["reused"]:
                    # An identical retry (no idempotency_key, or a replay after the
                    # caller could not tell whether the first request landed) must
                    # not spawn a second OS process for the same durable job.
                    return queued
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
                idempotency_key,
            )

        @server.tool(name="integrator.record_external_landing", structured_output=True)
        def integrator_record_external_landing(
            ticket_id: str,
            expected_version: int,
            landed_commit: str,
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Record that a ticket's work is already on the default branch.

            For work that landed outside the gate: a hotfix, an emergency merge, a
            revert applied by hand. It records history and certifies nothing. No
            candidate becomes verified and no acceptance criterion becomes
            satisfied; the service checks for itself that the commit is really on
            the branch before recording anything.
            """
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "landed_commit": landed_commit,
                "reason": reason,
            }
            return invoke(
                "integrator.record_external_landing",
                arguments,
                lambda: service.record_external_landing(actor, **arguments),
                idempotency_key,
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

    if actor.role is Role.QA:

        @server.tool(name="qa.run_capability_preflight", structured_output=True)
        async def qa_run_capability_preflight(project_id: str) -> dict[str, Any]:
            """Have the HoH host observe this machine's declared QA equipment.

            Needs no claim or lease. Returns a preflight_ref and what it proved;
            for a QA-ready candidate blocked only on approval, HoH opens one
            Planner job naming the exact approval call. Run it once, then wait.
            """
            arguments = {"project_id": project_id}
            return await anyio.to_thread.run_sync(lambda: invoke(
                "qa.run_capability_preflight", arguments,
                lambda: service.run_capability_preflight(actor, **arguments)))

    if actor.role in {Role.QA, Role.ADMIN}:

        @server.tool(name="qa.claim_candidate", structured_output=True)
        def qa_claim_candidate(
            ticket_id: str,
            expected_version: int,
            lease_seconds: int = 900,
            idempotency_key: str | None = None,
            context_snapshot_id: str | None = None,
        ) -> dict[str, Any]:
            """Claim a frozen candidate for an independent QA pass."""
            arguments = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "lease_seconds": lease_seconds,
                "context_snapshot_id": context_snapshot_id,
            }
            return invoke(
                "qa.claim_candidate", arguments,
                lambda: service.claim_qa(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="qa.run_project_tests", structured_output=True)
        def qa_run_project_tests(
            ticket_id: str, lease_id: str, rerun: bool = False
        ) -> dict[str, Any]:
            """Queue the frozen candidate test in a detached worker and return its test_run_id.

            If this candidate already has a completed passing run of the same frozen
            command, that run is returned with reused=true and nothing executes; poll
            it as usual. Pass rerun=true only to investigate a suspected flaky result.
            """
            arguments = {"ticket_id": ticket_id, "lease_id": lease_id, "rerun": rerun}

            def queue_and_start() -> dict[str, Any]:
                queued = service.queue_project_tests(actor, **arguments)
                if queued.get("reused"):
                    return queued
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
            context_snapshot_id: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Submit verdict "passed" or "failed" with evidence for every finding and acceptance criterion.

            context_snapshot_id must come from a ticket.get_context read made after
            the QA claim; a verdict decided from stale context is refused.
            """
            context_snapshot_id = _require_snapshot(context_snapshot_id, "qa.submit_verdict")
            arguments = {
                "ticket_id": ticket_id,
                "lease_id": lease_id,
                "candidate_id": candidate_id,
                "expected_version": expected_version,
                "verdict": verdict,
                "findings": findings,
                "context_snapshot_id": context_snapshot_id,
            }
            return invoke(
                "qa.submit_verdict", arguments,
                lambda: service.submit_qa_verdict(actor, **arguments),
                idempotency_key,
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


def _require_snapshot(context_snapshot_id: Any, tool_name: str) -> str:
    """Fail fast at the transport; the service enforces the same rule."""
    return require_snapshot_id(context_snapshot_id, tool_name)


def _strip_reference(reference: str, kind: str) -> str:
    context_prefix = f"ctx:{kind}:"
    if reference.startswith(context_prefix):
        return reference[len(context_prefix) :].rsplit(":v", 1)[0]
    prefix = f"{kind}:"
    return reference[len(prefix) :] if reference.startswith(prefix) else reference


def create_mcp_http_app(
    service: ControlPlaneService,
    credentials: tuple[AgentCredential, ...],
    transport: str = "streamable-http",
    credentials_provider: Callable[[], tuple[AgentCredential, ...]] | None = None,
) -> Any:
    """Serve one authenticated MCP endpoint per configured credential.

    Each credential gets its own server instance bound to its own identity, so a
    role's tools are physically absent from the surface it reaches rather than
    merely refused when called. Authentication happens before dispatch, so an
    unauthenticated request never reaches a tool at all.
    """
    if not credentials:
        raise ValidationError(
            f"A network transport requires agent credentials in {TOKENS_VARIABLE}"
        )
    per_actor = {}
    group = None
    creation_lock = anyio.Lock()

    async def serve_child(child, *, task_status=anyio.TASK_STATUS_IGNORED):
        # Each lifespan is entered and exited by the same task (AnyIO scopes).
        async with child.router.lifespan_context(child):
            task_status.started()
            await anyio.sleep_forever()

    @asynccontextmanager
    async def lifespan(_app: Any):
        nonlocal group
        async with anyio.create_task_group() as running:
            group = running
            try:
                yield
            finally:
                running.cancel_scope.cancel()
                group = None
                per_actor.clear()

    from starlette.applications import Starlette

    lifecycle = Starlette(lifespan=lifespan)

    async def refuse(scope: Any, receive: Any, send: Any, detail: str) -> None:
        body = json.dumps({"error": {"code": "unauthorized", "message": detail}})
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body.encode("utf-8")})

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await lifecycle(scope, receive, send)
            return
        if scope["type"] != "http":
            raise ValueError("Only HTTP scopes are served by this endpoint")
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1") or None
        try:
            current = credentials_provider() if credentials_provider else credentials
            actor = resolve_actor(current, authorization)
        except (AuthorizationError, ValueError, OSError, ValidationError):
            # The reason is deliberately coarse: which of missing or wrong is not
            # information an unauthenticated caller has earned.
            await refuse(scope, receive, send, 'A valid bearer credential is required')
            return
        async with creation_lock:
            if actor not in per_actor:
                if group is None:
                    raise RuntimeError('MCP HTTP lifespan has not started')
                server = create_mcp_server(service, actor, stateless_http=transport == 'streamable-http')
                child = server.sse_app() if transport == 'sse' else server.streamable_http_app()
                await group.start(serve_child, child)
                per_actor[actor] = child
        await per_actor[actor](scope, receive, send)

    return app


def main() -> None:
    store = store_from_environment()
    service = ControlPlaneService(store)
    transport = requested_transport()

    if transport != STDIO_TRANSPORT:
        credentials = credentials_from_environment()
        if not credentials:
            raise SystemExit(
                f"{TOKENS_VARIABLE} is required to serve MCP on a network transport; "
                "the environment is not trusted to grant a role off stdio"
            )
        import uvicorn

        provider = None
        if os.getenv('HOH_LOCAL_CREDENTIALS_PATH'):
            from hoh_control_plane.local_credentials import credential_provider
            provider = credential_provider(credentials, os.environ['HOH_LOCAL_CREDENTIALS_PATH'])

        uvicorn.run(
            create_mcp_http_app(service, credentials, transport, provider),
            host=os.getenv("HOH_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("HOH_MCP_PORT", "8931")),
            log_level="warning",
        )
        return

    agent_id = os.getenv("HOH_AGENT_ID")
    role_name = os.getenv("HOH_AGENT_ROLE")
    project_ids = os.getenv("HOH_PROJECT_IDS", "*")
    if not agent_id or not role_name:
        raise SystemExit("HOH_AGENT_ID and HOH_AGENT_ROLE are required")
    try:
        role = Role(role_name)
    except ValueError as exc:
        raise SystemExit(f"Unknown HOH_AGENT_ROLE: {role_name}") from exc
    actor = Actor(agent_id, role, frozenset(item.strip() for item in project_ids.split(",")))
    create_mcp_server(service, actor).run(transport=STDIO_TRANSPORT)


if __name__ == "__main__":
    main()
