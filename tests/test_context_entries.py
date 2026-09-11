"""What gets written to a ticket's history, and what gets read back (HOH-247)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hoh_control_plane.domain import Role
from hoh_control_plane.service._common import (
    CONTEXT_ENTRY_ALIASES,
    CONTEXT_ENTRY_TYPES,
    GENERAL_CONTEXT_ENTRY_TYPE,
    _VERBOSE_ENTRY_TYPES,
)


DECISION = "Retries must stay idempotent for the same order id."


def _entries(service, actor, ticket_id: str) -> list[dict]:
    return service.get_ticket_context(actor, ticket_id, 60_000)["context_entries"]


def _rows(service, ticket_id: str) -> list[dict]:
    with service.store.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM context_entries WHERE ticket_id = ? ORDER BY created_at",
                (ticket_id,),
            ).fetchall()
        ]


# --- duplication --------------------------------------------------------------


def test_saying_the_same_thing_twice_does_not_add_a_second_row(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]

    first = service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )
    again = service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    assert first["deduplicated"] is False
    assert again["deduplicated"] is True, "the caller can tell recorded from already said"
    assert again["entry_id"] == first["entry_id"]
    assert len(_rows(service, ticket["ticket_id"])) == 1


def test_a_near_duplicate_is_still_a_new_decision(service, actors, seeded) -> None:
    """Deduplication must not swallow a decision that says something different."""
    ticket = seeded["ticket"]
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    narrower = service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION + " Applies to partial captures as well.",
        ticket_id=ticket["ticket_id"],
    )

    assert narrower["deduplicated"] is False
    assert len(_rows(service, ticket["ticket_id"])) == 2


def test_the_same_words_under_a_different_type_are_a_different_statement(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    as_risk = service.append_planner_context(
        actors["planner"], entry_type="risk", content=DECISION, ticket_id=ticket["ticket_id"]
    )

    assert as_risk["deduplicated"] is False


def test_deduplication_is_scoped_to_one_ticket(service, actors, seeded) -> None:
    """A decision repeated on another ticket is a decision about that ticket."""
    planner = actors["planner"]
    other = service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-808",
        title="A second ticket",
        objective="Its own objective",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=["src/other/**"],
    )
    service.append_planner_context(
        planner, entry_type="planner_decision", content=DECISION,
        ticket_id=seeded["ticket"]["ticket_id"],
    )

    elsewhere = service.append_planner_context(
        planner, entry_type="planner_decision", content=DECISION,
        ticket_id=other["ticket_id"],
    )

    assert elsewhere["deduplicated"] is False


# --- the type vocabulary ------------------------------------------------------


def test_an_unknown_label_lands_in_the_general_category_without_being_lost(
    service, actors, seeded
) -> None:
    """41 types for 206 entries is what happens when every situation invents one."""
    ticket = seeded["ticket"]

    entry = service.append_planner_context(
        actors["planner"],
        entry_type="stale_candidate_hold",
        content="Hold this candidate until HOH-213 integrates.",
        ticket_id=ticket["ticket_id"],
    )

    assert entry["entry_type"] == GENERAL_CONTEXT_ENTRY_TYPE
    assert "[stale_candidate_hold]" in entry["content"], "the caller's intent survives"
    assert "Hold this candidate" in entry["content"]


def test_a_synonym_is_renamed_rather_than_qualified(service, actors, seeded) -> None:
    """Mapping a label that means exactly what a real type means loses nothing."""
    ticket = seeded["ticket"]

    entry = service.append_planner_context(
        actors["planner"],
        entry_type="planning_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    assert entry["entry_type"] == "planner_decision"
    assert entry["content"] == DECISION, "no qualifier, because nothing was lost"


def test_every_alias_points_at_a_real_type(service) -> None:
    assert set(CONTEXT_ENTRY_ALIASES.values()) <= CONTEXT_ENTRY_TYPES


def test_the_general_category_is_protected_from_compaction(service) -> None:
    """An unrecognised label is a reason to keep something, not to throw it away."""
    assert GENERAL_CONTEXT_ENTRY_TYPE in CONTEXT_ENTRY_TYPES
    assert GENERAL_CONTEXT_ENTRY_TYPE not in _VERBOSE_ENTRY_TYPES


# --- what compaction may and may not remove -----------------------------------


def test_machine_written_command_status_is_treated_as_narration(service) -> None:
    """40 rows of it restate what qa_test_runs already holds."""
    assert "qa_automated_test_run" in _VERBOSE_ENTRY_TYPES


def test_a_decision_survives_compaction_that_removes_narration(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    for index in range(12):
        service.append_progress(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            f"Checkpoint {index}: " + "verbose narration that compaction may drop. " * 20,
        )
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    packet = service.get_ticket_context(actors["developer"], ticket["ticket_id"], 2_000)

    entries = {entry["entry_type"]: entry for entry in packet["context_entries"]}
    assert "planner_decision" in entries, "a decision is never traded for room"
    assert entries["planner_decision"]["content"] == DECISION, "and never shortened"
    narration = [
        entry for entry in packet["context_entries"] if entry["entry_type"] == "progress_update"
    ]
    # Shortening comes before dropping, so at this budget the narration is still
    # present but has been reduced to checkpoints.
    assert narration, "the run is still visible"
    assert all(len(entry["content"]) < 400 for entry in narration), "narration is what gives way"
    assert packet["estimated_tokens"] <= 2_000


def test_nothing_already_written_is_rewritten_by_any_of_this(
    service, actors, seeded
) -> None:
    """This changes what gets written and what gets read, never what was recorded."""
    ticket = seeded["ticket"]
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )
    before = _rows(service, ticket["ticket_id"])

    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content=DECISION,
        ticket_id=ticket["ticket_id"],
    )

    assert _rows(service, ticket["ticket_id"]) == before
