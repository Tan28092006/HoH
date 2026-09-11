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


def test_planner_updates_bounded_fields_on_a_backlog_ticket(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors, "DEV-920", ready=False)

    updated = service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        "Scope narrowed after review.",
        title="Obsolete approach, narrowed",
        objective="Only remove the dead code path",
        priority=80,
        acceptance_criteria=["The dead code path is removed"],
        preservation_constraints=["Live code paths stay untouched"],
    )

    assert updated["ticket"]["title"] == "Obsolete approach, narrowed"
    assert updated["ticket"]["objective"] == "Only remove the dead code path"
    assert updated["ticket"]["priority"] == 80
    assert updated["ticket"]["acceptance_criteria"] == ["The dead code path is removed"]
    assert updated["ticket"]["preservation_constraints"] == ["Live code paths stay untouched"]
    assert updated["ticket"]["version"] == ticket["version"] + 1
    assert set(updated["changed_fields"]) == {
        "title",
        "objective",
        "priority",
        "acceptance_criteria",
        "preservation_constraints",
    }
    assert updated["context_entry"]["entry_type"] == "ticket_fields_updated"
    assert updated["context_entry"]["content"] == "Scope narrowed after review."


def test_planner_updates_dependencies_on_a_backlog_ticket(service, actors, seeded) -> None:
    dependency = _planned_ticket(service, actors, "DEV-921", ready=False)
    ticket = _planned_ticket(service, actors, "DEV-922", ready=False)

    updated = service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        "Now depends on the shared groundwork ticket.",
        dependency_ids=[dependency["ticket_id"]],
    )

    assert updated["ticket"]["dependency_ids"] == [dependency["ticket_id"]]
    assert updated["changed_fields"] == ["dependency_ids"]


def test_update_can_clear_dependencies(service, actors, seeded) -> None:
    dependency = _planned_ticket(service, actors, "DEV-923", ready=False)
    ticket = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-924",
        title="Depends on DEV-923",
        objective="Needs the groundwork ticket first",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Something"],
        preservation_constraints=[],
        file_scope=["src/**"],
        dependency_ids=[dependency["ticket_id"]],
        ready=False,
    )

    updated = service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        "No longer needed.",
        dependency_ids=[],
    )

    assert updated["ticket"]["dependency_ids"] == []


def test_update_refuses_a_dependency_cycle(service, actors, seeded) -> None:
    a = _planned_ticket(service, actors, "DEV-925", ready=False)
    b = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-926",
        title="B",
        objective="Depends on A",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Something"],
        preservation_constraints=[],
        file_scope=["src/**"],
        dependency_ids=[a["ticket_id"]],
        ready=False,
    )

    with pytest.raises(ValidationError, match="cycle"):
        service.update_ticket_fields(
            actors["planner"],
            a["ticket_id"],
            a["version"],
            "Try to close the loop.",
            dependency_ids=[b["ticket_id"]],
        )


def test_update_refuses_self_dependency(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors, "DEV-927", ready=False)

    with pytest.raises(ValidationError, match="itself"):
        service.update_ticket_fields(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            "Oops.",
            dependency_ids=[ticket["ticket_id"]],
        )


def test_update_refuses_a_cross_project_dependency(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors, "DEV-928", ready=False)
    cross_project_admin = Actor("admin-setup", Role.ADMIN, frozenset({"*"}))
    service.create_project(cross_project_admin, "project-2", "Other", "Other project", "main")
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
        ticket_id="OTHER-2",
        title="Foreign ticket",
        objective="Belongs to another project",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Something"],
        preservation_constraints=[],
        file_scope=["src/**"],
        ready=True,
    )

    with pytest.raises(ValidationError, match="same project"):
        service.update_ticket_fields(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            "Cross-project.",
            dependency_ids=[foreign["ticket_id"]],
        )


