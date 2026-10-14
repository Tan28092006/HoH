"""Role-scoped server-run control. This surface never launches a CLI worker."""
from __future__ import annotations
from typing import Any

from hoh_control_plane.contracts import AutonomyLimits
from hoh_control_plane.domain import Role


def register(server, service, actor, invoke):
    @server.tool(name="autonomy.status", structured_output=True)
    def autonomy_status(run_id: str) -> dict[str, Any]:
        """Read retained attempts, admission counts and unacknowledged notices."""
        return invoke("autonomy.status", {"run_id": run_id}, lambda: service.autonomy_status(actor, run_id))

    if actor.role in {Role.PLANNER, Role.ADMIN}:
        @server.tool(name="autonomy.reconcile", structured_output=True)
        def autonomy_reconcile(run_id: str) -> dict[str, Any]:
            """Resolve lost reservations from durable jobs/fresh Git, preserving uncertainty."""
            return invoke("autonomy.reconcile", {"run_id": run_id}, lambda: service.autonomy_reconcile(actor, run_id))

        @server.tool(name="autonomy.start", structured_output=True)
        def autonomy_start(project_id: str, limits: AutonomyLimits, role_agents: dict[str, str],
                           idempotency_key: str, ticket_ids: list[str] | None = None,
                           feature_id: str | None = None, idle_seconds: int = 300) -> dict[str, Any]:
            """Freeze an authorized ticket scope and finite server admission ceilings."""
            arguments = dict(project_id=project_id, limits=limits.model_dump(), role_agents=role_agents,
                             idempotency_key=idempotency_key, ticket_ids=ticket_ids,
                             feature_id=feature_id, idle_seconds=idle_seconds)
            return invoke("autonomy.start", arguments, lambda: service.autonomy_start(actor, **arguments))

        @server.tool(name="autonomy.stop", structured_output=True)
        def autonomy_stop(run_id: str, reason: str = "owner_stop") -> dict[str, Any]:
            """Stop admissions and drain accepted leases and execution reservations."""
            return invoke("autonomy.stop", {"run_id": run_id, "reason": reason}, lambda: service.autonomy_stop(actor, run_id, reason))

        @server.tool(name="autonomy.resume", structured_output=True)
        def autonomy_resume(run_id: str, expected_version: int, limits: AutonomyLimits | None = None) -> dict[str, Any]:
            """Explicitly resume a settled paused run after capability and budget checks."""
            arguments = {"run_id": run_id, "expected_version": expected_version, "limits": limits.model_dump() if limits else None}
            return invoke("autonomy.resume", arguments, lambda: service.autonomy_resume(actor, **arguments))

        @server.tool(name="autonomy.acknowledge", structured_output=True)
        def autonomy_acknowledge(run_id: str, notification_id: str) -> dict[str, Any]:
            """Mark a durable notice viewed; status retains it until acknowledged."""
            return invoke("autonomy.acknowledge", {"run_id": run_id, "notification_id": notification_id}, lambda: service.autonomy_acknowledge(actor, run_id, notification_id))

    if actor.role in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
        @server.tool(name="autonomy.signal", structured_output=True)
        def autonomy_signal(run_id: str, reason: str) -> dict[str, Any]:
            """Drain on a typed provider, repository, plan or infrastructure signal."""
            return invoke("autonomy.signal", {"run_id": run_id, "reason": reason}, lambda: service.autonomy_signal(actor, run_id, reason))

        @server.tool(name="autonomy.admit", structured_output=True)
        def autonomy_admit(run_id: str, ticket_id: str, expected_version: int,
                           context_snapshot_id: str, idempotency_key: str, lease_seconds: int = 900) -> dict[str, Any]:
            """Reserve one launch and claim through existing role checks atomically."""
            arguments = dict(run_id=run_id, ticket_id=ticket_id, expected_version=expected_version,
                             context_snapshot_id=context_snapshot_id, idempotency_key=idempotency_key,
                             lease_seconds=lease_seconds)
            return invoke("autonomy.admit", arguments, lambda: service.autonomy_admit(actor, **arguments))

        @server.tool(name="autonomy.finish_attempt", structured_output=True)
        def autonomy_finish_attempt(attempt_id: str, observed_tokens: int | None,
                                    cli_outcome: str, idempotency_key: str) -> dict[str, Any]:
            """Settle optional usage once; durable job truth survives nonzero exit."""
            arguments = dict(attempt_id=attempt_id, observed_tokens=observed_tokens,
                             cli_outcome=cli_outcome, idempotency_key=idempotency_key)
            return invoke("autonomy.finish_attempt", arguments, lambda: service.autonomy_finish_attempt(actor, **arguments))
