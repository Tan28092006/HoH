"""Actual simultaneous operations; enabled with HOH_TEST_POSTGRES_DSN."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from test_postgres_store import pg_service, pg_seeded, _developer, pytestmark
from hoh_control_plane.domain import ConflictError, Role


def test_overlapping_scopes_on_different_tickets_cannot_both_be_claimed(pg_service, pg_seeded):
    second = pg_service.create_ticket(
        pg_seeded["planner"], "f-pg", "PG-2", "Same scope", "Exclusive write scope",
        Role.DEVELOPER, ["Only one writer"], [], ["src/**"], ready=True,
    )
    barrier = Barrier(2)

    def claim(index):
        ticket = [pg_seeded["ticket"], second][index]
        barrier.wait(timeout=5)
        try:
            pg_service.claim_ticket(_developer(str(index)), ticket["ticket_id"], ticket["version"], 900)
            return "claimed"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (0, 1)))
    assert sorted(outcomes) == ["claimed", "conflict"]
