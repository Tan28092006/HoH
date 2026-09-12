"""Experience indexed by the code it was learned in, not the ticket it closed with."""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError


LESSON = "The working interpreter is .venv-hoh; the .venv beside it has no pytest."


def _ticket(service, planner, ticket_id: str, scope: list[str]):
    return service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title=f"Work in {scope[0] if scope else 'nowhere named'}",
        objective="Touch the scope this lesson is about",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It behaves"],
        preservation_constraints=[],
        file_scope=scope,
    )


def _experience(service, actor, ticket_id: str):
    return service.get_ticket_context(actor, f"ticket:{ticket_id}")["experience"]


# --- reaching the agent who was not there --------------------------------------


def test_a_lesson_reaches_the_next_ticket_in_the_same_scope(
    service, actors, seeded
) -> None:
    """The whole point: nobody has to remember to go and read it."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-801", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-801", LESSON)
    _ticket(service, planner, "DEV-802", ["src/payments/retry.py"])

    surfaced = _experience(service, actors["developer"], "DEV-802")

    assert [note["lesson"] for note in surfaced] == [LESSON]


def test_a_lesson_stays_out_of_unrelated_work(service, actors, seeded) -> None:
    """Surfacing everything to everyone is how it becomes noise nobody reads."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-803", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-803", LESSON)
    _ticket(service, planner, "DEV-804", ["docs/**"])

    assert _experience(service, actors["developer"], "DEV-804") == []


def test_an_agent_is_not_shown_its_own_ticket_s_lesson(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-805", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-805", LESSON)

    assert _experience(service, actors["developer"], "DEV-805") == []


def test_a_lesson_recorded_later_still_reaches_work_already_in_flight(
    service, actors, seeded
) -> None:
    """Read-time, like inherited constraints: the ticket need not be re-planned."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-806", ["src/payments/**"])
    _ticket(service, planner, "DEV-807", ["src/payments/**"])
    assert _experience(service, actors["developer"], "DEV-807") == []

    service.record_experience(actors["developer"], "ticket:DEV-806", LESSON)

    assert _experience(service, actors["developer"], "DEV-807")


# --- repetition as signal --------------------------------------------------------


def test_the_same_lesson_twice_is_counted_not_duplicated(service, actors, seeded) -> None:
    """Eight copies of one trap is what this replaces."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-808", ["src/payments/**"])
    _ticket(service, planner, "DEV-809", ["src/payments/retry.py"])
    service.record_experience(actors["developer"], "ticket:DEV-808", LESSON)

    again = service.record_experience(actors["developer_2"], "ticket:DEV-809", LESSON)

    assert again["times_seen"] == 2
    assert len(service.list_experience(planner, "project-1", limit=100)) == 1


def test_spacing_alone_does_not_make_a_second_lesson(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-810", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-810", LESSON)

    again = service.record_experience(
        actors["developer"], "ticket:DEV-810", f"  {LESSON}\n"
    )

    assert again["times_seen"] == 2


