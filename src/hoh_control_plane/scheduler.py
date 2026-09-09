from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


def sweep_once(database_path: str | Path) -> dict[str, object]:
    """Run one trusted-local maintenance pass and return its public summary."""
    store = SQLiteStore(database_path)
    store.initialize()
    service = ControlPlaneService(store)
    scheduler = Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))
    recovered = service.sweep_expired_leases(scheduler)
    dispatch = service.dispatch_pending_work(scheduler)
    return {**recovered, "dispatch": dispatch}


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
        if arguments.once:
            return
        time.sleep(arguments.interval_seconds)


if __name__ == "__main__":
    main()
