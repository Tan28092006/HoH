from __future__ import annotations

import pytest

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    Role,
    ValidationError,
)


def _planned_ticket(service, actors, ticket_id: str = "DEV-900", ready: bool = True):
    return service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title="Obsolete approach",
        objective="Superseded by a better plan",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["The obsolete behavior is removed"],
        preservation_constraints=[],
        file_scope=["src/obsolete/**"],
        ready=ready,
    )


def test_planner_cancels_ready_work_and_closes_its_offers(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors)
    service.register_agent(actors["developer"], skills=["hoh-developer"])
    service.get_agent_inbox(actors["developer"], "project-1")

    cancelled = service.cancel_ticket(
        actors["planner"], ticket["ticket_id"], ticket["version"], "Approach abandoned."
    )

    assert cancelled["ticket"]["status"] == "CANCELLED"
    assert cancelled["ticket"]["version"] == ticket["version"] + 1
    assert cancelled["context_entry"]["entry_type"] == "ticket_cancelled"
    assert cancelled["context_entry"]["content"] == "Approach abandoned."
    with service.store.connection() as connection:
        queue_states = {
            row["status"]
            for row in connection.execute(
                "SELECT status FROM work_queue WHERE ticket_id = ?", (ticket["ticket_id"],)
            ).fetchall()
        }
        offer_states = {
            row["status"]
            for row in connection.execute(
                """
                SELECT o.status FROM work_offers o
                JOIN work_queue q ON q.queue_id = o.queue_id
                WHERE q.ticket_id = ?
                """,
                (ticket["ticket_id"],),
            ).fetchall()
        }
        outbox_states = {
            row["status"]
            for row in connection.execute(
                """
                SELECT d.status FROM dispatch_outbox d
                JOIN work_queue q ON q.queue_id = d.queue_id
                WHERE q.ticket_id = ?
                """,
                (ticket["ticket_id"],),
            ).fetchall()
        }
    assert queue_states == {"cancelled"}
    assert offer_states <= {"cancelled"}
    # Nothing undelivered may still go out; an already-dispatched row stays as
    # delivery history rather than being rewritten.
    assert "pending" not in outbox_states


