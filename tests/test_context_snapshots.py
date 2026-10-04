"""A consequential write can be bound to the context it was decided from (HOH-203)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hoh_control_plane.domain import ContextStaleError, Role, ValidationError


def _snapshot(service, actor, ticket_id: str) -> str:
    return service.get_ticket_context(actor, ticket_id)["context_snapshot_id"]


def _recorded(service, snapshot_id: str) -> dict:
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM context_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    return dict(row) if row is not None else {}


def test_reading_context_records_a_durable_snapshot_of_what_was_observed(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]

    snapshot_id = _snapshot(service, actors["developer"], ticket["ticket_id"])

    recorded = _recorded(service, snapshot_id)
    assert recorded["subject_type"] == "ticket"
    assert recorded["subject_id"] == ticket["ticket_id"]
    assert recorded["role"] == Role.DEVELOPER.value
    versions = json.loads(recorded["source_versions"])
    assert versions["ticket_version"] == ticket["version"]
    assert "feature_version" in versions
    assert "candidate_commit" in versions
    assert "dependencies" in versions


def test_the_same_unchanged_context_records_one_snapshot_not_many(
    service, actors, seeded
) -> None:
    """The identifier is derived from content, so re-reading writes nothing new."""
    ticket = seeded["ticket"]

    first = _snapshot(service, actors["developer"], ticket["ticket_id"])
    second = _snapshot(service, actors["developer"], ticket["ticket_id"])

    assert first == second
    with service.store.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM context_snapshots WHERE subject_id = ? AND role = ?",
            (ticket["ticket_id"], Role.DEVELOPER.value),
        ).fetchone()[0]
    assert count == 1


def test_new_planner_decision_invalidates_a_snapshot_even_without_ticket_version_change(service, actors, seeded):
    ticket = seeded["ticket"]
    before = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    decision = service.append_planner_context(actors["planner"], ticket_id=ticket["ticket_id"],
        entry_type="planner_decision", content="Stop: installed runtime is ahead of this source baseline.",
        source_ref="release:0.3.5")
    with pytest.raises(ContextStaleError):
        service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900,
                             context_snapshot_id=before["context_snapshot_id"])
    current = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    assert "latest_planner_decision" not in current
    assert any(
        entry["entry_id"] == decision["entry_id"]
        for entry in current["context_entries"]
    )


def test_a_snapshot_records_what_each_role_actually_saw(service, actors, seeded) -> None:
    """Packets differ per role, so their snapshots must be distinguishable."""
    ticket = seeded["ticket"]

    developer = _snapshot(service, actors["developer"], ticket["ticket_id"])
    planner = _snapshot(service, actors["planner"], ticket["ticket_id"])

    assert developer != planner
    assert _recorded(service, developer)["role"] == Role.DEVELOPER.value
    assert _recorded(service, planner)["role"] == Role.PLANNER.value


def test_a_claim_bound_to_current_context_succeeds(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    snapshot_id = _snapshot(service, actors["developer"], ticket["ticket_id"])

    claimed = service.claim_ticket(
        actors["developer"],
        ticket["ticket_id"],
        ticket["version"],
        900,
        context_snapshot_id=snapshot_id,
    )

    assert claimed["lease"]["lease_id"]
    assert claimed["context_snapshot_id"] == snapshot_id, "echo what was bound, not a new id"


def test_a_claim_bound_to_context_the_world_moved_past_is_refused(
    service, actors, seeded
) -> None:
    """The caller read a correct packet, then acted on it after the ticket changed."""
    ticket = seeded["ticket"]
    stale = _snapshot(service, actors["developer"], ticket["ticket_id"])
    service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="Sharpen the objective while the Developer was reading",
        objective="Retry without creating duplicate orders, and say so in the receipt",
    )

    with pytest.raises(ContextStaleError) as error:
        service.claim_ticket(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"] + 1,
            900,
            context_snapshot_id=stale,
        )

    assert "ticket" in error.value.details["stale"]
    assert error.value.code == "context_stale"


def test_a_stale_response_names_the_sections_to_refresh_and_nothing_else(
    service, actors, seeded
) -> None:
    """Which part to reload is what the caller needs; the values come from reloading."""
    ticket = seeded["ticket"]
    stale = _snapshot(service, actors["developer"], ticket["ticket_id"])
    service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="A reason that must not travel in the refusal",
        priority=71,
    )

    with pytest.raises(ContextStaleError) as error:
        service.claim_ticket(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"] + 1,
            900,
            context_snapshot_id=stale,
        )

    details = error.value.details
    assert set(details) == {"stale", "ticket_id"}
    assert details["stale"] == ["planning_decisions", "ticket"]
    assert "must not travel" not in str(error.value)


def test_omitting_a_snapshot_leaves_existing_behaviour_untouched(
    service, actors, seeded
) -> None:
    """A caller that never read context cannot be stale against it."""
    ticket = seeded["ticket"]

    claimed = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    assert claimed["lease"]["lease_id"]
    assert claimed["context_snapshot_id"] is None


def test_an_unknown_snapshot_is_refused_rather_than_ignored(
    service, actors, seeded
) -> None:
    """Accepting an identifier nobody issued would make the whole check decorative."""
    ticket = seeded["ticket"]

    with pytest.raises(ContextStaleError) as error:
        service.claim_ticket(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"],
            900,
            context_snapshot_id="ctxsnap_0000000000000000",
        )

    assert error.value.details["stale"] == ["snapshot"]


def test_a_snapshot_taken_against_another_ticket_is_refused(
    service, actors, seeded
) -> None:
    planner = actors["planner"]
    other = service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-999",
        title="A different ticket entirely",
        objective="Not the one being claimed",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Its own criterion"],
        preservation_constraints=[],
        file_scope=["src/elsewhere/**"],
    )
    borrowed = _snapshot(service, actors["developer"], other["ticket_id"])
    ticket = seeded["ticket"]

    with pytest.raises(ContextStaleError):
        service.claim_ticket(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"],
            900,
            context_snapshot_id=borrowed,
        )


def test_expected_version_still_guards_independently(service, actors, seeded) -> None:
    """Snapshot binding is an addition, not a replacement for optimistic concurrency."""
    from hoh_control_plane.domain import ConflictError

    ticket = seeded["ticket"]
    snapshot_id = _snapshot(service, actors["developer"], ticket["ticket_id"])

    with pytest.raises(ConflictError):
        service.claim_ticket(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"] + 5,
            900,
            context_snapshot_id=snapshot_id,
        )


def test_a_snapshot_holds_no_narrative_only_versions(service, actors, seeded) -> None:
    """Nothing a person wrote belongs in a record used only to compare state."""
    ticket = seeded["ticket"]
    service.append_planner_context(
        actors["planner"],
        entry_type="design_decision",
        content="A sensitive decision that must not be copied into a snapshot row",
        ticket_id=ticket["ticket_id"],
    )

    snapshot_id = _snapshot(service, actors["developer"], ticket["ticket_id"])

    recorded = json.dumps(_recorded(service, snapshot_id))
    assert "sensitive decision" not in recorded
    assert set(json.loads(_recorded(service, snapshot_id)["source_versions"])) == {
        "planning_revision",
        "ticket_version",
        "feature_version",
        "candidate_commit",
        "dependencies",
        "repository_revision",
        # Barrier and retirement identity, never their narrative reasons.
        "repository_reconciliation_refs",
        "retirement",
    }


@pytest.mark.parametrize("snapshot", [None, "", "   "])
@pytest.mark.parametrize(
    ("method", "role", "tool", "arguments"),
    [
        ("submit_candidate", "developer", "dev.submit_candidate",
         ("lease", 1, "a" * 40, "b" * 40, "branch", [])),
        ("queue_candidate_submission", "developer", "dev.submit_candidate",
         ("lease", 1, "a" * 40, "b" * 40, "branch", [])),
        ("submit_qa_verdict", "qa", "qa.submit_verdict",
         ("lease", "cand", 1, "passed", [{"criterion": "x"}])),
        ("queue_merge_candidate", "integrator", "integrator.merge_candidate",
         ("lease", 1)),
    ],
)
def test_the_service_refuses_a_consequential_write_without_a_snapshot(
    service, actors, seeded, method, role, tool, arguments, snapshot
) -> None:
    """The rule holds whichever transport calls the service, not only MCP."""
    with pytest.raises(ValidationError, match=f"{tool} requires context_snapshot_id"):
        getattr(service, method)(
            actors[role], seeded["ticket"]["ticket_id"], *arguments,
            context_snapshot_id=snapshot,
        )
