from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from hoh_control_plane.agent_wakeup import SECRET_VARIABLE, deliver_pending_offers
from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.planner_wakeup import deliver_pending
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import store_from_environment


def sweep_once(database_path: str | Path) -> dict[str, object]:
    """Run one trusted-local maintenance pass and return its public summary."""
    store = store_from_environment(database_path)
    service = ControlPlaneService(store)
    scheduler = Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))
    recovered = service.sweep_expired_leases(scheduler)
    dispatch = service.dispatch_pending_work(scheduler)
    # An urgent Planner problem should not wait for the next backlog scan; a
    # failed attempt simply leaves the request pending for the following sweep.
    wakeups = deliver_pending(service)
    # After dispatch, not before: dispatch is what creates the offers this knocks
    # about, so the same tick that queues work is the one that announces it.
    agent_wakeups = deliver_pending_offers(service, secret=os.getenv(SECRET_VARIABLE))
    return {
        **recovered,
        "dispatch": dispatch,
        "planner_wakeups": wakeups,
        "agent_wakeups": agent_wakeups,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep expired Harness Control leases")
    parser.add_argument("--once", action="store_true", help="Run one sweep then exit")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.getenv("HOH_SCHEDULER_INTERVAL_SECONDS", "30")),
        help="Seconds between sweeps when not using --once (5-3600)",
    )
    arguments = parser.parse_args()
    if not 5 <= arguments.interval_seconds <= 3600:
        raise SystemExit("--interval-seconds must be between 5 and 3600")
    database_path = Path(os.getenv("HOH_DATABASE_PATH", "data/control-plane.db"))

    while True:
        result = sweep_once(database_path)
        if result["count"]:
            print(f"Requeued {result['count']} expired lease(s).", flush=True)
        if result["dispatch"]["offered_count"]:
            print(f"Offered {result['dispatch']['offered_count']} queued hand-off(s).", flush=True)
        if result["agent_wakeups"]["delivered"]:
            woken = len(result["agent_wakeups"]["delivered"])
            print(f"Woke {woken} agent(s) about offered work.", flush=True)
        if arguments.once:
            return
        time.sleep(arguments.interval_seconds)


if __name__ == "__main__":
    main()
