"""Planner and admin tools."""
from __future__ import annotations

import anyio
from collections.abc import Callable
from typing import Any
from mcp.server.fastmcp import FastMCP
from hoh_control_plane import contracts
from hoh_control_plane.contracts import TicketDecisions, ValidationContract, as_payload
from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService


def _execution_role(value: str) -> Role:
    try:
        role = Role(value)
    except ValueError as error:
        raise ValidationError("required_role is invalid") from error
    if role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        raise ValidationError("required_role must be developer, qa or integrator")
    return role


def register_lesson_audit(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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

        @server.tool(name="experience.retire", structured_output=True)
        def experience_retire(note_ref: str, reason: str) -> dict[str, Any]:
            """Stop a stale or wrong lesson from surfacing; the note is kept for audit."""
            arguments = {"note_ref": note_ref, "reason": reason}
            return invoke(
                "experience.retire",
                arguments,
                lambda: service.retire_experience(actor, **arguments),
            )

        @server.tool(name="planner.cycle_metrics", structured_output=True)
        def planner_cycle_metrics(project_id: str, limit: int = 20) -> dict[str, Any]:
            """Read-only loop metrics as p50/p90: wait vs service per phase of the last
            delivered tickets, barrier-open time, QA/merge job queue age, suite runs
            per landed ticket and the merge barrier shadow agreement."""
            arguments = {"project_id": project_id, "limit": limit}
            return invoke(
                "planner.cycle_metrics",
                arguments,
                lambda: service.cycle_metrics(actor, **arguments),
            )




def register(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
    if actor.role in {Role.PLANNER, Role.ADMIN}:

        @server.tool(name="planner.configure_repository", structured_output=True)
        async def planner_configure_repository(
            project_id: str, repository_path: str,
            workspace_model: contracts.WorkspaceModelName = "colocated",
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
            project_id: str, status: contracts.ReconciliationStatus = "PENDING", limit: int = 20
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
            ticket_decisions: TicketDecisions,
            decision_summary: str,
            new_ticket_ids: list[str] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Reopen execution after classifying every ticket open before a Git change.

            Apply ticket mutations first with cancel/supersede/update and the
            existing integration landing workflow.  The decision list is then
            checked against the actual board before the barrier can resolve.
            Give exactly one decision per ticket in the observation's
            ticket_versions; disposition is keep, revise, cancel, supersede,
            complete or rework, and supersede also names replacement_ticket_id.

            Example:
                planner.resolve_repository_reconciliation(
                    project_id="p1", reconciliation_id="recon_12",
                    ticket_decisions=[
                        {"ticket_id": "T-7", "disposition": "keep",
                         "reason": "Commit touched docs only"},
                        {"ticket_id": "T-9", "disposition": "supersede",
                         "reason": "Split into T-11", "replacement_ticket_id": "T-11"}],
                    decision_summary="Docs commit; T-9 replaced by T-11",
                    new_ticket_ids=["T-11"])
            """
            arguments = {
                "project_id": project_id,
                "reconciliation_id": reconciliation_id,
                "ticket_decisions": as_payload(ticket_decisions),
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

        @server.tool(name="planner.close_stuck_merge_job", structured_output=True)
        def planner_close_stuck_merge_job(
            merge_job_id: str,
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """End a merge job that outlived the worker timeout and recovery cannot settle.

            Recovery runs once more first. The job ends FAILED with your reason and
            the Git facts; its intent is kept and the ticket is untouched. Then
            record the ungated landing or retire the ticket with its own tool.
            """
            arguments = {"merge_job_id": merge_job_id, "reason": reason}
            return invoke(
                "planner.close_stuck_merge_job",
                arguments,
                lambda: service.close_stuck_merge_job(actor, **arguments),
                idempotency_key,
            )

        @server.tool(name="planner.configure_run_guardrails", structured_output=True)
        def planner_configure_run_guardrails(
            project_id: str, max_merges_per_hour: int = 12, max_consecutive_failures: int = 3
        ) -> dict[str, Any]:
            """Set the project's merge budgets; exceeding one pauses its merge queue."""
            arguments = {
                "project_id": project_id,
                "max_merges_per_hour": max_merges_per_hour,
                "max_consecutive_failures": max_consecutive_failures,
            }
            return invoke(
                "planner.configure_run_guardrails", arguments,
                lambda: service.configure_run_guardrails(actor, **arguments),
            )

        @server.tool(name="planner.get_run_guardrails", structured_output=True)
        def planner_get_run_guardrails(project_id: str) -> dict[str, Any]:
            """Read merge budgets, the merge-queue pause and the latest check of main."""
            arguments = {"project_id": project_id}
            return invoke(
                "planner.get_run_guardrails", arguments,
                lambda: service.get_run_guardrails(actor, **arguments),
            )

        @server.tool(name="planner.resume_merge_queue", structured_output=True)
        def planner_resume_merge_queue(
            project_id: str, reason: str, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            """Lift a merge-queue pause (red main or failure budget) after deciding on it.

            Resets the consecutive-failure count. A red main's revert ticket stays
            open; resume once it is readied or main is green again.
            """
            arguments = {"project_id": project_id, "reason": reason}
            return invoke(
                "planner.resume_merge_queue", arguments,
                lambda: service.resume_merge_queue(actor, **arguments),
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
            project_id: str, test_command: list[str], timeout_seconds: int = 600,
            provenance_modules: list[str] | None = None,
        ) -> dict[str, Any]:
            """Register the only project test command that future QA candidates may execute.

            provenance_modules: packages every run must import from the frozen
            candidate, e.g. ["hoh_control_plane"]. Omit it to freeze what each
            candidate's base commit declares; a candidate never chooses it.
            """
            arguments = {
                "project_id": project_id,
                "test_command": test_command,
                "timeout_seconds": timeout_seconds,
                "provenance_modules": provenance_modules,
            }
            return invoke(
                "planner.configure_test_command",
                arguments,
                lambda: service.configure_test_command(actor, **arguments),
            )

        @server.tool(name="planner.get_test_profile", structured_output=True)
        def planner_get_test_profile(project_id: str) -> dict[str, Any]:
            """Read the current test command, timeout and configured_at that new candidates freeze."""
            arguments = {"project_id": project_id}
            return invoke(
                "planner.get_test_profile",
                arguments,
                lambda: service.get_test_profile(actor, project_id),
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
            quality_policy: contracts.QualityPolicy = "claim-evidence-v1",
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

        @server.tool(name="planner.configure_risk_policy", structured_output=True)
        def planner_configure_risk_policy(
            project_id: str,
            checker_distinct_from_maker: bool = True,
            high_risk_patterns: list[str] | None = None,
            medium_risk_patterns: list[str] | None = None,
            medium_max_files: int = 10,
        ) -> dict[str, Any]:
            """Set maker/checker distinctness and the path patterns that make a candidate medium or high risk.

            Omitted pattern lists keep the built-in defaults. Medium needs an
            adversarial_review evidence item before merge; high also waits for
            policy.approve_high_risk.
            """
            arguments = {
                "project_id": project_id,
                "checker_distinct_from_maker": checker_distinct_from_maker,
                "high_risk_patterns": high_risk_patterns,
                "medium_risk_patterns": medium_risk_patterns,
                "medium_max_files": medium_max_files,
            }
            return invoke(
                "planner.configure_risk_policy", arguments,
                lambda: service.configure_risk_policy(actor, **arguments),
            )

        @server.tool(name="policy.approve_high_risk", structured_output=True)
        def policy_approve_high_risk(ticket_id: str, reason: str) -> dict[str, Any]:
            """Record the policy sign-off that lets this ticket's current high-risk candidate merge.

            Bound to the frozen candidate: a rework needs a new sign-off.
            """
            arguments = {"ticket_id": ticket_id, "reason": reason}
            return invoke(
                "policy.approve_high_risk", arguments,
                lambda: service.approve_high_risk(actor, **arguments),
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
            required_role: contracts.ExecutionRole,
            acceptance_criteria: list[str],
            preservation_constraints: list[str],
            file_scope: list[str],
            dependency_ids: list[str] | None = None,
            ticket_type: contracts.TicketType = "feature_work",
            priority: int = 50,
            parent_ticket_id: str | None = None,
            validation_contract: ValidationContract | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Create a bounded backlog ticket for Developer, QA or Integrator work."""
            validation_contract = as_payload(validation_contract)
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
            validation_contract: ValidationContract,
            reason: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Bind every criterion to a scenario, observation and required evidence."""
            validation_contract = as_payload(validation_contract)
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
            resume_role: contracts.ExecutionRole,
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
            target_status: contracts.RetirementStatus = "CANCELLED",
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
            validation_contract: ValidationContract | None = None,
            preservation_constraints: list[str] | None = None,
            dependency_ids: list[str] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            """Update bounded planning fields on an eligible backlog, ready or blocked ticket."""
            validation_contract = as_payload(validation_contract)
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
            entry_type: contracts.PlannerEntryType,
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
