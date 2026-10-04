"""Experience provenance, expiry, retirement and recency ranking (H11)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hoh_control_plane.domain import AuthorizationError, Role
from hoh_control_plane.service.experience import EXPERIENCE_EXPIRY_DAYS


def _ticket(service, planner, ticket_id: str, scope: list[str]):
    return service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title="Work",
        objective="Touch the scope",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It behaves"],
        preservation_constraints=[],
        file_scope=scope,
    )


def _experience(service, actor, ticket_id: str):
    return service.get_ticket_context(actor, f"ticket:{ticket_id}")["experience"]


def _age(service, note_id: str, days: float) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE experience_notes SET last_seen_at = ?, created_at = ? WHERE note_id = ?",
            (stamp, stamp, note_id),
        )


def test_a_note_records_the_commit_it_was_learned_against(service, actors, seeded) -> None:
    _ticket(service, actors["planner"], "DEV-901", ["src/payments/**"])
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET base_commit = ? WHERE ticket_id = ?", ("abc1234", "DEV-901")
        )
    note = service.record_experience(actors["developer"], "ticket:DEV-901", "Lesson one.")

    assert note["source_commit"] == "abc1234"
    resolved = service.get_experience(actors["developer"], f"experience:{note['note_id']}")
    assert resolved["source_commit"] == "abc1234"


def test_a_note_not_seen_for_the_expiry_window_stops_surfacing(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-902", ["src/payments/**"])
    _ticket(service, planner, "DEV-903", ["src/payments/**"])
    note = service.record_experience(actors["developer"], "ticket:DEV-902", "Old trap.")
    _age(service, note["note_id"], EXPERIENCE_EXPIRY_DAYS + 1)

    assert _experience(service, actors["developer"], "DEV-903") == []
    assert service.list_experience(planner, "project-1") == []

    # Seeing it again revives it rather than filing a duplicate.
    again = service.record_experience(actors["developer"], "ticket:DEV-902", "Old trap.")
    assert again["note_id"] == note["note_id"]
    assert [n["lesson"] for n in _experience(service, actors["developer"], "DEV-903")] == ["Old trap."]


def test_planner_retires_a_note_and_it_stops_surfacing(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-904", ["src/payments/**"])
    _ticket(service, planner, "DEV-905", ["src/payments/**"])
    note = service.record_experience(actors["developer"], "ticket:DEV-904", "Wrong now.")

    with pytest.raises(AuthorizationError):
        service.retire_experience(actors["developer"], f"experience:{note['note_id']}", "no")

    retired = service.retire_experience(planner, f"experience:{note['note_id']}", "Fixed upstream")

    assert retired["retired_at"] and retired["retired_by"] == planner.agent_id
    assert _experience(service, actors["developer"], "DEV-905") == []
    assert service.list_experience(planner, "project-1") == []
    # Recording the lesson again files a fresh note instead of reviving the retired one.
    fresh = service.record_experience(actors["developer"], "ticket:DEV-904", "Wrong now.")
    assert fresh["note_id"] != note["note_id"]


def test_recency_keeps_stale_popular_notes_from_dominating(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-906", ["src/payments/**"])
    _ticket(service, planner, "DEV-907", ["src/payments/**"])
    old = None
    for _ in range(5):
        old = service.record_experience(actors["developer"], "ticket:DEV-906", "Stale but popular.")
    _age(service, old["note_id"], 25)
    service.record_experience(actors["developer"], "ticket:DEV-906", "Fresh lesson.")

    surfaced = _experience(service, actors["developer"], "DEV-907")

    assert [n["lesson"] for n in surfaced] == ["Fresh lesson.", "Stale but popular."]
    assert [n["lesson"] for n in service.list_experience(planner, "project-1")][0] == "Fresh lesson."
