"""Every packet says who wrote what, so advice never reads as instruction.

A Developer's claims and a QA observation reach Planner and the other roles in
the same packet as the Planner's contract. Labelled by author, a reader can tell
the direction it must follow from the evidence it must check.
"""
from __future__ import annotations

from pathlib import Path

from test_context_compaction import TIGHT_BUDGET, _all_roles, _rich_ticket

AUTHORS = {"planner", "developer", "qa", "integrator", "system"}


def test_sections_and_entries_carry_their_author(service, actors, seeded, tmp_path: Path) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for role, actor in _all_roles(actors).items():
        packet = service.get_ticket_context(actor, ticket["ticket_id"])

        sources = packet["sources"]
        assert set(sources) <= AUTHORS, role
        assert "ticket" in sources["planner"], role
        assert sources["developer"] == ["candidate"], role
        assert "known_failures" in sources["qa"], role
        for entry in packet["context_entries"]:
            assert "actor_role" not in entry
            stored = service.resolve_context_reference(
                actor, f"ctx:entry:{entry['entry_id']}"
            )["entry"]["actor_role"]
            assert entry["source"] == (stored if stored in AUTHORS else "system"), role


def test_qa_and_planner_see_qa_and_planner_text_labelled(service, actors, seeded, tmp_path: Path) -> None:
    ticket, _candidate, decision = _rich_ticket(service, actors, seeded, tmp_path)

    for actor in (actors["qa"], actors["planner"]):
        entries = {
            entry["entry_type"]: entry["source"]
            for entry in service.get_ticket_context(actor, ticket["ticket_id"])["context_entries"]
        }
        assert entries["design_decision"] == "planner"
        assert entries["known_failure"] == "qa"


def test_a_section_dropped_for_the_budget_loses_its_label(service, actors, seeded, tmp_path: Path) -> None:
    ticket, _candidate, _decision = _rich_ticket(service, actors, seeded, tmp_path)

    for actor in _all_roles(actors).values():
        packet = service.get_ticket_context(actor, ticket["ticket_id"], token_budget=TIGHT_BUDGET)

        labelled = [section for sections in packet["sources"].values() for section in sections]
        assert labelled and all(packet.get(section) for section in labelled)


def test_feature_packets_are_labelled_too(service, actors, seeded) -> None:
    packet = service.get_feature_context(actors["developer"], "payment-retry")

    assert "feature" in packet["sources"]["planner"]
    assert "tickets" in packet["sources"]["planner"]