def test_update_refuses_an_unfinished_dependency_on_a_ready_ticket(
    service, actors, seeded
) -> None:
    unfinished = _planned_ticket(service, actors, "DEV-929", ready=True)
    ticket = _planned_ticket(service, actors, "DEV-930", ready=True)

    with pytest.raises(ConflictError, match="unfinished work"):
        service.update_ticket_fields(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            "Adding a dependency that is not finished.",
            dependency_ids=[unfinished["ticket_id"]],
        )


def test_update_allows_an_unfinished_dependency_on_a_backlog_ticket(
    service, actors, seeded
) -> None:
    unfinished = _planned_ticket(service, actors, "DEV-931", ready=True)
    ticket = _planned_ticket(service, actors, "DEV-932", ready=False)

    updated = service.update_ticket_fields(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        "Backlog work may depend on unfinished work.",
        dependency_ids=[unfinished["ticket_id"]],
    )

    assert updated["ticket"]["dependency_ids"] == [unfinished["ticket_id"]]


def test_update_refuses_non_eligible_statuses_stale_versions_and_other_roles(
    service, actors, seeded
) -> None:
    active = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], active["ticket_id"], active["version"], 900)
    eligible = _planned_ticket(service, actors, "DEV-933", ready=False)

    with pytest.raises(ConflictError, match="backlog, ready or blocked"):
        service.update_ticket_fields(
            actors["planner"],
            active["ticket_id"],
            claim["ticket"]["version"],
            "No.",
            priority=10,
        )
    with pytest.raises(ConflictError, match="version"):
        service.update_ticket_fields(
            actors["planner"],
            eligible["ticket_id"],
            eligible["version"] + 3,
            "Stale.",
            priority=10,
        )
    with pytest.raises(AuthorizationError):
        service.update_ticket_fields(
            actors["developer"],
            eligible["ticket_id"],
            eligible["version"],
            "Not my call.",
            priority=10,
        )
    assert (
        service.get_ticket_context(actors["planner"], eligible["ticket_id"])["ticket"]["priority"]
        != 10
    )


