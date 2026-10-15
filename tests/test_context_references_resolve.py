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


@pytest.mark.parametrize("role", ["planner", "integrator"])
@pytest.mark.parametrize("prefix", ["mainline_check:", "ctx:mainline_check:"])
def test_mainline_check_reference_is_bounded_and_redacts_raw_output(service, actors, seeded, role, prefix):
    from tests.test_mainline_guardrails import _setup, _finish, OLD

    _setup(service, actors, seeded)
    _finish(service)
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE mainline_checks SET output_tail = ?, error_message = ?",
                   ("token=DO_NOT_EXPOSE" * 1000, "Bearer PRIVATE" * 1000))
        job = db.execute("SELECT evidence_refs FROM planner_jobs WHERE summary LIKE 'merge_queue_paused%'").fetchone()
    emitted = next(ref for ref in json.loads(job["evidence_refs"]) if ref.startswith("mainline_check:"))
    assert emitted == "mainline_check:old"
    resolved = service.resolve_context_reference(actors[role], prefix + "old")
    summary = resolved["mainline_check"]
    assert summary["mainline_commit"] == OLD
    assert summary["outcome"] == "red"
    assert summary["termination_reason"] == "test_failure"
    assert summary["diagnostic"] == {
        "output_present": True, "error_present": True, "evidence_ref": emitted,
    }
    assert "DO_NOT_EXPOSE" not in json.dumps(resolved)
    assert "PRIVATE" not in json.dumps(resolved)
    assert len(json.dumps(resolved)) < 1200


def test_mainline_check_reference_enforces_role_project_and_existence(service, actors, seeded):
    from hoh_control_plane.domain import Actor, AuthorizationError, NotFoundError, Role
    from tests.test_mainline_guardrails import _setup

    _setup(service, actors, seeded)
    with pytest.raises(AuthorizationError):
        service.resolve_context_reference(Actor("other", Role.PLANNER, frozenset({"other-project"})),
                                          "mainline_check:old")
    for role in ("developer", "qa"):
        with pytest.raises(AuthorizationError):
            service.resolve_context_reference(actors[role], "mainline_check:old")
    with pytest.raises(NotFoundError):
        service.resolve_context_reference(actors["planner"], "mainline_check:missing")
