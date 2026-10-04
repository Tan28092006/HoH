"""Integrator tools: claim and merge a QA-verified candidate."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from mcp.server.fastmcp import FastMCP
from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.invoke import _require_snapshot
from hoh_control_plane.mcp import workers


def register(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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
                    workers._start_merge_worker(service.store.path, queued["merge_job"]["merge_job_id"])
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
