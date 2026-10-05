from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from hoh_control_plane.agent_wakeup import SECRET_VARIABLE, deliver_pending_offers
from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.planner_wakeup import deliver_pending
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store_factory import database_path_from_environment, store_from_environment


def sweep_once(database_path: str | Path | None) -> dict[str, object]:
    """Run one trusted-local maintenance pass and return its public summary."""
    store = store_from_environment(database_path)
    service = ControlPlaneService(store)
    scheduler = Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))
    recovered = service.sweep_expired_leases(scheduler)
    # Git is observed here too, not only while a Planner waits: a commit opens
    # its reconciliation, a candidate that landed outside HoH is recorded as an
    # ungated landing by system:sweep, and a merge job recovery cannot settle
    # reaches Planner as a job. Throttled per project
    # by the same observed_at the Planner wait uses. Before dispatch, so work is
    # not offered past a barrier this pass opened.
    repositories = service.sweep_repositories(scheduler)
    dispatch = service.dispatch_pending_work(scheduler)
    # An urgent Planner problem should not wait for the next backlog scan; a
    # failed attempt simply leaves the request pending for the following sweep.
    wakeups = deliver_pending(service)
    # After dispatch, not before: dispatch is what creates the offers this knocks
    # about, so the same tick that queues work is the one that announces it.
    agent_wakeups = deliver_pending_offers(service, secret=os.getenv(SECRET_VARIABLE))
    return {
        **recovered,
        "repositories": repositories,
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
    # No relative default: refuse to start without HOH_DATABASE_PATH (or a URL).
    database_path = database_path_from_environment()

    while True:
        result = sweep_once(database_path)
        if result["count"]:
            print(f"Requeued {result['count']} expired lease(s).", flush=True)
        repositories = result["repositories"]
        landed = repositories["candidates_already_on_main"]
        recorded = sum(1 for item in landed if item.get("recorded"))
        raised = len(landed) - recorded
        stuck = len(repositories["stuck_merge_jobs"])
        if recorded:
            print(f"Recorded {recorded} landing(s) already on the default branch as "
                  "ungated (system:sweep).", flush=True)
        if raised or stuck:
            print(f"Raised {raised} candidate(s) already on the default branch and "
                  f"{stuck} stuck merge job(s) for Planner.", flush=True)
        if repositories["failed"]:
            # Only the error type: a Git message can carry a remote URL.
            kinds = sorted({item["error_type"] for item in repositories["failed"]})
            print(f"Repository sweep failed {len(repositories['failed'])} step(s): "
                  f"{', '.join(kinds)}.", flush=True)
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
