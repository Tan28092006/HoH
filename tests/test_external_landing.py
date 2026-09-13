"""Recording work that landed outside the gate, without certifying it (HOH-249)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import (
    AuthorizationError,
    ConflictError,
    Role,
    ValidationError,
)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


@pytest.fixture
def landed(service, actors, seeded, tmp_path: Path):
    """A repository whose default branch already carries a commit for the ticket."""
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    (repository / "hotfix.txt").write_text("applied by hand\n", encoding="utf-8")
    _git(repository, "add", "hotfix.txt")
    _git(repository, "commit", "-m", "Emergency fix applied outside the gate")
    on_branch = _git(repository, "rev-parse", "HEAD")

    # A commit that exists but was never merged.
    _git(repository, "checkout", "-b", "abandoned")
    (repository / "elsewhere.txt").write_text("never merged\n", encoding="utf-8")
    _git(repository, "add", "elsewhere.txt")
    _git(repository, "commit", "-m", "Work that never landed")
    off_branch = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "main")

    service.configure_repository(actors["planner"], "project-1", str(repository))
    return {
        "repository": repository,
        "ticket": seeded["ticket"],
        "on_branch": on_branch,
        "off_branch": off_branch,
    }


def _record(service, actor, landed, commit=None, version=None):
    ticket = landed["ticket"]
    return service.record_external_landing(
        actor,
        ticket["ticket_id"],
        version if version is not None else ticket["version"],
        commit or landed["on_branch"],
        "Applied by hand during an incident; recording so the board stops lying.",
    )


def test_a_commit_really_on_the_branch_can_be_recorded(service, actors, landed) -> None:
    result = _record(service, actors["integrator"], landed)

    assert result["ticket"]["status"] == "DONE"
    assert result["merge_status"] == "landed_ungated"
    assert result["gated"] is False


def test_expired_lease_cannot_resurrect_recorded_landing(service, actors, landed):
    from hoh_control_plane.domain import Actor
    ticket = landed["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    with service.store.connection() as connection:
        connection.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z'")
    result = _record(service, actors["integrator"], landed, version=claim["ticket"]["version"])
    service.sweep_expired_leases(Actor("maintenance", Role.ADMIN, frozenset({"project-1"})))
    current = service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]
    assert current["status"] == "DONE"
    assert current["version"] == result["ticket"]["version"]


@pytest.mark.parametrize("terminal", ["DONE", "CANCELLED", "SUPERSEDED", "BLOCKED"])
@pytest.mark.parametrize("method", ["sweep", "claim"])
def test_stale_lease_cannot_override_a_later_lifecycle_decision(
    service, actors, seeded, terminal, method,
):
    from hoh_control_plane.domain import Actor
    ticket = seeded["ticket"]
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    # Simulate an older server's orphaned lease in a disposable test database.
    with service.store.connection() as connection:
        connection.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00Z'")
        connection.execute("UPDATE tickets SET status = ?, version = version + 1", (terminal,))
    if method == "sweep":
        service.sweep_expired_leases(Actor("maintenance", Role.ADMIN, frozenset({"project-1"})))
    else:
        with service.store.connection(immediate=True) as connection:
            service._expire_lease(connection, ticket["ticket_id"])
    current = service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]
    assert current["status"] == terminal
    assert current["version"] == 3


def test_a_commit_that_never_landed_is_refused(service, actors, landed) -> None:
    """The caller's claim that something shipped is exactly what must be checked."""
    with pytest.raises(ValidationError) as error:
        _record(service, actors["integrator"], landed, commit=landed["off_branch"])

    assert error.value.details["reason"] == "landing_commit_not_on_branch"
    with service.store.connection() as connection:
        status = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?",
            (landed["ticket"]["ticket_id"],),
        ).fetchone()["status"]
    assert status != "DONE", "nothing is recorded when the claim does not hold"


def test_the_record_says_plainly_that_it_did_not_pass_the_gate(
    service, actors, landed
) -> None:
    """A reader of the ticket must not be able to mistake this for a verdict."""
    _record(service, actors["integrator"], landed)

    with service.store.connection() as connection:
        entry = dict(
            connection.execute(
                """
                SELECT * FROM context_entries WHERE ticket_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (landed["ticket"]["ticket_id"],),
            ).fetchone()
        )
    assert entry["entry_type"] == "integration_landed_ungated"
    assert "did not pass the gate" in entry["content"]
    assert "no QA verdict" in entry["content"]
    assert landed["on_branch"] in entry["content"]


def test_the_event_cannot_be_counted_as_a_gated_merge(service, actors, landed) -> None:
    _record(service, actors["integrator"], landed)

    with service.store.connection() as connection:
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM audit_events WHERE aggregate_id = ? ORDER BY sequence DESC",
                (landed["ticket"]["ticket_id"],),
            ).fetchall()
        ]
    latest = events[0]
    assert latest["event_type"] == "integration.landed_ungated"
    assert latest["event_type"] != "integration.merged"
    assert '"gated": false' in latest["payload"].replace(" ", " ")
    assert '"qa_verified": false' in latest["payload"]


def test_it_certifies_nothing_about_quality(service, actors, landed) -> None:
    """Recording that work shipped is not the same as saying it was any good."""
    ticket = landed["ticket"]

    _record(service, actors["integrator"], landed)

    with service.store.connection() as connection:
        findings = connection.execute(
            """
            SELECT COUNT(*) FROM qa_findings fi
            JOIN candidates c ON c.candidate_id = fi.candidate_id
            WHERE c.ticket_id = ?
            """,
            (ticket["ticket_id"],),
        ).fetchone()[0]
        evidence = connection.execute(
            """
            SELECT COUNT(*) FROM evidence ev
            JOIN candidates c ON c.candidate_id = ev.candidate_id
            WHERE c.ticket_id = ?
            """,
            (ticket["ticket_id"],),
        ).fetchone()[0]
    assert findings == 0
    assert evidence == 0


def test_only_the_integrator_may_record_a_landing(service, actors, landed) -> None:
    for role in ("developer", "qa", "planner"):
        with pytest.raises(AuthorizationError):
            _record(service, actors[role], landed)


def test_work_still_in_flight_must_finish_first(service, actors, landed) -> None:
    ticket = landed["ticket"]
    service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )

    with pytest.raises(ConflictError) as error:
        _record(service, actors["integrator"], landed, version=ticket["version"] + 1)

    assert error.value.details["reason"] == "landing_lease_active"


def test_a_ticket_already_terminal_is_refused(service, actors, landed) -> None:
    first = _record(service, actors["integrator"], landed)

    with pytest.raises(ConflictError):
        _record(
            service, actors["integrator"], landed, version=first["ticket"]["version"]
        )


def test_a_stale_version_is_refused(service, actors, landed) -> None:
    with pytest.raises(ConflictError):
        _record(service, actors["integrator"], landed, version=999)


def test_the_default_branch_is_not_moved(service, actors, landed) -> None:
    before = _git(landed["repository"], "rev-parse", "main")

    _record(service, actors["integrator"], landed)

    assert _git(landed["repository"], "rev-parse", "main") == before


def test_the_ticket_stops_being_offered_as_work(service, actors, landed) -> None:
    _record(service, actors["integrator"], landed)

    discovered = service.discover_work(actors["developer"], "project-1")

    offered = {
        entry["ticket_id"]
        for feature in discovered["features"]
        for entry in feature["tickets"]
    }
    assert landed["ticket"]["ticket_id"] not in offered