def test_what_has_bitten_most_is_read_first(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-811", ["src/payments/**"])
    _ticket(service, planner, "DEV-812", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-811", "Rarely bites.")
    for _ in range(3):
        service.record_experience(actors["developer"], "ticket:DEV-811", LESSON)

    surfaced = _experience(service, actors["developer"], "DEV-812")

    assert surfaced[0]["lesson"] == LESSON
    assert surfaced[0]["times_seen"] == 3


def test_the_surfaced_list_is_bounded(service, actors, seeded) -> None:
    """It competes with the ticket for the same budget, so it cannot grow freely."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-813", ["src/payments/**"])
    _ticket(service, planner, "DEV-814", ["src/payments/**"])
    for index in range(9):
        service.record_experience(actors["developer"], "ticket:DEV-813", f"Lesson {index}")

    assert len(_experience(service, actors["developer"], "DEV-814")) <= 5


# --- what must not be recordable --------------------------------------------------


def test_a_lesson_with_no_scope_is_refused(service, actors, seeded) -> None:
    """An empty scope overlaps everything, which is the same as no index at all."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-815", [])

    with pytest.raises(ValidationError) as error:
        service.record_experience(actors["developer"], "ticket:DEV-815", LESSON)

    assert error.value.details["reason"] == "experience_scope_required"


def test_a_lesson_is_bounded_in_size(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-816", ["src/payments/**"])

    with pytest.raises(ValidationError):
        service.record_experience(actors["developer"], "ticket:DEV-816", "x" * 2001)
    with pytest.raises(ValidationError):
        service.record_experience(actors["developer"], "ticket:DEV-816", "   ")


def test_recording_needs_access_to_the_project(service, actors, seeded) -> None:
    planner = actors["planner"]
    _ticket(service, planner, "DEV-817", ["src/payments/**"])
    outsider = Actor("outsider-01", Role.DEVELOPER, frozenset({"project-9"}))

    with pytest.raises(AuthorizationError):
        service.record_experience(outsider, "ticket:DEV-817", LESSON)
    with pytest.raises(AuthorizationError):
        service.list_experience(outsider, "project-1")


def test_every_role_may_record_what_it_tripped_over(service, actors, seeded) -> None:
    """QA and the Integrator trip over different traps than a Developer does."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-818", ["src/payments/**"])

    for role in ("qa", "integrator", "planner"):
        note = service.record_experience(
            actors[role], "ticket:DEV-818", f"Learned by {role}."
        )
        assert note["actor_role"] == actors[role].role.value


def test_recording_a_lesson_does_not_touch_the_ticket(service, actors, seeded) -> None:
    planner = actors["planner"]
    created = _ticket(service, planner, "DEV-819", ["src/payments/**"])

    service.record_experience(actors["developer"], "ticket:DEV-819", LESSON)

    after = service.get_ticket_context(planner, "ticket:DEV-819")["ticket"]
    assert after["version"] == created["version"]
    assert after["status"] == created["status"]


# --- the index has to agree with the rule it replaced ----------------------------


@pytest.mark.parametrize(
    "note_scope,ticket_scope",
    [
        (["src/payments/**"], ["src/payments/**"]),
        (["src/payments/**"], ["src/payments/retry.py"]),
        (["src/payments/retry.py"], ["src/payments/**"]),
        (["src/**"], ["src/payments/deep/nested.py"]),
        (["src/payments/**"], ["src/pay/**"]),
        (["src/payments/**"], ["docs/**"]),
        (["**/conftest.py"], ["src/payments/**"]),
        (["src/a/**", "docs/**"], ["docs/guide.md"]),
        (["src/payments/**"], ["src/payments2/**"]),
    ],
)
def test_the_indexed_query_answers_what_the_overlap_rule_says(
    service, actors, seeded, note_scope, ticket_scope
) -> None:
    """Pushing the filter into SQL must not change which lessons are found."""
    from hoh_control_plane import scopes

    planner = actors["planner"]
    _ticket(service, planner, "DEV-900", note_scope)
    service.record_experience(actors["developer"], "ticket:DEV-900", LESSON, note_scope)
    _ticket(service, planner, "DEV-901", ticket_scope)

    surfaced = _experience(service, actors["developer"], "DEV-901")

    expected = any(
        scopes.roots_overlap(left, right)
        for left in scopes.roots_of(note_scope)
        for right in scopes.roots_of(ticket_scope)
    )
    assert bool(surfaced) is expected, (
        f"{note_scope} against {ticket_scope}: the index and the rule disagree"
    )


def test_a_note_written_before_the_index_existed_is_not_lost(
    service, actors, seeded
) -> None:
    """Upgrading must not make an already-recorded lesson invisible."""
    planner = actors["planner"]
    _ticket(service, planner, "DEV-902", ["src/payments/**"])
    service.record_experience(actors["developer"], "ticket:DEV-902", LESSON)
    _ticket(service, planner, "DEV-903", ["src/payments/retry.py"])
    with service.store.connection(immediate=True) as connection:
        connection.execute("DELETE FROM experience_note_scopes")
    assert _experience(service, actors["developer"], "DEV-903") == [], "index really gone"

    with service.store.connection(immediate=True) as connection:
        from hoh_control_plane.store import backfill_experience_scopes

        indexed = backfill_experience_scopes(connection)

    assert indexed, "it had something to rebuild"
    assert _experience(service, actors["developer"], "DEV-903"), "and the lesson is back"


def test_surfacing_uses_the_index_instead_of_reading_every_note(
    service, actors, seeded
) -> None:
    """A context read is the hottest path there is.

    Asserted on the query plan rather than on a stopwatch: a timing threshold
    passes on a fast machine no matter how the query is written, and this is a
    claim about how the work grows, not about how long it took here.
    """
    from hoh_control_plane import scopes
    from hoh_control_plane.service.experience import ExperienceMixin

    planner = actors["planner"]
    for index in range(40):
        holder = f"DEV-91{index:02d}"
        _ticket(service, planner, holder, [f"src/unrelated{index}/**"])
        service.record_experience(actors["developer"], f"ticket:{holder}", f"Lesson {index}")
    _ticket(service, planner, "DEV-999", ["src/payments/**"])

    assert _experience(service, actors["developer"], "DEV-999") == [], (
        "none of those 40 notes touch this scope"
    )

    clause, parameters = ExperienceMixin._overlap_clause(scopes.roots_of(["src/payments/**"]))
    with service.store.connection() as connection:
        plan = "\n".join(
            str(row[3])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN SELECT note_id FROM experience_note_scopes WHERE {clause}",
                tuple(parameters),
            ).fetchall()
        )

    assert "SCAN experience_note_scopes" not in plan, (
        f"the scope lookup reads the whole table:\n{plan}"
    )
    assert "experience_note_scopes_root" in plan or "SEARCH" in plan, plan
