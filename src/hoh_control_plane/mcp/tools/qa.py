"""QA tools: capability preflight, claim, test runs and verdicts."""
from __future__ import annotations

import anyio
from collections.abc import Callable
from typing import Any
from mcp.server.fastmcp import FastMCP
from hoh_control_plane.contracts import Findings, Verdict, as_payload
from hoh_control_plane.domain import Actor, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.mcp.invoke import _require_snapshot
from hoh_control_plane.mcp import workers


def register(server: FastMCP, service: ControlPlaneService, actor: Actor, invoke: Callable[..., Any]) -> None:
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
                    workers._start_qa_test_worker(service.store.path, queued["test_run"]["test_run_id"])
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
            verdict: Verdict,
            findings: Findings,
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
                "findings": as_payload(findings),
                "context_snapshot_id": context_snapshot_id,
            }
            return invoke(
                "qa.submit_verdict", arguments,
                lambda: service.submit_qa_verdict(actor, **arguments),
                idempotency_key,
            )
