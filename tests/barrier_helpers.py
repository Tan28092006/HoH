"""Shared test helper for the Planner repository-reconciliation barrier."""
from __future__ import annotations


def keep_open_tickets(service, planner, project_id: str = "project-1",
                      summary: str = "The observed commit does not change the open plan") -> int:
    """Resolve every pending repository reconciliation as Planner, keeping each ticket.

    QA rejection, integration results and observed commits open a reconciliation
    barrier atomically with their result. Tests that exercise what happens after
    Planner has reviewed that boundary use this to record the review explicitly.
    """
    resolved = 0
    while True:
        pending = [
            item for item in service.list_repository_reconciliations(planner, project_id)["reconciliations"]
            if item["status"] == "PENDING"
        ]
        if not pending:
            return resolved
        pending.sort(key=lambda item: item["created_at"])
        reconciliation = pending[0]
        decisions = [
            {"ticket_id": ticket_id, "disposition": "keep", "reason": summary}
            for ticket_id in reconciliation["ticket_versions"]
        ]
        service.resolve_repository_reconciliation(
            planner, project_id, reconciliation["reconciliation_id"], decisions, summary
        )
        resolved += 1
