"""Ticket state can be explained from the audit log.

Every ticket creation and every guarded transition writes one
``ticket.transitioned`` event in the same transaction. Folding those events in
sequence order must land on exactly the status and version each ticket row
holds. The fold is a reconciliation between the view and the log: it does not
replay commands, but it says how every ticket reached its state, and a write
that bypassed the log shows up as a divergence.
"""
from __future__ import annotations

import json
from pathlib import Path

from merge_helpers import merge_through_queue

from hoh_control_plane.domain import Role
from hoh_control_plane.service._common import TICKET_TRANSITION_EVENT, replay_ticket_states
from test_integration_async import REGRESSION_COMMAND, _git, _repository


def _ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _context(service, actor, ticket_id: str) -> str:
    return service.get_ticket_context(actor, ticket_id)["context_snapshot_id"]


def _resolve_pending(service, planner, project_id: str = "project-1") -> None:
    """Resolve every pending reconciliation with the disposition the board shows."""
    disposition_for = {"CANCELLED": "cancel", "DONE": "complete"}
    while True:
        pending = sorted(
            (
                item for item in service.list_repository_reconciliations(
                    planner, project_id
                )["reconciliations"]
                if item["status"] == "PENDING"
            ),
            key=lambda item: item["created_at"],
        )
        if not pending:
            return
        reconciliation = pending[0]
        decisions = [
            {
                "ticket_id": ticket_id,
                "disposition": disposition_for.get(_ticket(service, ticket_id)["status"], "keep"),
                "reason": "Reviewed for the replay scenario.",
            }
            for ticket_id in reconciliation["ticket_versions"]
        ]
        service.resolve_repository_reconciliation(
            planner, project_id, reconciliation["reconciliation_id"], decisions,
            "Reviewed for the replay scenario.",
        )


def _findings(ticket: dict, status: str) -> list[dict]:
    return [
        {
            "criterion": criterion,
            "status": status,
            "severity": "none" if status == "verified" else "high",
            "observation": "Checked against the frozen candidate.",
            "evidence": [{
                "type": "test_result",
                "observation": status,
                "storage_ref": f"evidence://qa/{index}-{status}.json",
            }],
        }
        for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
    ]


def _develop(service, developer, ticket_id: str, path: str, text: str) -> dict:
    claim = service.claim_ticket(developer, ticket_id, _ticket(service, ticket_id)["version"], 900)
    worktree = service.prepare_worktree(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"]
    )["worktree"]
    root = Path(worktree["worktree_path"])
    change = root / path
    change.parent.mkdir(parents=True, exist_ok=True)
    change.write_text(text, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "commit", "-m", f"Write {path}")
    return service.submit_candidate(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
        _git(root, "rev-parse", "HEAD"), worktree["base_commit"], worktree["branch_ref"],
        [f"{path} is ready for QA."],
        context_snapshot_id=_context(service, developer, ticket_id),
    )


def _assess(service, qa, ticket_id: str, submitted: dict, verdict: str) -> dict:
    qa_claim = service.claim_qa(qa, ticket_id, submitted["ticket"]["version"], 900)
    return service.submit_qa_verdict(
        qa, ticket_id, qa_claim["lease"]["lease_id"], submitted["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"], verdict,
        _findings(_ticket(service, ticket_id), "verified" if verdict == "passed" else "failed"),
        context_snapshot_id=_context(service, qa, ticket_id),
    )


