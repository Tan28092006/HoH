"""HOH-283 concurrency on the existing disposable PostgreSQL fixture."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from conftest import FakeProber
from helpers.postgres import pg_service, pg_seeded, pytestmark
from hoh_control_plane.domain import Actor, Role


def test_postgres_last_slot_and_retry_share_one_attempt(pg_service, pg_seeded):
    service = pg_service
    planner = pg_seeded["planner"]
    actors = {role: Actor(f"autonomy-pg-{role}", Role(role), frozenset({"project-pg"}))
              for role in ("developer", "qa", "integrator")}
    service.configure_verification_profile(planner, "project-pg", ["build_execution"], quality_policy="legacy")
    service.capability_prober = FakeProber(equipped=["build_execution"])
    for role, actor in actors.items():
        service.register_agent(actor, skills=[f"hoh-{role}"], machine_id="qa-host-a",
            execution_capabilities=["build_execution"], model_family="anthropic" if role == "qa" else "openai",
            presence_seconds=3600)
    service.run_capability_preflight(actors["qa"], "project-pg")
    second = service.create_ticket(planner, "f-pg", "PG-2", "Disjoint work", "Another last-slot contender",
                                  Role.DEVELOPER, ["No excess admission"], [], ["other/**"], ready=True)
    tickets = [pg_seeded["ticket"]["ticket_id"], second["ticket_id"]]
    run = service.autonomy_start(planner, "project-pg",
        dict(wall_seconds=600, launch_attempts=1, in_flight=2, suite_starts=2, landed_tickets=2),
        {role: actor.agent_id for role, actor in actors.items()}, "pg-start", ticket_ids=tickets)
    run_id = run["run"]["run_id"]
    packets = [service.get_ticket_context(actors["developer"], ticket) for ticket in tickets]
    barrier = Barrier(2)
    def call(index):
        barrier.wait(timeout=10)
        return service.autonomy_admit(actors["developer"], run_id, tickets[index],
            packets[index]["ticket"]["version"], packets[index]["context_snapshot_id"], f"pg-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert sum(result["admitted"] for result in results) == 1
    winner = next(index for index, result in enumerate(results) if result["admitted"])
    replay = service.autonomy_admit(actors["developer"], run_id, tickets[winner],
        packets[winner]["ticket"]["version"], packets[winner]["context_snapshot_id"], f"pg-{winner}")
    assert replay["attempt_id"] == results[winner]["attempt_id"]
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM autonomy_attempts WHERE run_id = ?", (run_id,)).fetchone()[0] == 1
