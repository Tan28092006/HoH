"""Every typed reference a packet hands a role must resolve for that role."""
from __future__ import annotations

import json
import re

import pytest

_REFERENCE = re.compile(r"ctx:[A-Za-z0-9_.\-]+(?::[A-Za-z0-9_.\-]+)*")


def _references(value: object) -> set[str]:
    return set(_REFERENCE.findall(json.dumps(value)))


def _crowd_the_ticket(service, actors, seeded) -> None:
    ticket_id = seeded["ticket"]["ticket_id"]
    for index in range(12):
        for entry_type in ("planner_decision", "risk", "note"):
            service.append_planner_context(
                actors["planner"],
                entry_type=entry_type,
                content=f"{entry_type} {index}: " + "a long explanation " * 40,
                ticket_id=ticket_id,
            )


@pytest.mark.parametrize("role", ["planner", "developer", "qa", "integrator"])
@pytest.mark.parametrize("budget", [200, 600, None])
def test_every_reference_in_a_packet_resolves_for_its_role(
    service, actors, seeded, role, budget
) -> None:
    _crowd_the_ticket(service, actors, seeded)
    actor = actors[role]
    packets = [
        service.get_ticket_context(actor, f"ticket:{seeded['ticket']['ticket_id']}", budget),
        service.get_feature_context(actor, f"feature:{seeded['feature']['feature_id']}", budget),
        service.discover_work(actor, seeded["project"]["project_id"]),
    ]
    references = set().union(*(_references(packet) for packet in packets))

    assert any(reference.startswith("ctx:ticket:") for reference in references)
    for reference in sorted(references):
        resolved = service.resolve_context_reference(actor, reference)
        assert resolved, reference


def test_a_budget_dropped_section_resolves_to_its_content(service, actors, seeded) -> None:
    _crowd_the_ticket(service, actors, seeded)
    packet = service.get_ticket_context(
        actors["developer"], f"ticket:{seeded['ticket']['ticket_id']}", 200
    )
    dropped = [
        (item["section"], reference)
        for item in packet["omitted_sections"]
        if item["reason"] == "token_budget"
        for reference in item["references"]
        if reference.startswith("ctx:ticket:")
    ]
    for section, reference in dropped:
        assert section in service.resolve_context_reference(actors["developer"], reference)


def test_the_feature_rollup_reference_resolves_to_a_compact_ticket(
    service, actors, seeded
) -> None:
    ticket_id = seeded["ticket"]["ticket_id"]
    resolved = service.resolve_context_reference(actors["qa"], f"ctx:ticket:{ticket_id}")
    assert resolved["ticket"]["ticket_id"] == ticket_id
    assert "acceptance_criteria" not in resolved["ticket"]
