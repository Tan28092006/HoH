"""A QA-ready candidate blocked on capability approval alone (HOH-278)."""
from __future__ import annotations

import pytest

from conftest import FakeProber

CAPABILITIES = ["browser_interaction", "build_execution"]
HOST = "qa-host-a"


@pytest.fixture
def blocked(service, actors, seeded):
    """The reported state: a QA-ready candidate, a QA that declared the right
    equipment, and no approval anyone can justify."""
    prober = FakeProber()
    service.capability_prober = prober
    ticket = seeded["ticket"]
    contract = [
        {
            "criterion": criterion,
            "scenario_ref": f"browser:{index}",
            "method": "black_box",
            "expected_observation": f"Observed in a browser: {criterion}",
            "required_evidence_types": ["test_result"],
            "required_capabilities": CAPABILITIES,
        }
        for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
    ]
    service.configure_validation_contract(
        actors["planner"], ticket["ticket_id"], ticket["version"], contract, "Browser surface.",
    )
    service.configure_verification_profile(
        actors["planner"], "project-1", capabilities=CAPABILITIES + ["runtime_trace"],
        quality_policy="claim-evidence-v1",
    )
    version = service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]["version"]
    claimed = service.claim_ticket(actors["developer"], ticket["ticket_id"], version, 900)
    submitted = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claimed["lease"]["lease_id"],
        claimed["ticket"]["version"], "a" * 40, "b" * 40, "agent/dev-01/DEV-142",
        ["Ready for independent QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    service.register_agent(
        actors["qa"], skills=["hoh-qa"], execution_capabilities=CAPABILITIES, machine_id=HOST,
    )
    return {"prober": prober, "ticket_id": ticket["ticket_id"], "submitted": submitted}


def _open_jobs(service, ticket_id: str) -> list[dict]:
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM planner_jobs WHERE ticket_id = ? AND status IN ('READY','IN_PROGRESS')",
            (ticket_id,),
        )]