def _project_tickets(service, project_id: str = "project-1") -> dict[str, dict]:
    with service.store.connection() as connection:
        rows = connection.execute(
            """
            SELECT t.ticket_id, t.status, t.version FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id WHERE f.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    return {row["ticket_id"]: {"status": row["status"], "version": row["version"]} for row in rows}


def test_folding_the_transition_events_reproduces_every_ticket(
    service, actors, seeded, tmp_path
) -> None:
    planner, developer, developer_2 = actors["planner"], actors["developer"], actors["developer_2"]
    repository = _repository(tmp_path)
    service.configure_repository(planner, "project-1", str(repository))
    service.configure_test_command(planner, "project-1", REGRESSION_COMMAND, 60)
    main_id = seeded["ticket"]["ticket_id"]
    feature_id = seeded["feature"]["feature_id"]

    def create(ticket_id: str, scope: str, ready: bool) -> dict:
        return service.create_ticket(
            planner, feature_id=feature_id, ticket_id=ticket_id, title=f"Work {ticket_id}",
            objective="Bounded work for the replay scenario", required_role=Role.DEVELOPER,
            acceptance_criteria=["It behaves as specified"], preservation_constraints=[],
            file_scope=[scope], ready=ready,
        )

    # Cancelled straight from the backlog.
    cancelled = create("DEV-150", "src/cancelled/**", ready=False)
    service.cancel_ticket(planner, "DEV-150", cancelled["version"], "No longer needed.")

    # Claimed, then retired through a reconciliation while a lease was live.
    retired = create("DEV-160", "src/retired/**", ready=True)
    service.claim_ticket(developer_2, "DEV-160", retired["version"], 900)

    # The main ticket: submit, fail QA, rework, submit again, pass, merge.
    first = _develop(service, developer, main_id, "src/checkout/retry.txt", "first try\n")
    rejected = _assess(service, actors["qa"], main_id, first, "failed")
    assert rejected["ticket"]["status"] == "REWORK"
    pending = [
        item for item in service.list_repository_reconciliations(planner, "project-1")["reconciliations"]
        if item["status"] == "PENDING"
    ]
    service.retire_ticket_for_reconciliation(
        planner, "DEV-160", _ticket(service, "DEV-160")["version"],
        pending[0]["reconciliation_id"], "The QA finding makes this work obsolete.",
    )
    assert _ticket(service, "DEV-160")["status"] == "CANCELLED"
    _resolve_pending(service, planner)

    second = _develop(service, developer_2, main_id, "src/checkout/fix.txt", "second try\n")
    assert second["candidate"]["base_commit"] == first["candidate"]["base_commit"]
    verified = _assess(service, actors["qa"], main_id, second, "passed")
    assert verified["ticket"]["status"] == "VERIFIED"
    integration = service.claim_integration(
        actors["integrator"], main_id, verified["ticket"]["version"], 900
    )
    merged = merge_through_queue(
        service, actors["integrator"], main_id, integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )
    assert merged["ticket"]["status"] == "DONE"
    assert _git(repository, "rev-parse", "main") == second["candidate"]["commit_sha"]

    with service.store.connection() as connection:
        replayed = replay_ticket_states(connection, "project-1")
    current = _project_tickets(service)

    assert set(replayed) == set(current) == {main_id, "DEV-150", "DEV-160"}
    for ticket_id, row in current.items():
        state = replayed[ticket_id]
        assert state["replay_errors"] == [], ticket_id
        assert (state["status"], state["version"]) == (row["status"], row["version"]), ticket_id
    assert (current["DEV-150"]["status"], current["DEV-160"]["status"]) == ("CANCELLED", "CANCELLED")
    # Every version step of the main ticket is one logged transition.
    assert replayed[main_id]["transitions"] == current[main_id]["version"]


def test_a_transition_event_names_both_ends_and_the_changed_fields(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    with service.store.connection() as connection:
        rows = connection.execute(
            """
            SELECT actor_id, aggregate_version, payload FROM audit_events
            WHERE aggregate_id = ? AND event_type = ? ORDER BY sequence
            """,
            (ticket["ticket_id"], TICKET_TRANSITION_EVENT),
        ).fetchall()
    created, claimed = [dict(row) for row in rows]

    assert json.loads(created["payload"]) == {
        "from_status": None, "to_status": "READY", "from_version": 0, "to_version": 1,
        "changed_fields": ["created"],
    }
    assert json.loads(claimed["payload"]) == {
        "from_status": "READY", "to_status": "DEV_IN_PROGRESS",
        "from_version": ticket["version"], "to_version": claim["ticket"]["version"],
        "changed_fields": ["status"],
    }
    assert claimed["aggregate_version"] == claim["ticket"]["version"]
    assert claimed["actor_id"] == actors["developer"].agent_id


def test_a_write_that_bypasses_the_log_shows_up_as_a_divergence(service, seeded) -> None:
    ticket_id = seeded["ticket"]["ticket_id"]
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET version = version + 1 WHERE ticket_id = ?", (ticket_id,)
        )
    with service.store.connection() as connection:
        replayed = replay_ticket_states(connection, "project-1")

    assert replayed[ticket_id]["version"] == 1
    assert _project_tickets(service)[ticket_id]["version"] == 2


def test_a_gap_in_the_log_is_reported_not_raised(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "DELETE FROM audit_events WHERE aggregate_id = ? AND event_type = ? "
            "AND aggregate_version = 1",
            (ticket["ticket_id"], TICKET_TRANSITION_EVENT),
        )
        replayed = replay_ticket_states(connection, "project-1")

    errors = replayed[ticket["ticket_id"]]["replay_errors"]
    assert len(errors) == 1 and "from_version 1 after version 0" in errors[0]