def test_backlog_and_blocked_tickets_are_also_retirable(service, actors, seeded) -> None:
    backlog = _planned_ticket(service, actors, "DEV-901", ready=False)
    blocked_source = _planned_ticket(service, actors, "DEV-902")
    claim = service.claim_ticket(
        actors["developer"], blocked_source["ticket_id"], blocked_source["version"], 900
    )
    blocked = service.report_blocker(
        actors["developer"],
        blocked_source["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream contract is undecided.",
    )

    cancelled_backlog = service.cancel_ticket(
        actors["planner"], backlog["ticket_id"], backlog["version"], "Never needed."
    )
    cancelled_blocked = service.cancel_ticket(
        actors["planner"],
        blocked_source["ticket_id"],
        blocked["ticket"]["version"],
        "Blocker will never be resolved.",
    )

    assert cancelled_backlog["ticket"]["status"] == "CANCELLED"
    assert cancelled_blocked["ticket"]["status"] == "CANCELLED"


def test_supersede_records_an_auditable_replacement_reference(service, actors, seeded) -> None:
    obsolete = _planned_ticket(service, actors, "DEV-903")
    replacement = _planned_ticket(service, actors, "DEV-904")

    superseded = service.supersede_ticket(
        actors["planner"],
        obsolete["ticket_id"],
        obsolete["version"],
        replacement["ticket_id"],
        "Replaced by the narrower ticket.",
    )

    assert superseded["ticket"]["status"] == "SUPERSEDED"
    assert superseded["replacement"]["ticket_id"] == replacement["ticket_id"]
    assert superseded["context_entry"]["source_ref"] == f"ticket:{replacement['ticket_id']}"
    event = next(
        event
        for event in service.list_events(actors["planner"], "project-1")["events"]
        if event["event_type"] == "ticket.superseded"
    )
    assert event["payload"]["replacement_ticket_id"] == replacement["ticket_id"]
    assert event["payload"]["previous_status"] == "READY"
    assert event["aggregate_version"] == superseded["ticket"]["version"]


def test_retirement_preserves_history_and_leaves_the_replacement_workable(
    service, actors, seeded
) -> None:
    obsolete = _planned_ticket(service, actors, "DEV-905")
    replacement = _planned_ticket(service, actors, "DEV-906")
    service.append_planner_context(
        actors["planner"],
        entry_type="planning_decision",
        content="Original reasoning that must survive retirement.",
        ticket_id=obsolete["ticket_id"],
    )

    service.supersede_ticket(
        actors["planner"],
        obsolete["ticket_id"],
        obsolete["version"],
        replacement["ticket_id"],
        "Split into a narrower ticket.",
    )
    context = service.get_ticket_context(actors["planner"], obsolete["ticket_id"])
    claim = service.claim_ticket(
        actors["developer"], replacement["ticket_id"], replacement["version"], 900
    )

    assert context["ticket"]["status"] == "SUPERSEDED"
    assert any(
        entry["content"] == "Original reasoning that must survive retirement."
        for entry in context["context_entries"]
    )
    assert claim["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_retirement_refuses_active_leases_running_jobs_and_frozen_candidates(
    service, actors, seeded
) -> None:
    leased = _planned_ticket(service, actors, "DEV-907")
    claim = service.claim_ticket(
        actors["developer"], leased["ticket_id"], leased["version"], 900
    )

    blocked = service.report_blocker(
        actors["developer"],
        leased["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "Parked for now.",
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO worktree_provision_jobs
                (provision_id, project_id, ticket_id, lease_id, expected_version, requested_by,
                 status, created_at)
            VALUES ('prov_running', 'project-1', ?, 'lease_gone', 1, 'dev-01', 'RUNNING', '2026-01-01T00:00:00Z')
            """,
            (leased["ticket_id"],),
        )

    with pytest.raises(ConflictError, match="running job"):
        service.cancel_ticket(
            actors["planner"], leased["ticket_id"], blocked["ticket"]["version"], "Too late."
        )

    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE worktree_provision_jobs SET status = 'COMPLETED' WHERE provision_id = 'prov_running'"
        )
        connection.execute(
            """
            INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims,
                 submitted_by, submitted_at, frozen_at)
            VALUES ('cand_frozen', ?, 'a', 'b', 'branch', '[]', 'dev-01',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (leased["ticket_id"],),
        )

    with pytest.raises(ConflictError, match="frozen candidate"):
        service.cancel_ticket(
            actors["planner"], leased["ticket_id"], blocked["ticket"]["version"], "Too late."
        )


def test_a_live_lease_on_retirable_work_is_still_refused(service, actors, seeded) -> None:
    """Defense in depth: the status guard normally fires first, but a lease that
    outlives a retirable status must never be cancelled out from under its owner."""
    ticket = _planned_ticket(service, actors, "DEV-913")
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO leases
                (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
            VALUES ('lease_live', ?, 'dev-01', 'developer',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z')
            """,
            (ticket["ticket_id"],),
        )

    with pytest.raises(ConflictError, match="active lease"):
        service.cancel_ticket(
            actors["planner"], ticket["ticket_id"], ticket["version"], "Too late."
        )


def test_an_expired_lease_does_not_block_retirement(service, actors, seeded) -> None:
    """A stale lease row is abandoned work, not active work."""
    ticket = _planned_ticket(service, actors, "DEV-914")
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO leases
                (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
            VALUES ('lease_stale', ?, 'dev-01', 'developer',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:15:00Z')
            """,
            (ticket["ticket_id"],),
        )

    cancelled = service.cancel_ticket(
        actors["planner"], ticket["ticket_id"], ticket["version"], "Abandoned."
    )

    assert cancelled["ticket"]["status"] == "CANCELLED"


def test_retirement_refuses_active_phases_stale_versions_and_other_roles(
    service, actors, seeded
) -> None:
    active = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], active["ticket_id"], active["version"], 900
    )
    retirable = _planned_ticket(service, actors, "DEV-908")

    with pytest.raises(ConflictError, match="backlog, ready or blocked"):
        service.cancel_ticket(
            actors["planner"], active["ticket_id"], claim["ticket"]["version"], "No."
        )
    with pytest.raises(ConflictError, match="version"):
        service.cancel_ticket(
            actors["planner"], retirable["ticket_id"], retirable["version"] + 3, "Stale."
        )
    with pytest.raises(AuthorizationError):
        service.cancel_ticket(
            actors["developer"], retirable["ticket_id"], retirable["version"], "Not my call."
        )
    assert (
        service.get_ticket_context(actors["planner"], retirable["ticket_id"])["ticket"]["status"]
        == "READY"
    )


def test_supersede_refuses_a_missing_self_or_cross_project_replacement(
    service, actors, seeded
) -> None:
    obsolete = _planned_ticket(service, actors, "DEV-909")
    # The second project is set up by an actor that can reach it; the Planner under
    # test deliberately cannot, which is exactly the cross-project case being refused.
    cross_project_admin = Actor("admin-setup", Role.ADMIN, frozenset({"*"}))
    service.create_project(
        cross_project_admin, "project-2", "Other", "Other project", "main"
    )
    service.create_feature(
        cross_project_admin,
        project_id="project-2",
        feature_id="other-feature",
        title="Other",
        objective="Other objective",
    )
    foreign = service.create_ticket(
        cross_project_admin,
        feature_id="other-feature",
        ticket_id="OTHER-1",
        title="Foreign ticket",
        objective="Belongs to another project",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Something"],
        preservation_constraints=[],
        file_scope=["src/**"],
        ready=True,
    )

    with pytest.raises(NotFoundError):
        service.supersede_ticket(
            actors["planner"], obsolete["ticket_id"], obsolete["version"], "MISSING-1", "Gone."
        )
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        service.supersede_ticket(
            actors["planner"],
            obsolete["ticket_id"],
            obsolete["version"],
            obsolete["ticket_id"],
            "Self.",
        )
    with pytest.raises(ValidationError, match="same project"):
        service.supersede_ticket(
            actors["planner"],
            obsolete["ticket_id"],
            obsolete["version"],
            foreign["ticket_id"],
            "Cross project.",
        )
    assert (
        service.get_ticket_context(actors["planner"], obsolete["ticket_id"])["ticket"]["status"]
        == "READY"
    )


def test_retired_work_leaves_discovery_but_the_board_keeps_its_history(
    service, actors, seeded
) -> None:
    ticket = _planned_ticket(service, actors, "DEV-910")

    service.cancel_ticket(
        actors["planner"], ticket["ticket_id"], ticket["version"], "Obsolete."
    )
    developer_view = service.discover_work(actors["developer"], "project-1")
    planner_view = service.discover_work(actors["planner"], "project-1")
    board = service.board("project-1")

    discovered = {
        found["ticket_id"]
        for view in (developer_view, planner_view)
        for feature in view["features"]
        for found in feature["tickets"]
    }
    assert ticket["ticket_id"] not in discovered
    assert ticket["ticket_id"] in {
        item["ticket_id"] for item in board["columns"]["CANCELLED"]
    }
    assert ticket["ticket_id"] not in {
        item["ticket_id"] for item in board["columns"].get("READY", [])
    }


def test_retirement_names_the_dependents_it_leaves_behind(service, actors, seeded) -> None:
    """Retiring work others depend on is allowed, but never silently."""
    obsolete = _planned_ticket(service, actors, "DEV-911")
    service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-912",
        title="Dependent work",
        objective="Waits on the obsolete ticket",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Depends on the retired ticket"],
        preservation_constraints=[],
        file_scope=["src/dependent/**"],
        dependency_ids=[obsolete["ticket_id"]],
        ready=True,
    )

    cancelled = service.cancel_ticket(
        actors["planner"], obsolete["ticket_id"], obsolete["version"], "Obsolete."
    )

    assert cancelled["dependent_ticket_ids"] == ["DEV-912"]
