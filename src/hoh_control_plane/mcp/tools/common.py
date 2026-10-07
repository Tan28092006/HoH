"""Tools every role shares: identity, presence, discovery, context, lessons, waiting."""
from __future__ import annotations

import anyio
import os
import time
from collections.abc import Callable
from typing import Any
from mcp.server.fastmcp import FastMCP
from hoh_control_plane import contracts
from hoh_control_plane.observability import correlation_scope
from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.mcp.errors import tool_error
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.invoke import _safe_input_summary


def register(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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
        skill_manifest: contracts.SkillManifest | None = None,
        protocol_version: str | None = None,
        model_family: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Register this authenticated agent as available for durable work dispatch.

        model_family (anthropic, openai, google or other) and the optional model
        name say which model runs this session. QA cannot claim a candidate made
        by the same family while the project's checker_distinct_from_maker
        policy is on; an unknown family is allowed but marked family_unverified.

        execution_capabilities lists only observable project equipment this
        session has checked it can use. Strict QA work is offered and claimable
        only when this set and the independent Planner/operator approval both
        cover the ticket validation contract on the same machine.

        skill_manifest is an optional list of {name, version, checksum, loaded_at}
        entries (loaded_at ISO-8601) recording which skill files this agent actually loaded; combined
        with protocol_version, it lets a project's skill policy flag this agent
        as incompatible before it can claim work, without ever granting it any
        role permission it does not already hold.
        """
        # A stdio profile set up with --model carries its family in the
        # environment; an explicit argument always wins.
        if model_family is None:
            model_family = os.environ.get("HOH_AGENT_MODEL_FAMILY") or None
            if model is None:
                model = os.environ.get("HOH_AGENT_MODEL") or None
        arguments = {
            "skills": skills,
            "execution_capabilities": execution_capabilities,
            "capacity": capacity,
            "machine_id": machine_id,
            "presence_seconds": presence_seconds,
            "skill_manifest": contracts.as_payload(skill_manifest),
            "protocol_version": protocol_version,
            "model_family": model_family,
            "model": model,
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
                                      job_kind: contracts.WaitJobKind | None = None,
                                      job_id: str | None = None,
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
            except Exception as raised:
                # The same mapping as every other tool; the finally below audits it.
                error = tool_error(raised, audited=True)
                outcome, error_code = error.outcome, error.payload['code']
                raise error from raised
            finally:
                service.record_tool_call(actor, tool_name='work.wait_for_event',
                    project_id=project_id, input_summary=_safe_input_summary(arguments),
                    outcome=outcome, error_code=error_code)

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        @server.tool(name="work.report_failure", structured_output=True)
        def work_report_failure(
            project_id: str,
            ticket_id: str,
            failure_class: contracts.FailureClass,
            summary: str,
            evidence_refs: list[str] | None = None,
            job_kind: contracts.FailedJobKind | None = None,
            job_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Report one durable execution failure to Planner before becoming idle.

            Use this only after a repeatable or terminal error. Do not report an
            empty inbox, stale context, lease/state conflict or first transient
            transport timeout. Supply a failed worker job when the error came
            from provision, submission, QA execution or merge.

            Example:
                work.report_failure(
                    project_id="p1", ticket_id="T-7", failure_class="infrastructure",
                    summary="Test runner cannot start: port 5432 refused",
                    job_kind="qa_test", job_id="run_12")
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

        @server.tool(name="work.report_usage", structured_output=True)
        def work_report_usage(
            ticket_id: str,
            worker: contracts.WorkerKind,
            tokens_in: contracts.TokenCount | None = None,
            tokens_out: contracts.TokenCount | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Record how you worked a ticket you claimed, once its worker returns.

            worker is fresh_subagent (you passed the worker_brief to a new
            subagent), new_session (a session started for this ticket) or
            dispatcher (this waiting chat did the work itself). Give the worker's
            token counts when your client shows them. Advisory metrics only.
            """
            arguments = {
                "ticket_id": ticket_id,
                "worker": worker,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            }
            return invoke(
                "work.report_usage",
                arguments,
                lambda: service.report_usage(actor, **arguments),
                idempotency_key,
            )

    @server.tool(name="work.discover", structured_output=True)
    def work_discover(
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = 50,
        status: list[contracts.TicketStatusName] | None = None,
        feature_id: str | None = None,
        priority_min: int | None = None,
        awaiting_role: contracts.ExecutionRole | None = None,
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
        known_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one current role-bounded ticket packet.

        Pass the context_snapshot_id you hold as known_snapshot_id: an unchanged
        packet returns only {unchanged: true, context_snapshot_id}.
        Planner/Admin may set include_history only for a deliberate audit of older
        typed decisions. A larger token_budget alone never widens role visibility.
        """
        return invoke(
            "ticket.get_context",
            {
                "ticket_ref": ticket_ref,
                "token_budget": token_budget,
                "include_history": include_history,
                "known_snapshot_id": known_snapshot_id,
            },
            lambda: service.get_ticket_context(
                actor, ticket_ref, token_budget, include_history, known_snapshot_id
            ),
        )

    @server.tool(name="context.resolve", structured_output=True)
    def context_resolve(reference: str) -> dict[str, Any]:
        """Resolve one typed reference from a packet, Planner job, event or tool result.

        Accepts ctx:entry:<id>, ctx:candidate:<id>[:developer_claims|:changed_paths],
        ctx:finding:<id>, ctx:experience:<id>, ctx:test_run:<id>, ctx:merge_job:<id>,
        ctx:planner_job:<id>, ctx:ticket:<id>[:v<n>][:<section>|:history] and
        ctx:feature:<id>[:v<n>][:<section>], and the bare forms the server writes
        (candidate:<id>, context:<id>, finding:<id>, experience:<id>, test_run:<id>,
        merge_job:<id>, ticket:<id>, ...). Returns only that item.

        Example: context.resolve(reference="finding:finding_0007")
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
        """File a lesson against the file scope it was learned in, for whoever works there next.

        ticket_id is the ticket you hold (a ticket:<id> reference is accepted too).

        Example: experience.record(ticket_id="T-7",
            lesson="Reset the payment stub between retry tests", file_scope=["src/checkout/**"])
        """
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




def register_inbox(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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




def register_lease_heartbeat(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR, Role.ADMIN}:

        @server.tool(name="work.heartbeat", structured_output=True)
        def work_heartbeat(lease_id: str, lease_seconds: int = 900) -> dict[str, Any]:
            """Renew an active Developer, QA or Integrator lease while useful work is in progress."""
            arguments = {"lease_id": lease_id, "lease_seconds": lease_seconds}
            return invoke(
                "work.heartbeat", arguments, lambda: service.heartbeat(actor, **arguments)
            )
