"""Reading what changed, instead of re-reading what is (HOH-253)."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError


def _tokens(payload) -> int:
    return len(json.dumps(payload, default=str)) // 4


def _digest(service, actor, after: int = 0, **kwargs):
    return service.list_event_digest(actor, "project-1", after, **kwargs)


def _churn(service, actors, seeded) -> None:
    """Several events on one ticket, which is what collapsing has to handle."""
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    for index in range(4):
        service.append_progress(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            f"Checkpoint {index}",
        )


def test_a_quiet_interval_costs_almost_nothing(service, actors, seeded) -> None:
    """The common case under any wake-up schedule, so it has to be the cheap one."""
    planner = actors["planner"]
    first = _digest(service, planner)

    quiet = _digest(service, planner, first["next_cursor"])

    assert quiet["changed"] == []
    assert quiet["event_count"] == 0
    assert quiet["reset"] is False
    assert _tokens(quiet) < 60, "a nothing-happened answer stays tiny"


def test_many_events_on_one_ticket_read_as_one_entry(service, actors, seeded) -> None:
    planner = actors["planner"]
    before = _digest(service, planner)["next_cursor"]
    _churn(service, actors, seeded)

    digest = _digest(service, planner, before)

    assert digest["event_count"] > 1, "several events really happened"
    tickets = [entry for entry in digest["changed"] if entry["type"] == "ticket"]
    assert len(tickets) == 1, "collapsed by the thing they happened to"
    assert tickets[0]["events"] == digest["event_count"]


def test_an_entry_says_where_the_thing_ended_up(service, actors, seeded) -> None:
    """So a caller need not resolve a context packet just to learn the state."""
    planner = actors["planner"]
    before = _digest(service, planner)["next_cursor"]
    _churn(service, actors, seeded)

    entry = [
        item for item in _digest(service, planner, before)["changed"]
        if item["type"] == "ticket"
    ][0]

    assert entry["status"] == "DEV_IN_PROGRESS"
    assert entry["ref"] == f"ticket:{seeded['ticket']['ticket_id']}"
    assert entry["last_event"], "and what most recently happened to it"


def test_separate_things_stay_separate(service, actors, seeded) -> None:
    planner = actors["planner"]
    before = _digest(service, planner)["next_cursor"]
    service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-701",
        title="Another",
        objective="Separate work",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=["src/other/**"],
    )
    _churn(service, actors, seeded)

    changed = {entry["id"] for entry in _digest(service, planner, before)["changed"]}

    assert {"DEV-701", seeded["ticket"]["ticket_id"]} <= changed


def test_a_cursor_never_replays_what_it_already_gave(service, actors, seeded) -> None:
    planner = actors["planner"]
    first = _digest(service, planner)
    _churn(service, actors, seeded)

    second = _digest(service, planner, first["next_cursor"])
    third = _digest(service, planner, second["next_cursor"])

    assert second["event_count"] > 0
    assert third["event_count"] == 0, "continuing from the cursor yields nothing twice"


def test_a_cursor_this_history_cannot_contain_asks_for_a_reload(
    service, actors, seeded
) -> None:
    digest = _digest(service, actors["planner"], 999_999)

    assert digest["reset"] is True
    assert digest["changed"] == []
    assert digest["next_cursor"] == 0, "start again rather than wait forever"


def test_the_page_is_bounded_and_says_when_more_remains(service, actors, seeded) -> None:
    planner = actors["planner"]
    before = _digest(service, planner)["next_cursor"]
    _churn(service, actors, seeded)

    digest = _digest(service, planner, before, limit=2)

    assert digest["event_count"] == 2
    assert digest["has_more"] is True
    assert _digest(service, planner, digest["next_cursor"])["event_count"] > 0


def test_the_digest_carries_no_payload_or_identifiers(service, actors, seeded) -> None:
    """It exists to be cheap and polled often, which is the wrong place for prose."""
    planner = actors["planner"]
    before = _digest(service, planner)["next_cursor"]
    _churn(service, actors, seeded)

    rendered = json.dumps(_digest(service, planner, before))

    assert "payload" not in rendered
    assert "Checkpoint" not in rendered, "narration stays out"
    assert "lease" not in rendered.lower()
    for entry in _digest(service, planner, before)["changed"]:
        assert set(entry) <= {"type", "id", "ref", "events", "last_event", "at", "status"}


def test_the_digest_costs_materially_less_than_the_full_read(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    _churn(service, actors, seeded)

    full = service.list_events(planner, "project-1", 0)
    digest = _digest(service, planner, 0)

    assert digest["event_count"] == len(full["events"]), "the same events, said smaller"
    assert _tokens(digest) < _tokens(full) / 2


def test_a_negative_cursor_is_refused(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        _digest(service, actors["planner"], -1)


def test_the_digest_is_scoped_to_the_actor_s_projects(service, actors, seeded) -> None:
    outsider = Actor("outsider", Role.PLANNER, frozenset({"elsewhere"}))

    with pytest.raises(AuthorizationError):
        service.list_event_digest(outsider, "project-1", 0)


def test_reading_the_digest_changes_nothing(service, actors, seeded) -> None:
    planner = actors["planner"]
    _churn(service, actors, seeded)
    before = service.board("project-1")

    _digest(service, planner, 0)
    _digest(service, planner, 0)

    assert service.board("project-1") == before