def test_update_refuses_while_a_lease_is_active(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors, "DEV-934", ready=True)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO leases
                (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
            VALUES ('lease_live_2', ?, 'dev-01', 'developer',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z')
            """,
            (ticket["ticket_id"],),
        )

    with pytest.raises(ConflictError, match="active lease"):
        service.update_ticket_fields(
            actors["planner"], ticket["ticket_id"], ticket["version"], "Too late.", priority=5,
        )


def test_update_requires_at_least_one_field(service, actors, seeded) -> None:
    ticket = _planned_ticket(service, actors, "DEV-935", ready=False)

    with pytest.raises(ValidationError, match="At least one field"):
        service.update_ticket_fields(
            actors["planner"], ticket["ticket_id"], ticket["version"], "Nothing to change.",
        )


def test_planner_splits_a_ticket_into_two_replacements(service, actors, seeded) -> None:
    original = _planned_ticket(service, actors, "DEV-940")
    part_a = _planned_ticket(service, actors, "DEV-941")
    part_b = _planned_ticket(service, actors, "DEV-942")

    split = service.split_ticket(
        actors["planner"],
        original["ticket_id"],
        original["version"],
        [part_a["ticket_id"], part_b["ticket_id"]],
        "Narrower ticket split out the migration step.",
    )

    assert split["ticket"]["status"] == "SUPERSEDED"
    assert {r["ticket_id"] for r in split["replacements"]} == {
        part_a["ticket_id"],
        part_b["ticket_id"],
    }
    assert split["context_entry"]["source_ref"] == (
        f"ticket:{part_a['ticket_id']},ticket:{part_b['ticket_id']}"
    )
    event = next(
        event
        for event in service.list_events(actors["planner"], "project-1")["events"]
        if event["event_type"] == "ticket.split"
    )
    assert event["payload"]["replacement_ticket_ids"] == [
        part_a["ticket_id"],
        part_b["ticket_id"],
    ]
    claim_a = service.claim_ticket(
        actors["developer"], part_a["ticket_id"], part_a["version"], 900
    )
    assert claim_a["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_split_refuses_fewer_than_two_replacements(service, actors, seeded) -> None:
    original = _planned_ticket(service, actors, "DEV-943")
    only = _planned_ticket(service, actors, "DEV-944")

    with pytest.raises(ValidationError, match="at least two"):
        service.split_ticket(
            actors["planner"],
            original["ticket_id"],
            original["version"],
            [only["ticket_id"]],
            "Not really a split.",
        )


def test_split_refuses_duplicate_replacements(service, actors, seeded) -> None:
    original = _planned_ticket(service, actors, "DEV-945")
    part = _planned_ticket(service, actors, "DEV-946")

    with pytest.raises(ValidationError, match="duplicate"):
        service.split_ticket(
            actors["planner"],
            original["ticket_id"],
            original["version"],
            [part["ticket_id"], part["ticket_id"]],
            "Duplicate replacement.",
        )


def test_split_refuses_a_cross_project_replacement(service, actors, seeded) -> None:
    original = _planned_ticket(service, actors, "DEV-947")
    part_a = _planned_ticket(service, actors, "DEV-948")
    cross_project_admin = Actor("admin-setup", Role.ADMIN, frozenset({"*"}))
    service.create_project(cross_project_admin, "project-3", "Other", "Other project", "main")
    service.create_feature(
        cross_project_admin,
        project_id="project-3",
        feature_id="other-feature-2",
        title="Other",
        objective="Other objective",
    )
    foreign = service.create_ticket(
        cross_project_admin,
        feature_id="other-feature-2",
        ticket_id="OTHER-3",
        title="Foreign ticket",
        objective="Belongs to another project",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Something"],
        preservation_constraints=[],
        file_scope=["src/**"],
        ready=True,
    )

    with pytest.raises(ValidationError, match="same project"):
        service.split_ticket(
            actors["planner"],
            original["ticket_id"],
            original["version"],
            [part_a["ticket_id"], foreign["ticket_id"]],
            "Cross-project split.",
        )


def test_split_refuses_active_work_non_eligible_status_and_stale_version(
    service, actors, seeded
) -> None:
    active = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], active["ticket_id"], active["version"], 900)
    part_a = _planned_ticket(service, actors, "DEV-949")
    part_b = _planned_ticket(service, actors, "DEV-950")
    eligible = _planned_ticket(service, actors, "DEV-951")

    with pytest.raises(ConflictError, match="backlog, ready or blocked"):
        service.split_ticket(
            actors["planner"],
            active["ticket_id"],
            claim["ticket"]["version"],
            [part_a["ticket_id"], part_b["ticket_id"]],
            "No.",
        )
    with pytest.raises(ConflictError, match="version"):
        service.split_ticket(
            actors["planner"],
            eligible["ticket_id"],
            eligible["version"] + 3,
            [part_a["ticket_id"], part_b["ticket_id"]],
            "Stale.",
        )
    with pytest.raises(AuthorizationError):
        service.split_ticket(
            actors["developer"],
            eligible["ticket_id"],
            eligible["version"],
            [part_a["ticket_id"], part_b["ticket_id"]],
            "Not my call.",
        )


def test_split_refuses_active_leases_running_jobs_and_frozen_candidates(
    service, actors, seeded
) -> None:
    leased = _planned_ticket(service, actors, "DEV-952")
    claim = service.claim_ticket(actors["developer"], leased["ticket_id"], leased["version"], 900)
    blocked = service.report_blocker(
        actors["developer"],
        leased["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "Parked for now.",
    )
    part_a = _planned_ticket(service, actors, "DEV-953")
    part_b = _planned_ticket(service, actors, "DEV-954")

    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims,
                 submitted_by, submitted_at, frozen_at)
            VALUES ('cand_frozen_2', ?, 'a', 'b', 'branch', '[]', 'dev-01',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (leased["ticket_id"],),
        )

    with pytest.raises(ConflictError, match="frozen candidate"):
        service.split_ticket(
            actors["planner"],
            leased["ticket_id"],
            blocked["ticket"]["version"],
            [part_a["ticket_id"], part_b["ticket_id"]],
            "Too late.",
        )
