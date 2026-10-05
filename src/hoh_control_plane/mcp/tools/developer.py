"""Developer tools: claim, worktree, candidate submission."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from mcp.server.fastmcp import FastMCP
from hoh_control_plane.contracts import DeveloperClaims
from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.invoke import _require_snapshot
from hoh_control_plane.mcp import workers


def register(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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
                    workers._start_worktree_provision_worker(service.store.path, provision["provision_id"])
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
            developer_claims: DeveloperClaims,
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
            workers._start_candidate_submission_worker(service.store.path, queued["submission"]["submission_id"])
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
