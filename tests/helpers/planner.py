"""Planner-side ticket states."""
from __future__ import annotations


def _blocked_ticket(service, actors, seeded):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    return service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream payment contract is undecided.",
    )
